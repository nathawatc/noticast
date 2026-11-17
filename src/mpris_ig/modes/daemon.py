"""Daemon mode: poll MPRIS, debounce track changes, apply anti-bot jitter/cooldown, and post."""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable
from typing import Any

from mpris_ig.anti_bot import CooldownGuard, TrackDebouncer, random_jitter
from mpris_ig.config import AppConfig
from mpris_ig.mpris import (
    DbusMprisBackend,
    MprisBackendError,
    MprisTrack,
)
from mpris_ig.notes_service import (
    ActiveMusicNote,
    CheckpointRequiredError,
    InstagramNotesService,
    NotesApiError,
    RateLimitedError,
    SessionExpiredError,
)
from mpris_ig.session import SessionNotReadyError, load_authenticated_client
from mpris_ig.state import NoteState, load_note_state, save_note_state
from mpris_ig.text_utils import (
    format_note_text,
    is_meaningful_substring,
    normalize_match_key,
    strip_title_noise,
)


class DaemonRunner:
    """Stateful MPRIS-to-Instagram Music Notes daemon loop."""

    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
        *,
        backend: Any,
        service: InstagramNotesService,
        dry_run: bool = False,
        stop_event: threading.Event | None = None,
        clock_fn: Callable[[], float] = time.monotonic,
        jitter_fn: Callable[[float, float], float] = random_jitter,
    ) -> None:
        self._config = config
        self._logger = logger
        self._backend = backend
        self._service = service
        self._dry_run = dry_run or config.note.dry_run
        self._stop_event = stop_event or threading.Event()
        self._clock_fn = clock_fn
        self._jitter_fn = jitter_fn

        self._debouncer = TrackDebouncer(config.anti_bot.debounce_seconds)
        self._cooldown = CooldownGuard(
            config.anti_bot.min_post_interval_seconds,
            config.anti_bot.rate_limit_cooldown_seconds,
        )
        self._last_seen_status: str | None = None
        self._startup_checked = not config.note.check_existing_on_startup
        self._startup_active_note: ActiveMusicNote | None = None
        self._saved_state: NoteState | None = load_note_state(config.paths.state_file)
        self._has_active_note: bool = False

    def _matches_startup_note_metadata(self, track: MprisTrack) -> bool:
        """Check if active Note metadata or note_id-verified state already matches track."""
        active = self._startup_active_note
        if active is None or active.note.note_style != 1:
            return False

        goal_title = normalize_match_key(strip_title_noise(track.title) or track.title)
        raw_title = normalize_match_key(track.title)
        goal_artist = normalize_match_key(track.artist_display)

        if active.track_title:
            note_title = normalize_match_key(active.track_title)
            note_artist = normalize_match_key(active.artist_name)
            title_ok = note_title in (goal_title, raw_title) or (
                is_meaningful_substring(note_title, goal_title)
                or is_meaningful_substring(note_title, raw_title)
            )
            artist_ok = (
                not goal_artist
                or not note_artist
                or note_artist == goal_artist
                or is_meaningful_substring(note_artist, goal_artist)
            )
            if title_ok and artist_ok:
                return True

        state = self._saved_state
        if state is not None and state.note_id == str(active.note.id):
            saved_mpris = (
                normalize_match_key(state.mpris_title),
                normalize_match_key(state.mpris_artist),
            )
            if saved_mpris == (raw_title, goal_artist):
                return True
            if normalize_match_key(state.ig_title) in (goal_title, raw_title):
                ig_artist = normalize_match_key(state.ig_artist)
                if not goal_artist or ig_artist == goal_artist:
                    return True

        return False

    def _run_startup_note_check(self) -> int | None:
        """Fetch active Note once on startup. Return 1 on fatal auth failure, else None."""
        if self._startup_checked:
            return None
        try:
            active = self._service.get_active_music_note()
        except (SessionExpiredError, CheckpointRequiredError) as exc:
            self._logger.error("[Auth] Stopping daemon safely: %s", exc)
            return 1
        except RateLimitedError as exc:
            wait_s = self._cooldown.record_rate_limit(now=self._clock_fn())
            self._startup_checked = True
            self._logger.warning(
                "[RateLimit] Startup check throttled (%s) — pausing writes for %.0fs.",
                exc,
                wait_s,
            )
            return None
        except NotesApiError as exc:
            self._startup_checked = True
            self._logger.warning("[IG] Startup Note check failed (continuing): %s", exc)
            return None

        self._startup_checked = True
        self._startup_active_note = active
        if active is None:
            self._logger.info("[IG] Startup check: no active personal Note on Instagram.")
        else:
            self._logger.info(
                "[IG] Startup check: active Note id=%s "
                "(style=%s, title=%r, artist=%r, cluster_id=%s).",
                active.note.id,
                active.note.note_style,
                active.track_title or active.note.text,
                active.artist_name,
                active.audio_cluster_id or "-",
            )
        return None

    def _sleep_interruptible(self, seconds: float) -> bool:
        """Sleep up to `seconds`; return True if stop was requested."""
        if seconds <= 0:
            return self._stop_event.is_set()
        return self._stop_event.wait(timeout=seconds)

    def _poll_active_track(self) -> tuple[MprisTrack | None, bool]:
        try:
            return self._backend.get_active_track(), True
        except MprisBackendError as exc:
            self._logger.warning("[MPRIS] Failed to read active track (skipping cycle): %s", exc)
            return None, False

    def _handle_stop_cleanup(self, now: float) -> int | None:
        if not self._config.note.clear_on_stop or not self._has_active_note:
            return None
        allowed, remaining = self._cooldown.can_post(now=now)
        if not allowed:
            self._logger.debug("[Cooldown] Skipping clear_on_stop (%.1fs remaining).", remaining)
            return None
        if self._dry_run:
            self._logger.info("[Dry Run] Would clear personal note on playback stop.")
            self._has_active_note = False
            self._debouncer.clear_posted()
            return None
        try:
            self._service.delete_existing_user_notes()
        except (SessionExpiredError, CheckpointRequiredError) as exc:
            self._logger.error("[Auth] Stopping daemon safely: %s", exc)
            return 1
        except RateLimitedError as exc:
            wait_s = self._cooldown.record_rate_limit(now=self._clock_fn())
            self._logger.warning(
                "[RateLimit] %s — pausing clear_on_stop for %.0fs.",
                exc,
                wait_s,
            )
            return None
        except NotesApiError as exc:
            self._logger.warning("[IG] Failed to clear note on stop (will retry later): %s", exc)
            return None

        self._cooldown.record_post(now=now)
        self._has_active_note = False
        self._debouncer.clear_posted()
        return None

    def step(self) -> int | None:
        """Run one poll/debounce/post cycle. Return exit code to terminate, else None."""
        startup_rc = self._run_startup_note_check()
        if startup_rc is not None:
            return startup_rc

        now = self._clock_fn()
        track, ok = self._poll_active_track()
        if not ok:
            return None

        if track is None or track.status != "Playing":
            status = track.status if track else "None"
            if status != self._last_seen_status:
                self._logger.debug("[MPRIS] Playback state: %s", status)
                self._last_seen_status = status
            self._debouncer.observe(track, now=now)
            return self._handle_stop_cleanup(now)

        self._last_seen_status = track.status
        candidate = self._debouncer.observe(track, now=now)
        if candidate is None:
            return None

        if self._matches_startup_note_metadata(candidate):
            self._logger.info(
                "[IG] Active Note already matches current track (%s — %s); skipping initial post.",
                candidate.title,
                candidate.artist_display or "<unknown artist>",
            )
            self._debouncer.mark_posted(candidate)
            self._has_active_note = True
            self._startup_active_note = None
            return None

        allowed, remaining = self._cooldown.can_post(now=now)
        if not allowed:
            self._logger.debug(
                "[Cooldown] Waiting %.1fs before posting %s — %s",
                remaining,
                candidate.title,
                candidate.artist_display,
            )
            return None

        jitter_s = self._jitter_fn(
            self._config.anti_bot.jitter_min_seconds,
            self._config.anti_bot.jitter_max_seconds,
        )
        self._logger.info(
            "[Debounce] Stable track: %s — %s (%s). Waiting %.1fs jitter...",
            candidate.title,
            candidate.artist_display or "<unknown artist>",
            candidate.player_name,
            jitter_s,
        )
        if self._sleep_interruptible(jitter_s):
            return 0

        # Re-check MPRIS after jitter wait in case the user skipped or paused.
        confirmed, ok = self._poll_active_track()
        if (
            not ok
            or confirmed is None
            or confirmed.status != "Playing"
            or confirmed.identity_key != candidate.identity_key
        ):
            self._logger.info("[MPRIS] Track changed or paused during jitter wait; skipping post.")
            if ok:
                self._debouncer.observe(confirmed, now=self._clock_fn())
            return None

        try:
            matched = self._service.find_best_track(confirmed.title, confirmed.artist_display)
            if matched is None:
                self._logger.warning(
                    "[Match] No reliable Instagram track match for: %s — %s",
                    confirmed.title,
                    confirmed.artist_display,
                )
                self._debouncer.mark_posted(confirmed)
                return None

            ig_track, score = matched
            if (
                self._startup_active_note is not None
                and self._startup_active_note.audio_cluster_id
                and str(ig_track.audio_cluster_id) == self._startup_active_note.audio_cluster_id
            ):
                active_note_id = str(self._startup_active_note.note.id)
                self._logger.info(
                    "[IG] Active Note id=%s already matches cluster_id=%s (%s); skipping post.",
                    active_note_id,
                    ig_track.audio_cluster_id,
                    ig_track.title,
                )
                self._debouncer.mark_posted(confirmed)
                self._has_active_note = True
                self._startup_active_note = None
                save_note_state(
                    self._config.paths.state_file,
                    NoteState(
                        note_id=active_note_id,
                        mpris_title=confirmed.title,
                        mpris_artist=confirmed.artist_display,
                        ig_title=ig_track.title,
                        ig_artist=ig_track.display_artist,
                        audio_cluster_id=str(ig_track.audio_cluster_id),
                    ),
                )
                return None

            note_text = format_note_text(
                self._config.note.text_template,
                title=confirmed.title,
                artist=confirmed.artist_display,
            )
            audience = self._config.note.audience_enum

            if self._dry_run:
                self._logger.info(
                    "[Dry Run] Match score=%.2f -> %s — %s (cluster_id=%s, text=%r)",
                    score,
                    ig_track.title,
                    ig_track.display_artist,
                    ig_track.audio_cluster_id,
                    note_text,
                )
                self._debouncer.mark_posted(confirmed)
                self._has_active_note = True
                self._cooldown.record_post(now=self._clock_fn())
                return None

            note = self._service.replace_music_note(
                ig_track,
                text=note_text,
                audience=audience,
                delete_before_post=self._config.note.delete_before_post,
            )
            self._debouncer.mark_posted(confirmed)
            self._has_active_note = True
            self._cooldown.record_post(now=self._clock_fn())
            self._startup_active_note = None
            save_note_state(
                self._config.paths.state_file,
                NoteState(
                    note_id=str(note.id),
                    mpris_title=confirmed.title,
                    mpris_artist=confirmed.artist_display,
                    ig_title=ig_track.title,
                    ig_artist=ig_track.display_artist,
                    audio_cluster_id=str(ig_track.audio_cluster_id),
                ),
            )
            self._logger.info(
                "[IG] Posted Music Note id=%s: %s — %s (match=%.1f, cluster_id=%s)",
                note.id,
                ig_track.title,
                ig_track.display_artist,
                score,
                ig_track.audio_cluster_id,
            )
        except (SessionExpiredError, CheckpointRequiredError) as exc:
            self._logger.error("[Auth] Stopping daemon safely: %s", exc)
            return 1
        except RateLimitedError as exc:
            wait_s = self._cooldown.record_rate_limit(now=self._clock_fn())
            self._debouncer.reset_pending()
            self._logger.warning(
                "[RateLimit] %s — pausing Music Note writes for %.0fs.",
                exc,
                wait_s,
            )
        except NotesApiError as exc:
            self._debouncer.reset_pending()
            self._logger.warning("[IG] Temporary API error (will retry later): %s", exc)

        return None

    def run(self, *, once: bool = False) -> int:
        """Run the daemon polling loop until stopped or `once=True` finishes."""
        self._logger.info(
            "Starting MPRIS Music Notes daemon (poll=%.1fs, debounce=%.1fs, "
            "jitter=%.1f-%.1fs, min_interval=%.1fs, clear_on_stop=%s, dry_run=%s).",
            self._config.mpris.poll_interval_seconds,
            self._config.anti_bot.debounce_seconds,
            self._config.anti_bot.jitter_min_seconds,
            self._config.anti_bot.jitter_max_seconds,
            self._config.anti_bot.min_post_interval_seconds,
            self._config.note.clear_on_stop,
            self._dry_run,
        )

        while not self._stop_event.is_set():
            exit_code = self.step()
            if exit_code is not None:
                return exit_code
            if once:
                return 0

            base_poll = self._config.mpris.poll_interval_seconds
            poll_jitter = self._jitter_fn(0.0, min(1.0, base_poll * 0.2))
            self._sleep_interruptible(base_poll + poll_jitter)

        self._logger.info("Daemon stopped cleanly.")
        return 0


def run_daemon(
    config: AppConfig,
    logger: logging.Logger,
    *,
    dry_run: bool = False,
    once: bool = False,
    backend: Any = None,
    service: InstagramNotesService | None = None,
) -> int:
    """Initialize dependencies, attach signal handlers, and run the daemon."""
    try:
        if service is None:
            cl = load_authenticated_client(config)
            logger.info("Loaded session for user_id=%s (no login performed).", cl.user_id)
            service = InstagramNotesService(cl, logger)
        if backend is None:
            backend = DbusMprisBackend(config.mpris, logger)
    except (SessionNotReadyError, MprisBackendError) as exc:
        logger.error("%s", exc)
        return 1

    stop_event = threading.Event()

    def _handle_signal(signum: int, _frame: Any) -> None:
        logger.info("Received signal %s; shutting down...", signum)
        stop_event.set()

    prev_int = signal.getsignal(signal.SIGINT)
    prev_term = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        runner = DaemonRunner(
            config,
            logger,
            backend=backend,
            service=service,
            dry_run=dry_run,
            stop_event=stop_event,
        )
        return runner.run(once=once)
    finally:
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)
        close_fn = getattr(backend, "close", None)
        if callable(close_fn):
            close_fn()
