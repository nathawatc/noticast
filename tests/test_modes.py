"""Tests for setup/logout, manual mode, daemon mode, and CLI dispatch."""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

_SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

import requests  # noqa: E402
from instagrapi.exceptions import LoginRequired, PleaseWaitFewMinutes, TwoFactorRequired  # noqa: E402
from instagrapi.mixins.note import NoteAudience  # noqa: E402
from instagrapi.types import Note, Track, UserShort  # noqa: E402

from mpris_ig.cli import main as cli_main  # noqa: E402
from mpris_ig.config import (  # noqa: E402
    AntiBotConfig,
    AppConfig,
    MprisConfig,
    NoteConfig,
    PathsConfig,
)
from mpris_ig.modes.daemon import DaemonRunner  # noqa: E402
from mpris_ig.modes.manual import run_manual  # noqa: E402
from mpris_ig.mpris import MprisTrack  # noqa: E402
from mpris_ig.notes_service import InstagramNotesService, NotesApiError  # noqa: E402
from mpris_ig.session import (  # noqa: E402
    SessionNotReadyError,
    authenticate_and_save,
    load_authenticated_client,
    logout_session,
)
from mpris_ig.state import NoteState, load_note_state, save_note_state  # noqa: E402


def _track(title: str = "แค่มีเธอ", artist: str = "Three Man Down", idx: int = 1) -> Track:
    return Track(
        id=f"100{idx}",
        title=title,
        subtitle="",
        display_artist=artist,
        audio_cluster_id=2000 + idx,
        highlight_start_times_in_ms=[15000],
        is_explicit=False,
        dash_manifest="<MPD/>",
        has_lyrics=False,
        audio_asset_id=3000 + idx,
        duration_in_ms=180000,
        allows_saving=True,
        territory_validity_periods={},
    )


def _note(note_id: str = "999", user_id: str = "123", note_style: int = 1) -> Note:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Note(
        id=note_id,
        text="",
        user_id=user_id,
        user=UserShort(pk=user_id, username="tester"),
        audience=0,
        created_at=now,
        expires_at=now,
        is_emoji_only=False,
        has_translation=False,
        note_style=note_style,
    )


class SessionFlowTests(unittest.TestCase):
    def test_2fa_login_and_logout_flow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_path = Path(tmpdir) / "session.json"
            cl = mock.MagicMock()
            cl.user_id = 123
            cl.login.side_effect = [TwoFactorRequired("2FA"), True]
            cl.logout.return_value = True

            def fake_dump(path: Path) -> bool:
                Path(path).write_text("{}", encoding="utf-8")
                return True

            cl.dump_settings.side_effect = fake_dump

            saved = authenticate_and_save(
                cl, "alice", "secret", session_path, input_fn=lambda _: "123456"
            )
            self.assertTrue(saved.exists())

            cfg = AppConfig(paths=PathsConfig(session_file=session_path))
            rc = logout_session(cfg, client_factory=lambda: cl)
            self.assertEqual(rc, 0)
            cl.logout.assert_called_once_with()
            self.assertFalse(session_path.exists())

    def test_corrupted_session_and_state_files_fail_safely(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            session_path = tmp / "session.json"
            session_path.write_text("{broken json", encoding="utf-8")
            cfg = AppConfig(paths=PathsConfig(session_file=session_path))

            with self.assertRaises(SessionNotReadyError):
                load_authenticated_client(cfg)

            state_path = tmp / "state.json"
            state_path.write_text('{"note_id": ["not-a-string"]}', encoding="utf-8")
            self.assertIsNone(load_note_state(state_path))

            save_note_state(state_path, NoteState(note_id="42", mpris_title="Song"))
            loaded = load_note_state(state_path)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.note_id, "42")

    def test_notes_service_wraps_timeout_and_assertion_errors(self):
        cl = mock.MagicMock()
        cl.search_music.side_effect = requests.exceptions.ReadTimeout("timed out")
        service = InstagramNotesService(cl, logging.getLogger("test_svc_err"))
        with self.assertRaises(NotesApiError):
            service.search_tracks("hello")

        cl.notes_music_browser.return_value = {}
        cl.create_music_note.side_effect = AssertionError("alacorn_session_id is required")
        with self.assertRaises(NotesApiError):
            service.post_music_note(_track())


class ManualModeTests(unittest.TestCase):
    def test_manual_mode_overwrites_directly_by_default(self):
        cl = mock.MagicMock()
        cl.user_id = 123
        tracks = [_track("แค่มีเธอ", "Three Man Down", 1), _track("เพลงอื่น", "Other", 2)]
        cl.search_music.return_value = tracks
        cl.notes_music_browser.return_value = {"status": "ok", "alacorn_session_id": "al-1"}
        cl.create_music_note.return_value = _note("777", "123", 1)

        with tempfile.TemporaryDirectory() as tmpdir:
            logger = logging.getLogger("test_manual")
            cfg = AppConfig(paths=PathsConfig(state_file=Path(tmpdir) / "state.json"))
            answers = iter(["แค่มีเธอ", "1", "ฟังชิลๆ", "1"])

            rc = run_manual(
                cfg,
                logger,
                input_fn=lambda _: next(answers),
                service_factory=lambda _c, lgr: InstagramNotesService(cl, lgr),
            )
            self.assertEqual(rc, 0)
            self.assertTrue(cfg.paths.state_file.exists())
        cl.get_notes.assert_not_called()
        cl.delete_note.assert_not_called()
        cl.create_music_note.assert_called_once_with(
            track=tracks[0],
            text="ฟังชิลๆ",
            audience=NoteAudience.CLOSE_FRIENDS,
        )

    def test_manual_mode_deletes_first_when_configured(self):
        cl = mock.MagicMock()
        cl.user_id = 123
        tracks = [_track("แค่มีเธอ", "Three Man Down", 1)]
        cl.search_music.return_value = tracks
        cl.get_notes.return_value = [_note("555", "123", 0)]
        cl.delete_note.return_value = True
        cl.notes_music_browser.return_value = {"status": "ok", "alacorn_session_id": "al-1"}
        cl.create_music_note.return_value = _note("777", "123", 1)

        with tempfile.TemporaryDirectory() as tmpdir:
            logger = logging.getLogger("test_manual_del")
            cfg = AppConfig(
                paths=PathsConfig(state_file=Path(tmpdir) / "state.json"),
                note=NoteConfig(delete_before_post=True),
            )
            answers = iter(["แค่มีเธอ", "1", "", ""])

            rc = run_manual(
                cfg,
                logger,
                input_fn=lambda _: next(answers),
                service_factory=lambda _c, lgr: InstagramNotesService(cl, lgr),
            )
            self.assertEqual(rc, 0)
        cl.delete_note.assert_called_once_with(555)
        cl.create_music_note.assert_called_once_with(
            track=tracks[0],
            text="",
            audience=NoteAudience.MUTUAL_FOLLOWERS,
        )


class DaemonModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_file = Path(self._tmpdir.name) / "state.json"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _build_daemon_runner(
        self,
        *,
        backend: mock.MagicMock,
        cl: mock.MagicMock,
        dry_run: bool = False,
        clear_on_stop: bool = False,
        check_existing_on_startup: bool = False,
        clock_values: list[float] | None = None,
    ) -> DaemonRunner:
        cfg = AppConfig(
            paths=PathsConfig(state_file=self.state_file),
            mpris=MprisConfig(poll_interval_seconds=1.0),
            anti_bot=AntiBotConfig(
                debounce_seconds=10.0,
                jitter_min_seconds=0.0,
                jitter_max_seconds=0.0,
                min_post_interval_seconds=120.0,
                rate_limit_cooldown_seconds=600.0,
            ),
            note=NoteConfig(
                audience="mutual_followers",
                text_template="🎧 {title}",
                clear_on_stop=clear_on_stop,
                check_existing_on_startup=check_existing_on_startup,
            ),
        )
        logger = logging.getLogger("test_daemon")
        service = InstagramNotesService(cl, logger)
        times = iter(clock_values or [0.0, 5.0, 11.0, 11.0, 11.0, 11.0])
        return DaemonRunner(
            cfg,
            logger,
            backend=backend,
            service=service,
            dry_run=dry_run,
            clock_fn=lambda: next(times),
            jitter_fn=lambda _a, _b: 0.0,
        )

    def test_daemon_posts_after_debounce_and_keeps_note_on_pause(self):
        track_mpris = MprisTrack("spotify", "Playing", "夜曲", ("周杰倫",))
        paused_mpris = MprisTrack("spotify", "Paused", "夜曲", ("周杰倫",))

        backend = mock.MagicMock()
        backend.get_active_track.side_effect = [
            track_mpris,  # t=0: start debounce
            track_mpris,  # t=11: debounce passes (>=10s)
            track_mpris,  # re-confirm after jitter
            paused_mpris,  # paused later -> clear_on_stop is False
        ]

        ig_track = _track("夜曲", "周杰倫", 1)
        cl = mock.MagicMock()
        cl.user_id = 123
        cl.search_music.return_value = [ig_track]
        cl.get_notes.return_value = []
        cl.notes_music_browser.return_value = {"status": "ok", "alacorn_session_id": "al-2"}
        cl.create_music_note.return_value = _note("888", "123", 1)

        runner = self._build_daemon_runner(
            backend=backend,
            cl=cl,
            clear_on_stop=False,
            clock_values=[0.0, 11.0, 11.0, 11.0, 11.0, 30.0],
        )

        self.assertIsNone(runner.step())
        cl.create_music_note.assert_not_called()

        self.assertIsNone(runner.step())
        cl.create_music_note.assert_called_once_with(
            track=ig_track,
            text="🎧 夜曲",
            audience=NoteAudience.MUTUAL_FOLLOWERS,
        )

        # Pausing must NOT delete the note when clear_on_stop=False
        self.assertIsNone(runner.step())
        cl.delete_note.assert_not_called()

    def test_daemon_stops_safely_on_login_required(self):
        track_mpris = MprisTrack("spotify", "Playing", "Song", ("Artist",))
        backend = mock.MagicMock()
        backend.get_active_track.return_value = track_mpris

        cl = mock.MagicMock()
        cl.user_id = 123
        cl.search_music.side_effect = LoginRequired("login_required")

        runner = self._build_daemon_runner(backend=backend, cl=cl, clock_values=[0.0, 15.0, 15.0])
        self.assertIsNone(runner.step())
        self.assertEqual(runner.step(), 1)
        cl.login.assert_not_called()

    def test_daemon_enters_cooldown_on_rate_limit(self):
        track_mpris = MprisTrack("spotify", "Playing", "Song", ("Artist",))
        backend = mock.MagicMock()
        backend.get_active_track.return_value = track_mpris

        cl = mock.MagicMock()
        cl.user_id = 123
        cl.search_music.side_effect = PleaseWaitFewMinutes("Please wait")

        runner = self._build_daemon_runner(
            backend=backend, cl=cl, clock_values=[0.0, 15.0, 15.0, 15.0, 30.0]
        )
        self.assertIsNone(runner.step())
        # Rate limit caught -> returns None (continues safely in cooldown, does not crash)
        self.assertIsNone(runner.step())
        self.assertEqual(cl.search_music.call_count, 1)
        # Next step while in cooldown does not call Instagram
        self.assertIsNone(runner.step())
        self.assertEqual(cl.search_music.call_count, 1)

    def test_startup_skips_when_remote_music_note_matches(self):
        track_mpris = MprisTrack("spotify", "Playing", "แค่มีเธอ", ("Three Man Down",))
        backend = mock.MagicMock()
        backend.get_active_track.return_value = track_mpris

        cl = mock.MagicMock()
        cl.user_id = 123
        cl.get_notes.return_value = [_note("901", "123", 1)]
        cl.last_json = {
            "data": {
                "xdt_get_inbox_tray_items": {
                    "inbox_tray_items": [
                        {
                            "inbox_tray_item_id": "901",
                            "note_dict": {
                                "author_id": "123",
                                "note_style": 1,
                                "note_response_info": {
                                    "music_note_response_info": {
                                        "audio_info": {
                                            "music_asset_info": {
                                                "audio_cluster_id": "2001",
                                                "audio_asset_id": "3001",
                                                "title": "แค่มีเธอ",
                                                "display_artist": "Three Man Down",
                                            }
                                        }
                                    }
                                },
                            },
                        }
                    ]
                }
            }
        }

        runner = self._build_daemon_runner(
            backend=backend,
            cl=cl,
            check_existing_on_startup=True,
            clock_values=[0.0, 11.0, 20.0],
        )
        self.assertIsNone(runner.step())
        self.assertIsNone(runner.step())
        cl.get_notes.assert_called_once_with()
        cl.search_music.assert_not_called()
        cl.create_music_note.assert_not_called()

    def test_startup_skips_when_cluster_id_matches(self):
        track_mpris = MprisTrack("spotify", "Playing", "夜曲", ("周杰倫",))
        backend = mock.MagicMock()
        backend.get_active_track.return_value = track_mpris

        ig_track = _track("夜曲", "周杰倫", 1)
        cl = mock.MagicMock()
        cl.user_id = 123
        cl.get_notes.return_value = [_note("902", "123", 1)]
        cl.last_json = {
            "data": {
                "xdt_get_inbox_tray_items": {
                    "inbox_tray_items": [
                        {
                            "inbox_tray_item_id": "902",
                            "note_dict": {
                                "author_id": "123",
                                "note_style": 1,
                                "note_response_info": {
                                    "music_note_response_info": {
                                        "audio_cluster_id": str(ig_track.audio_cluster_id),
                                    }
                                },
                            },
                        }
                    ]
                }
            }
        }
        cl.search_music.return_value = [ig_track]

        runner = self._build_daemon_runner(
            backend=backend,
            cl=cl,
            check_existing_on_startup=True,
            clock_values=[0.0, 11.0, 11.0],
        )
        self.assertIsNone(runner.step())
        self.assertIsNone(runner.step())
        cl.search_music.assert_called_once()
        cl.create_music_note.assert_not_called()

    def test_startup_skips_when_saved_state_note_id_matches(self):
        save_note_state(
            self.state_file,
            NoteState(
                note_id="903",
                mpris_title="夜曲",
                mpris_artist="周杰倫",
                ig_title="夜曲",
                ig_artist="周杰倫",
                audio_cluster_id="2001",
            ),
        )
        track_mpris = MprisTrack("spotify", "Playing", "夜曲", ("周杰倫",))
        backend = mock.MagicMock()
        backend.get_active_track.return_value = track_mpris

        cl = mock.MagicMock()
        cl.user_id = 123
        cl.get_notes.return_value = [_note("903", "123", 1)]
        cl.last_json = {}

        runner = self._build_daemon_runner(
            backend=backend,
            cl=cl,
            check_existing_on_startup=True,
            clock_values=[0.0, 11.0],
        )
        self.assertIsNone(runner.step())
        self.assertIsNone(runner.step())
        cl.search_music.assert_not_called()
        cl.create_music_note.assert_not_called()

    def test_clear_on_stop_handles_api_errors_and_ignores_unmatched_tracks(self):
        playing = MprisTrack("spotify", "Playing", "Song", ("Artist",))
        paused = MprisTrack("spotify", "Paused", "Song", ("Artist",))
        backend = mock.MagicMock()
        backend.get_active_track.side_effect = [
            playing,
            playing,
            playing,
            paused,  # first pause: deletes note (hits RateLimitedError cleanly)
            paused,  # second pause: still in cooldown
        ]

        ig_track = _track("Song", "Artist", 1)
        cl = mock.MagicMock()
        cl.user_id = 123
        cl.search_music.return_value = [ig_track]
        cl.notes_music_browser.return_value = {"alacorn_session_id": "al-3"}
        cl.create_music_note.return_value = _note("701", "123", 1)
        cl.get_notes.side_effect = PleaseWaitFewMinutes("rate limited")

        runner = self._build_daemon_runner(
            backend=backend,
            cl=cl,
            clear_on_stop=True,
            clock_values=[0.0, 11.0, 11.0, 150.0, 150.0, 160.0],
        )
        self.assertIsNone(runner.step())  # start debounce
        self.assertIsNone(runner.step())  # posts note
        # Pause hits RateLimitedError during clear_on_stop -> caught, enters cooldown, returns None
        self.assertIsNone(runner.step())
        self.assertEqual(cl.get_notes.call_count, 1)
        # Next pause is blocked by cooldown
        self.assertIsNone(runner.step())
        self.assertEqual(cl.get_notes.call_count, 1)

        # Unmatched track must NOT trigger clear_on_stop when paused
        backend2 = mock.MagicMock()
        backend2.get_active_track.side_effect = [playing, playing, playing, paused]
        cl2 = mock.MagicMock()
        cl2.user_id = 123
        cl2.search_music.return_value = []
        runner2 = self._build_daemon_runner(
            backend=backend2,
            cl=cl2,
            clear_on_stop=True,
            clock_values=[0.0, 11.0, 11.0, 20.0],
        )
        self.assertIsNone(runner2.step())
        self.assertIsNone(runner2.step())
        self.assertIsNone(runner2.step())
        cl2.get_notes.assert_not_called()

    def test_startup_short_note_title_does_not_skip_unrelated_song(self):
        track_mpris = MprisTrack("spotify", "Playing", "Imagine", ("John Lennon",))
        backend = mock.MagicMock()
        backend.get_active_track.return_value = track_mpris

        ig_track = _track("Imagine", "John Lennon", 1)
        cl = mock.MagicMock()
        cl.user_id = 123
        cl.get_notes.return_value = [_note("904", "123", 1)]
        cl.last_json = {
            "data": {
                "xdt_get_inbox_tray_items": {
                    "inbox_tray_items": [
                        {
                            "inbox_tray_item_id": "904",
                            "note_dict": {
                                "author_id": "123",
                                "note_style": 1,
                                "note_response_info": {
                                    "music_note_response_info": {
                                        "audio_info": {
                                            "music_asset_info": {
                                                "audio_cluster_id": "9999",
                                                "title": "I",
                                                "display_artist": "Other",
                                            }
                                        }
                                    }
                                },
                            },
                        }
                    ]
                }
            }
        }
        cl.search_music.return_value = [ig_track]
        cl.create_music_note.return_value = _note("905", "123", 1)

        runner = self._build_daemon_runner(
            backend=backend,
            cl=cl,
            check_existing_on_startup=True,
            clock_values=[0.0, 11.0, 11.0, 11.0],
        )
        self.assertIsNone(runner.step())
        self.assertIsNone(runner.step())
        cl.create_music_note.assert_called_once()

    def test_daemon_skips_when_paused_during_jitter_and_stops_on_event(self):
        playing = MprisTrack("spotify", "Playing", "Song", ("Artist",))
        paused = MprisTrack("spotify", "Paused", "Song", ("Artist",))
        backend = mock.MagicMock()
        backend.get_active_track.side_effect = [playing, playing, paused]
        cl = mock.MagicMock()
        cl.user_id = 123

        runner = self._build_daemon_runner(
            backend=backend,
            cl=cl,
            clock_values=[0.0, 11.0, 11.0],
        )
        self.assertIsNone(runner.step())
        self.assertIsNone(runner.step())
        cl.search_music.assert_not_called()

        import threading

        stop = threading.Event()
        stop.set()
        cfg = AppConfig(
            paths=PathsConfig(state_file=self.state_file),
            note=NoteConfig(check_existing_on_startup=False),
        )
        loop_runner = DaemonRunner(
            cfg,
            logging.getLogger("test_stop"),
            backend=backend,
            service=InstagramNotesService(cl, logging.getLogger("test_stop")),
            stop_event=stop,
        )
        self.assertEqual(loop_runner.run(), 0)

    def test_cli_verbose_flag_and_once_dry_run_and_invalid_overrides(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            session_path = tmp / "session.json"
            state_path = tmp / "state.json"
            session_path.write_text(
                json.dumps({"authorization_data": {"ds_user_id": "123"}}),
                encoding="utf-8",
            )
            cfg_path = tmp / "config.toml"
            cfg_path.write_text(
                f'[paths]\nsession_file = "{session_path}"\n'
                f'state_file = "{state_path}"\n'
                "[note]\ncheck_existing_on_startup = false\n"
                "[logging]\nverbose = false\n",
                encoding="utf-8",
            )
            mock_backend = mock.MagicMock()
            mock_backend.get_active_track.return_value = None

            with mock.patch("mpris_ig.modes.daemon.DbusMprisBackend", return_value=mock_backend):
                rc = cli_main(
                    ["--config", str(cfg_path), "--verbose", "daemon", "--dry-run", "--once"]
                )
            self.assertEqual(rc, 0)
            self.assertEqual(
                cli_main(["--config", str(cfg_path), "daemon", "--poll-interval", "0"]), 1
            )
            self.assertEqual(cli_main(["--config", str(cfg_path), "daemon", "--debounce", "-1"]), 1)


if __name__ == "__main__":
    unittest.main()
