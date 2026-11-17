"""Tests for MPRIS dbus-fast parsing, debouncer, and cooldown guard."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast

_SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from dbus_fast import Message, MessageType, Variant  # noqa: E402

from mpris_ig.anti_bot import CooldownGuard, TrackDebouncer  # noqa: E402
from mpris_ig.config import MprisConfig  # noqa: E402
from mpris_ig.mpris import (  # noqa: E402
    DbusMprisBackend,
    MprisTrack,
    parse_mpris_properties,
    track_matches_filters,
)


class _FakeMessageBus:
    def __init__(self, responses: dict[str, tuple[str, list[object]]]) -> None:
        self._responses: dict[str, tuple[str, list[object]]] = responses
        self.disconnected: bool = False

    async def call(self, msg: Message) -> Message:
        key = "ListNames" if msg.member == "ListNames" else str(msg.destination)
        sig, body = self._responses[key]
        return Message.new_method_return(
            Message(
                destination=msg.destination,
                path=msg.path,
                interface=msg.interface,
                member=msg.member,
                serial=1,
            ),
            sig,
            body,
        )

    def disconnect(self) -> None:
        self.disconnected = True


class MprisParsingTests(unittest.TestCase):
    def test_parse_mpris_properties_preserves_thai_and_cjk_utf8(self):
        props = {
            "PlaybackStatus": "Playing",
            "Metadata": {
                "xesam:title": "แค่มีเธอ (夜曲)",
                "xesam:artist": ["Three Man Down", "周杰倫"],
                "xesam:album": "อัลบั้มทดสอบ",
                "xesam:url": "https://music.youtube.com/watch?v=abc123",
                "mpris:length": 215000000,
            },
        }

        track = parse_mpris_properties("spotify", props)
        self.assertIsNotNone(track)
        assert track is not None
        self.assertEqual(track.status, "Playing")
        self.assertEqual(track.title, "แค่มีเธอ (夜曲)")
        self.assertEqual(track.artists, ("Three Man Down", "周杰倫"))
        self.assertEqual(track.artist_display, "Three Man Down, 周杰倫")
        self.assertEqual(track.url, "https://music.youtube.com/watch?v=abc123")
        self.assertEqual(track.length_us, 215000000)

    def test_url_and_max_length_filters_and_disable_mode(self):
        logger = logging.getLogger("test_mpris_filters")
        yt_music = MprisTrack(
            "chromium",
            "Playing",
            "แค่มีเธอ",
            ("Three Man Down",),
            length_us=215_000_000,
            url="https://music.youtube.com/watch?v=abc",
        )
        long_mix = MprisTrack(
            "chromium",
            "Playing",
            "1 Hour Lo-Fi Mix",
            ("Channel",),
            length_us=420_000_000,  # exactly 7 minutes (>= 420s -> filtered)
            url="https://music.youtube.com/watch?v=mix",
        )
        regular_yt = MprisTrack(
            "chromium",
            "Playing",
            "Podcast Ep 1",
            ("Host",),
            length_us=180_000_000,
            url="https://www.youtube.com/watch?v=pod",
        )

        strict_cfg = MprisConfig(
            allowed_url_prefixes=("https://music.youtube.com",),
            max_length_seconds=420,
        )
        self.assertTrue(track_matches_filters(yt_music, strict_cfg, logger))
        self.assertFalse(track_matches_filters(long_mix, strict_cfg, logger))
        self.assertFalse(track_matches_filters(regular_yt, strict_cfg, logger))

        disabled_cfg = MprisConfig(allowed_url_prefixes=(), max_length_seconds=0)
        self.assertTrue(track_matches_filters(yt_music, disabled_cfg, logger))
        self.assertTrue(track_matches_filters(long_mix, disabled_cfg, logger))
        self.assertTrue(track_matches_filters(regular_yt, disabled_cfg, logger))

    def test_dbus_backend_filters_ignored_and_picks_playing(self):
        fake_bus = _FakeMessageBus(
            {
                "ListNames": (
                    "as",
                    [
                        [
                            "org.freedesktop.DBus",
                            "org.mpris.MediaPlayer2.kdeconnect.phone",
                            "org.mpris.MediaPlayer2.spotify",
                            "org.mpris.MediaPlayer2.chromium.instance1",
                        ]
                    ],
                ),
                "org.mpris.MediaPlayer2.chromium.instance1": (
                    "a{sv}",
                    [
                        {
                            "PlaybackStatus": Variant("s", "Paused"),
                            "Metadata": Variant(
                                "a{sv}",
                                {"xesam:title": Variant("s", "Paused Video")},
                            ),
                        }
                    ],
                ),
                "org.mpris.MediaPlayer2.spotify": (
                    "a{sv}",
                    [
                        {
                            "PlaybackStatus": Variant("s", "Playing"),
                            "Metadata": Variant(
                                "a{sv}",
                                {
                                    "xesam:title": Variant("s", "晴天"),
                                    "xesam:artist": Variant("as", ["周杰倫"]),
                                },
                            ),
                        }
                    ],
                ),
            }
        )

        async def fake_factory() -> _FakeMessageBus:
            return fake_bus

        cfg = MprisConfig(ignored_players=("kdeconnect",))
        backend = DbusMprisBackend(cfg, logging.getLogger("test_mpris"), bus_factory=fake_factory)
        try:
            active = backend.get_active_track()
        finally:
            backend.close()

        self.assertTrue(fake_bus.disconnected)
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active.player_name, "spotify")
        self.assertEqual(active.title, "晴天")
        self.assertEqual(active.artist_display, "周杰倫")

    def test_dbus_backend_resets_bus_on_timeout_and_reconnects(self):
        class _FlakyBus:
            def __init__(self, fail: bool) -> None:
                self.fail: bool = fail
                self.disconnected: bool = False

            async def call(self, msg: Message) -> Message:
                if self.fail:
                    raise TimeoutError("dbus timed out")
                return Message.new_method_return(
                    Message(
                        destination=msg.destination,
                        path=msg.path,
                        interface=msg.interface,
                        member=msg.member,
                        serial=1,
                    ),
                    "as",
                    [[]],
                )

            def disconnect(self) -> None:
                self.disconnected = True

        buses = [_FlakyBus(fail=True), _FlakyBus(fail=False)]
        created: list[_FlakyBus] = []

        async def factory() -> _FlakyBus:
            bus = buses[len(created)]
            created.append(bus)
            return bus

        backend = DbusMprisBackend(
            MprisConfig(), logging.getLogger("test_flaky"), bus_factory=factory
        )
        try:
            self.assertIsNone(backend.get_active_track())
            self.assertTrue(created[0].disconnected)
            self.assertIsNone(backend.get_active_track())
            self.assertEqual(len(created), 2)
        finally:
            backend.close()

    def test_dbus_backend_whitelist_and_error_reply_fallback(self):
        class _MixedBus:
            async def call(self, msg: Message) -> Message:
                req = Message(
                    destination=msg.destination,
                    path=msg.path,
                    interface=msg.interface,
                    member=msg.member,
                    serial=1,
                )
                if msg.member == "ListNames":
                    return Message.new_method_return(
                        req,
                        "as",
                        [
                            [
                                "org.mpris.MediaPlayer2.chromium",
                                "org.mpris.MediaPlayer2.spotify",
                                "org.mpris.MediaPlayer2.vlc",
                            ]
                        ],
                    )
                if msg.destination == "org.mpris.MediaPlayer2.spotify":
                    return Message.new_error(req, "org.freedesktop.DBus.Error.Failed", "boom")
                return Message.new_method_return(
                    req,
                    "a{sv}",
                    [
                        {
                            "PlaybackStatus": Variant("s", "Paused"),
                            "Metadata": Variant(
                                "a{sv}", {"xesam:title": Variant("s", "VLC Paused")}
                            ),
                        }
                    ],
                )

            def disconnect(self) -> None:
                pass

        async def factory() -> _MixedBus:
            return _MixedBus()

        cfg = MprisConfig(allowed_players=("spotify", "vlc"))
        backend = DbusMprisBackend(cfg, logging.getLogger("test_mixed"), bus_factory=factory)
        try:
            active = backend.get_active_track()
        finally:
            backend.close()

        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active.player_name, "vlc")
        self.assertEqual(active.status, "Paused")
        self.assertEqual(active.title, "VLC Paused")

    def test_dbus_backend_respects_allowed_players_priority_order(self):
        fake_bus = _FakeMessageBus(
            {
                "ListNames": (
                    "as",
                    [
                        [
                            "org.mpris.MediaPlayer2.chromium",
                            "org.mpris.MediaPlayer2.spotify",
                        ]
                    ],
                ),
                "org.mpris.MediaPlayer2.chromium": (
                    "a{sv}",
                    [
                        {
                            "PlaybackStatus": Variant("s", "Playing"),
                            "Metadata": Variant(
                                "a{sv}", {"xesam:title": Variant("s", "Browser Clip")}
                            ),
                        }
                    ],
                ),
                "org.mpris.MediaPlayer2.spotify": (
                    "a{sv}",
                    [
                        {
                            "PlaybackStatus": Variant("s", "Playing"),
                            "Metadata": Variant(
                                "a{sv}", {"xesam:title": Variant("s", "Spotify Song")}
                            ),
                        }
                    ],
                ),
            }
        )

        async def factory() -> _FakeMessageBus:
            return fake_bus

        cfg = MprisConfig(allowed_players=("spotify", "chromium"))
        backend = DbusMprisBackend(cfg, logging.getLogger("test_prio"), bus_factory=factory)
        try:
            active = backend.get_active_track()
        finally:
            backend.close()

        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active.player_name, "spotify")
        self.assertEqual(active.title, "Spotify Song")

    def test_dbus_backend_wakes_immediately_on_relevant_properties_changed_signal(self):
        class _SignalBus:
            def __init__(self) -> None:
                self.handlers: list[Callable[[Message], Message | bool | None]] = []
                self.match_rules: list[str] = []

            def add_message_handler(
                self, handler: Callable[[Message], Message | bool | None]
            ) -> None:
                self.handlers.append(handler)

            async def call(self, msg: Message) -> Message:
                req = Message(
                    destination=msg.destination,
                    path=msg.path,
                    interface=msg.interface,
                    member=msg.member,
                    serial=1,
                )
                if msg.member == "AddMatch":
                    msg_body = cast(list[object], msg.body)
                    self.match_rules.append(str(msg_body[0]))
                    return Message.new_method_return(req, "", [])
                if msg.member == "ListNames":
                    return Message.new_method_return(req, "as", [[]])
                return Message.new_method_return(req, "a{sv}", [{}])

            def emit_signal(self, msg: Message) -> None:
                for handler in self.handlers:
                    handler(msg)

            def disconnect(self) -> None:
                pass

        bus = _SignalBus()

        async def factory() -> _SignalBus:
            return bus

        backend = DbusMprisBackend(
            MprisConfig(), logging.getLogger("test_sig"), bus_factory=factory
        )
        stop_event = threading.Event()
        try:
            self.assertIsNone(backend.get_active_track())
            self.assertEqual(len(bus.match_rules), 2)

            # Irrelevant Position-only PropertiesChanged must NOT wake early.
            pos_sig = Message(
                message_type=MessageType.SIGNAL,
                path="/org/mpris/MediaPlayer2",
                interface="org.freedesktop.DBus.Properties",
                member="PropertiesChanged",
                signature="sa{sv}as",
                body=["org.mpris.MediaPlayer2.Player", {"Position": Variant("x", 1000)}, []],
            )
            bus.emit_signal(pos_sig)

            start = time.monotonic()
            self.assertFalse(backend.wait_for_update(0.05, stop_event))
            self.assertGreaterEqual(time.monotonic() - start, 0.04)

            # Metadata / PlaybackStatus signal wakes wait_for_update immediately.
            track_sig = Message(
                message_type=MessageType.SIGNAL,
                path="/org/mpris/MediaPlayer2",
                interface="org.freedesktop.DBus.Properties",
                member="PropertiesChanged",
                signature="sa{sv}as",
                body=[
                    "org.mpris.MediaPlayer2.Player",
                    {"PlaybackStatus": Variant("s", "Playing")},
                    [],
                ],
            )

            async def emit_soon() -> None:
                await asyncio.sleep(0.01)
                bus.emit_signal(track_sig)

            t0 = time.monotonic()

            async def run_wait_with_signal() -> bool:
                task = asyncio.create_task(emit_soon())
                try:
                    return await backend._wait_for_signal_async(5.0, stop_event)  # pyright: ignore[reportPrivateUsage]
                finally:
                    await task

            woken_by_stop = backend._run(run_wait_with_signal())  # pyright: ignore[reportPrivateUsage]
            self.assertFalse(woken_by_stop)
            self.assertLess(time.monotonic() - t0, 1.0)
        finally:
            backend.close()


class AntiBotTests(unittest.TestCase):
    def test_debouncer_ignores_rapid_skips_and_emits_after_stable_window(self):
        debouncer = TrackDebouncer(debounce_seconds=20.0)
        t1 = MprisTrack("spotify", "Playing", "Song A", ("Artist A",))
        t2 = MprisTrack("spotify", "Playing", "Song B", ("Artist B",))

        # Song A starts at t=0, skipped at t=10 before 20s window expires
        self.assertIsNone(debouncer.observe(t1, now=0.0))
        self.assertIsNone(debouncer.observe(t1, now=10.0))
        self.assertIsNone(debouncer.observe(t2, now=12.0))

        # Song B stays stable until t=32 -> emitted
        self.assertIsNone(debouncer.observe(t2, now=25.0))
        stable = debouncer.observe(t2, now=32.0)
        self.assertEqual(stable, t2)

        # Once marked posted, same track is never re-emitted
        debouncer.mark_posted(t2)
        self.assertIsNone(debouncer.observe(t2, now=60.0))

    def test_debouncer_resets_when_paused(self):
        debouncer = TrackDebouncer(debounce_seconds=15.0)
        playing = MprisTrack("spotify", "Playing", "Song A", ("Artist A",))
        paused = MprisTrack("spotify", "Paused", "Song A", ("Artist A",))

        self.assertIsNone(debouncer.observe(playing, now=0.0))
        self.assertIsNone(debouncer.observe(paused, now=10.0))
        # Resuming restarts the debounce timer
        self.assertIsNone(debouncer.observe(playing, now=16.0))
        self.assertEqual(debouncer.observe(playing, now=31.0), playing)

    def test_debouncer_expires_last_posted_after_24h_note_ttl(self):
        debouncer = TrackDebouncer(debounce_seconds=10.0)
        track = MprisTrack("spotify", "Playing", "Song A", ("Artist A",))

        debouncer.mark_posted(track, now=100.0)
        self.assertIsNone(debouncer.observe(track, now=86_499.0))
        # At >= 86400s later, expired Note allows re-debouncing & re-posting.
        self.assertIsNone(debouncer.observe(track, now=86_500.0))
        self.assertEqual(debouncer.observe(track, now=86_510.0), track)

    def test_cooldown_guard_enforces_post_interval_and_rate_limit_backoff(self):
        guard = CooldownGuard(min_post_interval_seconds=150.0, rate_limit_cooldown_seconds=900.0)
        self.assertEqual(guard.can_post(now=100.0), (True, 0.0))

        guard.record_post(now=100.0)
        allowed, rem = guard.can_post(now=200.0)
        self.assertFalse(allowed)
        self.assertAlmostEqual(rem, 50.0)
        self.assertEqual(guard.can_post(now=250.0), (True, 0.0))

        guard.record_rate_limit(now=300.0)
        allowed_rl, rem_rl = guard.can_post(now=600.0)
        self.assertFalse(allowed_rl)
        self.assertAlmostEqual(rem_rl, 600.0)
        self.assertEqual(guard.can_post(now=1200.0), (True, 0.0))

        # Transient API errors apply exponential backoff (60s -> 120s) and reset on post.
        self.assertEqual(guard.record_transient_error(now=1300.0), 60.0)
        self.assertEqual(guard.can_post(now=1320.0), (False, 40.0))
        self.assertEqual(guard.record_transient_error(now=1360.0), 120.0)
        self.assertEqual(guard.can_post(now=1420.0), (False, 60.0))
        guard.record_post(now=1500.0)
        self.assertEqual(guard.record_transient_error(now=1700.0), 60.0)


if __name__ == "__main__":
    unittest.main()
