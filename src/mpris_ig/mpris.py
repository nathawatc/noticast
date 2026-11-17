"""MPRIS D-Bus reader backend powered by `dbus-fast` with full UTF-8 metadata support."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable

from dbus_fast import DBusError, Message, MessageType, unpack_variants
from dbus_fast.aio import MessageBus

from mpris_ig.config import MprisConfig
from mpris_ig.text_utils import normalize_display

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_OBJECT_PATH = "/org/mpris/MediaPlayer2"
MPRIS_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
DBUS_TIMEOUT_SECONDS = 5.0
_MPRIS_MATCH_RULES = (
    "type='signal',interface='org.freedesktop.DBus.Properties',"
    + f"member='PropertiesChanged',path='{MPRIS_OBJECT_PATH}',arg0='{MPRIS_PLAYER_IFACE}'",
    "type='signal',interface='org.freedesktop.DBus',"
    + f"member='NameOwnerChanged',arg0namespace='{MPRIS_PREFIX.removesuffix('.')}'",
)


class MprisBackendError(RuntimeError):
    """Raised when querying the MPRIS bus fails unexpectedly."""


@dataclass(frozen=True)
class MprisTrack:
    """Normalized metadata for the currently active MPRIS player track."""

    player_name: str
    status: str
    title: str
    artists: tuple[str, ...] = ()
    length_us: int = 0
    url: str = ""

    @property
    def artist_display(self) -> str:
        return ", ".join(self.artists)

    @property
    def identity_key(self) -> tuple[str, str]:
        return (
            normalize_display(self.title),
            normalize_display(self.artist_display),
        )


class _MessageBusProtocol(Protocol):
    async def call(self, msg: Message) -> Message: ...
    def disconnect(self) -> None: ...


@runtime_checkable
class _SignalBusProtocol(Protocol):
    def add_message_handler(self, handler: Callable[[Message], Message | bool | None]) -> None: ...


def _is_relevant_mpris_signal(msg: Message) -> bool:
    if msg.message_type != MessageType.SIGNAL:
        return False
    body = cast(list[object], msg.body or [])
    if (
        msg.interface == "org.freedesktop.DBus.Properties"
        and msg.member == "PropertiesChanged"
        and msg.path == MPRIS_OBJECT_PATH
        and len(body) >= 2
        and body[0] == MPRIS_PLAYER_IFACE
    ):
        changed = cast(dict[object, object], body[1]) if isinstance(body[1], dict) else {}
        invalidated = (
            cast(list[object], body[2]) if len(body) >= 3 and isinstance(body[2], list) else []
        )
        relevant = {"PlaybackStatus", "Metadata"}
        return any(k in changed for k in relevant) or any(k in invalidated for k in relevant)
    if (
        msg.interface == "org.freedesktop.DBus"
        and msg.member == "NameOwnerChanged"
        and len(body) >= 1
        and isinstance(body[0], str)
    ):
        return body[0].startswith(MPRIS_PREFIX)
    return False


def _player_priority(
    service_name: str,
    allowed: tuple[str, ...],
    ignored: tuple[str, ...],
) -> int | None:
    short_name = service_name.removeprefix(MPRIS_PREFIX).casefold()

    if any(token.casefold() in short_name for token in ignored if token):
        return None

    if not allowed:
        return 0

    for idx, token in enumerate(allowed):
        if token and token.casefold() in short_name:
            return idx
    return None


def _extract_artists(meta: Mapping[str, object]) -> tuple[str, ...]:
    raw_val = meta.get("xesam:artist")
    if isinstance(raw_val, list):
        items = cast(list[object], raw_val)
        cleaned = [normalize_display(str(a)) for a in items if str(a).strip()]
        return tuple(a for a in cleaned if a)
    if isinstance(raw_val, str) and raw_val.strip():
        return (normalize_display(raw_val),)
    return ()


def _extract_int(meta: Mapping[str, object], key: str) -> int:
    raw_val = meta.get(key)
    if isinstance(raw_val, bool):
        return int(raw_val)
    if isinstance(raw_val, (int, float, str)):
        try:
            return int(raw_val)
        except ValueError:
            return 0
    return 0


def parse_mpris_properties(player_name: str, props: Mapping[str, object]) -> MprisTrack | None:
    """Parse unpacked `org.mpris.MediaPlayer2.Player` D-Bus properties into MprisTrack."""
    raw_status = props.get("PlaybackStatus")
    status = normalize_display(str(raw_status)) if raw_status is not None else ""

    raw_meta = props.get("Metadata")
    meta: Mapping[str, object] = (
        cast(dict[str, object], raw_meta) if isinstance(raw_meta, dict) else {}
    )

    raw_title = meta.get("xesam:title")
    title = normalize_display(str(raw_title)) if raw_title is not None else ""
    artists = _extract_artists(meta)
    length_us = _extract_int(meta, "mpris:length")
    raw_url = meta.get("xesam:url")
    url = normalize_display(str(raw_url)) if raw_url is not None else ""

    if not status and not title:
        return None

    return MprisTrack(
        player_name=player_name,
        status=status or "Stopped",
        title=title,
        artists=artists,
        length_us=length_us,
        url=url,
    )


def track_matches_filters(
    track: MprisTrack,
    config: MprisConfig,
    logger: logging.Logger,
) -> bool:
    """Check optional xesam:url prefix and mpris:length (< max_length_seconds) filters."""
    if config.allowed_url_prefixes:
        url_cf = track.url.strip().casefold()
        if not any(url_cf.startswith(p.strip().casefold()) for p in config.allowed_url_prefixes):
            logger.debug(
                "[MPRIS] Filtered %s (%s): xesam:url=%r does not match %s",
                track.title,
                track.player_name,
                track.url,
                config.allowed_url_prefixes,
            )
            return False

    if config.max_length_seconds > 0 and track.length_us > 0:
        max_us = config.max_length_seconds * 1_000_000
        if track.length_us >= max_us:
            logger.debug(
                "[MPRIS] Filtered %s (%s): length=%.1fs >= max_length_seconds=%ss",
                track.title,
                track.player_name,
                track.length_us / 1_000_000,
                config.max_length_seconds,
            )
            return False

    return True


async def _default_bus_factory() -> MessageBus:
    return await MessageBus().connect()


class DbusMprisBackend:
    """Native D-Bus MPRIS reader powered by `dbus-fast`."""

    def __init__(
        self,
        config: MprisConfig,
        logger: logging.Logger,
        *,
        bus_factory: Callable[[], Awaitable[_MessageBusProtocol]] = _default_bus_factory,
        timeout_seconds: float = DBUS_TIMEOUT_SECONDS,
    ) -> None:
        self._config: MprisConfig = config
        self._logger: logging.Logger = logger
        self._bus_factory: Callable[[], Awaitable[_MessageBusProtocol]] = bus_factory
        self._timeout_seconds: float = timeout_seconds
        self._runner: asyncio.Runner | None = None
        self._bus: _MessageBusProtocol | None = None
        self._changed_event: asyncio.Event | None = None
        self._signals_subscribed: bool = False

    def _run[T](self, coro: Coroutine[object, object, T]) -> T:
        if self._runner is None:
            self._runner = asyncio.Runner()
        return self._runner.run(coro)

    def _reset_bus(self) -> None:
        self._signals_subscribed = False
        self._changed_event = None
        if self._bus is not None:
            with contextlib.suppress(Exception):
                self._bus.disconnect()
            self._bus = None

    def close(self) -> None:
        """Disconnect the D-Bus session bus and close the internal asyncio runner."""
        self._reset_bus()
        if self._runner is not None:
            self._runner.close()
            self._runner = None

    def _on_dbus_message(self, msg: Message) -> None:
        if _is_relevant_mpris_signal(msg) and self._changed_event is not None:
            self._changed_event.set()

    async def _ensure_bus_async(self) -> _MessageBusProtocol:
        if self._bus is not None:
            return self._bus
        bus = await asyncio.wait_for(
            self._bus_factory(),
            timeout=self._timeout_seconds,
        )
        self._bus = bus
        self._signals_subscribed = False
        self._changed_event = None
        if isinstance(bus, _SignalBusProtocol):
            self._changed_event = asyncio.Event()
            bus.add_message_handler(self._on_dbus_message)
            try:
                for rule in _MPRIS_MATCH_RULES:
                    reply = await asyncio.wait_for(
                        bus.call(
                            Message(
                                destination="org.freedesktop.DBus",
                                path="/org/freedesktop/DBus",
                                interface="org.freedesktop.DBus",
                                member="AddMatch",
                                signature="s",
                                body=[rule],
                            )
                        ),
                        timeout=self._timeout_seconds,
                    )
                    if reply.message_type == MessageType.ERROR:
                        raise MprisBackendError("AddMatch returned D-Bus error")
                self._signals_subscribed = True
            except (TimeoutError, OSError, EOFError, DBusError, MprisBackendError) as exc:
                self._logger.debug(
                    "[MPRIS] PropertiesChanged match failed (using polling fallback): %s",
                    exc,
                )
                self._signals_subscribed = False
        return bus

    async def _call_dbus(
        self,
        *,
        destination: str,
        path: str,
        interface: str,
        member: str,
        signature: str = "",
        body: Sequence[object] | None = None,
    ) -> list[object]:
        try:
            bus = await self._ensure_bus_async()
            msg = Message(
                destination=destination,
                path=path,
                interface=interface,
                member=member,
                signature=signature,
                body=list(body) if body else [],
            )
            reply = await asyncio.wait_for(
                bus.call(msg),
                timeout=self._timeout_seconds,
            )
        except (TimeoutError, OSError, EOFError, DBusError) as exc:
            self._reset_bus()
            raise MprisBackendError(f"D-Bus call {interface}.{member} failed: {exc}") from exc

        reply_body = cast(list[object], reply.body)
        if reply.message_type == MessageType.ERROR:
            err_detail = reply_body[0] if reply_body else "unknown D-Bus error"
            raise MprisBackendError(f"D-Bus call {interface}.{member} returned error: {err_detail}")
        return list(reply_body)

    async def _list_players_async(self) -> list[str]:
        body = await self._call_dbus(
            destination="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus",
            member="ListNames",
        )
        names = cast(list[object], body[0]) if body and isinstance(body[0], list) else []
        matched: list[tuple[int, str]] = []
        for name in names:
            if isinstance(name, str) and name.startswith(MPRIS_PREFIX):
                prio = _player_priority(
                    name,
                    self._config.allowed_players,
                    self._config.ignored_players,
                )
                if prio is not None:
                    matched.append((prio, name))
        matched.sort()
        return [name for _, name in matched]

    async def _read_player_track_async(self, service_name: str) -> MprisTrack | None:
        body = await self._call_dbus(
            destination=service_name,
            path=MPRIS_OBJECT_PATH,
            interface="org.freedesktop.DBus.Properties",
            member="GetAll",
            signature="s",
            body=[MPRIS_PLAYER_IFACE],
        )
        if not body or not isinstance(body[0], dict):
            return None
        unpacked = cast(object, unpack_variants(body[0]))
        if not isinstance(unpacked, dict):
            return None
        short_name = service_name.removeprefix(MPRIS_PREFIX)
        return parse_mpris_properties(short_name, cast(dict[str, object], unpacked))

    async def _get_active_track_async(self) -> MprisTrack | None:
        if self._changed_event is not None:
            self._changed_event.clear()
        try:
            players = await self._list_players_async()
        except MprisBackendError as exc:
            self._logger.debug("[MPRIS] Failed to list D-Bus names: %s", exc)
            return None

        fallback_paused: MprisTrack | None = None
        for service in players:
            try:
                track = await self._read_player_track_async(service)
            except MprisBackendError as exc:
                self._logger.debug("[MPRIS] Skipping %s: %s", service, exc)
                continue
            if track is None or not track_matches_filters(track, self._config, self._logger):
                continue
            if track.status == "Playing" and track.title:
                self._logger.debug(
                    "[MPRIS] Playing from %s: %s — %s",
                    track.player_name,
                    track.title,
                    track.artist_display,
                )
                return track
            if fallback_paused is None and track.title:
                fallback_paused = track

        return fallback_paused

    def get_active_track(self) -> MprisTrack | None:
        return self._run(self._get_active_track_async())

    async def _wait_for_signal_async(
        self, timeout_seconds: float, stop_event: threading.Event
    ) -> bool:
        event = self._changed_event
        if event is None:
            return stop_event.wait(timeout=timeout_seconds)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        waiter = asyncio.create_task(event.wait())
        try:
            while not stop_event.is_set():
                if event.is_set():
                    return False
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return stop_event.is_set()
                done, _ = await asyncio.wait({waiter}, timeout=min(0.25, remaining))
                if waiter in done:
                    return False
            return True
        finally:
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter

    def wait_for_update(self, timeout_seconds: float, stop_event: threading.Event) -> bool:
        """Wait for an MPRIS D-Bus signal or timeout fallback; return True if stop requested."""
        if timeout_seconds <= 0:
            return stop_event.is_set()
        if not self._signals_subscribed or self._bus is None or self._changed_event is None:
            return stop_event.wait(timeout=timeout_seconds)
        return self._run(self._wait_for_signal_async(timeout_seconds, stop_event))
