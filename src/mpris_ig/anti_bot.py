"""Anti-bot controls: track-change debouncing, randomized jitter, and cooldown guards."""

from __future__ import annotations

import random
import time

from mpris_ig.mpris import MprisTrack

DEFAULT_NOTE_TTL_SECONDS = 86_400.0


class TrackDebouncer:
    """Require a Playing track to stay unchanged for `debounce_seconds` before emitting."""

    def __init__(
        self,
        debounce_seconds: float,
        *,
        note_ttl_seconds: float = DEFAULT_NOTE_TTL_SECONDS,
    ) -> None:
        self._debounce_seconds: float = max(0.0, float(debounce_seconds))
        self._note_ttl_seconds: float = max(0.0, float(note_ttl_seconds))
        self._pending_key: tuple[str, str] | None = None
        self._pending_since: float = 0.0
        self._last_posted_key: tuple[str, str] | None = None
        self._last_posted_at: float | None = None

    @property
    def last_posted_key(self) -> tuple[str, str] | None:
        return self._last_posted_key

    def reset_pending(self) -> None:
        self._pending_key = None
        self._pending_since = 0.0

    def mark_posted(self, track: MprisTrack, *, now: float | None = None) -> None:
        self._last_posted_key = track.identity_key
        self._last_posted_at = time.monotonic() if now is None else now
        self._pending_key = None
        self._pending_since = 0.0

    def clear_posted(self) -> None:
        self._last_posted_key = None
        self._last_posted_at = None

    def observe(self, track: MprisTrack | None, *, now: float | None = None) -> MprisTrack | None:
        """Feed the latest MPRIS observation; return track only when stable & new."""
        ts = time.monotonic() if now is None else now

        if (
            self._last_posted_key is not None
            and self._note_ttl_seconds > 0
            and self._last_posted_at is not None
            and (ts - self._last_posted_at) >= self._note_ttl_seconds
        ):
            self.clear_posted()

        if track is None or track.status != "Playing" or not track.title:
            self.reset_pending()
            return None

        key = track.identity_key
        if key == self._last_posted_key:
            self.reset_pending()
            return None

        if key != self._pending_key:
            self._pending_key = key
            self._pending_since = ts
            if self._debounce_seconds == 0.0:
                return track
            return None

        if (ts - self._pending_since) >= self._debounce_seconds:
            return track

        return None


class CooldownGuard:
    """Enforce minimum spacing between Music Note writes and backoff on rate limits."""

    _ERROR_BACKOFF_BASE_SECONDS: float = 60.0
    _ERROR_BACKOFF_MAX_SECONDS: float = 600.0

    def __init__(
        self,
        min_post_interval_seconds: float,
        rate_limit_cooldown_seconds: float,
    ) -> None:
        self._min_post_interval: float = max(0.0, float(min_post_interval_seconds))
        self._rate_limit_cooldown: float = max(0.0, float(rate_limit_cooldown_seconds))
        self._last_post_ts: float | None = None
        self._blocked_until_ts: float = 0.0
        self._error_streak: int = 0

    def can_post(self, *, now: float | None = None) -> tuple[bool, float]:
        """Return (allowed, seconds_remaining)."""
        ts = time.monotonic() if now is None else now
        remaining = 0.0

        if ts < self._blocked_until_ts:
            remaining = max(remaining, self._blocked_until_ts - ts)

        if self._last_post_ts is not None:
            elapsed = ts - self._last_post_ts
            if elapsed < self._min_post_interval:
                remaining = max(remaining, self._min_post_interval - elapsed)

        return (remaining <= 0.0, round(remaining, 2))

    def record_post(self, *, now: float | None = None) -> None:
        ts = time.monotonic() if now is None else now
        self._last_post_ts = ts
        self._error_streak = 0

    def record_rate_limit(self, *, now: float | None = None) -> float:
        ts = time.monotonic() if now is None else now
        self._error_streak = 0
        self._blocked_until_ts = max(self._blocked_until_ts, ts + self._rate_limit_cooldown)
        return self._rate_limit_cooldown

    def record_transient_error(self, *, now: float | None = None) -> float:
        """Apply capped exponential backoff after a temporary API error."""
        ts = time.monotonic() if now is None else now
        self._error_streak += 1
        multiplier = 1 << min(10, self._error_streak - 1)
        backoff = min(
            self._ERROR_BACKOFF_MAX_SECONDS,
            self._ERROR_BACKOFF_BASE_SECONDS * multiplier,
        )
        self._blocked_until_ts = max(self._blocked_until_ts, ts + backoff)
        return backoff


def random_jitter(min_seconds: float, max_seconds: float) -> float:
    """Sample a uniform randomized delay in [min_seconds, max_seconds]."""
    low = max(0.0, float(min_seconds))
    high = max(low, float(max_seconds))
    return random.uniform(low, high)
