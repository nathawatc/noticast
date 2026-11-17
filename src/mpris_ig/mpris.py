"""MPRIS D-Bus reader backend powered by `dbus-fast` with full UTF-8 metadata support."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from dbus_fast import DBusError, Message, MessageType, unpack_variants
from dbus_fast.aio import MessageBus

from mpris_ig.config import MprisConfig
from mpris_ig.text_utils import normalize_display

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_OBJECT_PATH = "/org/mpris/MediaPlayer2"
MPRIS_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
DBUS_TIMEOUT_SECONDS = 5.0


class MprisBackendError(RuntimeError):
    """Raised when querying the MPRIS bus fails unexpectedly."""


@dataclass(frozen=True)
class MprisTrack:
    """Normalized metadata for the currently active MPRIS player track."""

    player_name: str
    status: str
    title: str
    artists: tuple[str, ...] = ()
    album: str = ""
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


class MprisBackend(Protocol):
    """Interface for polling currently playing MPRIS tracks."""

    def get_active_track(self) -> MprisTrack | None: ...


def _is_player_eligible(
    service_name: str,
    allowed: tuple[str, ...],
    ignored: tuple[str, ...],
) -> bool:
    short_name = service_name.removeprefix(MPRIS_PREFIX).casefold()

    if any(token.casefold() in short_name for token in ignored if token):
        return False

    if not allowed:
        return True

    return any(token.casefold() in short_name for token in allowed if token)


def _extract_artists(meta: dict[str, Any]) -> tuple[str, ...]:
    raw_val = meta.get("xesam:artist")
    if isinstance(raw_val, list):
        cleaned = [normalize_display(str(a)) for a in raw_val if str(a).strip()]
        return tuple(a for a in cleaned if a)
    if isinstance(raw_val, str) and raw_val.strip():
        return (normalize_display(raw_val),)
    return ()


def _extract_int(meta: dict[str, Any], key: str) -> int:
    raw_val = meta.get(key)
    try:
        return int(raw_val) if raw_val is not None else 0
    except TypeError, ValueError:
        return 0


def parse_mpris_properties(player_name: str, props: dict[str, Any]) -> MprisTrack | None:
    """Parse unpacked `org.mpris.MediaPlayer2.Player` D-Bus properties into MprisTrack."""
    raw_status = props.get("PlaybackStatus")
    status = normalize_display(str(raw_status)) if raw_status is not None else ""

    raw_meta = props.get("Metadata")
    meta = raw_meta if isinstance(raw_meta, dict) else {}

    raw_title = meta.get("xesam:title")
    title = normalize_display(str(raw_title)) if raw_title is not None else ""
    artists = _extract_artists(meta)
    raw_album = meta.get("xesam:album")
    album = normalize_display(str(raw_album)) if raw_album is not None else ""
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
        album=album,
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
        bus_factory: Callable[[], Awaitable[Any]] = _default_bus_factory,
        timeout_seconds: float = DBUS_TIMEOUT_SECONDS,
    ) -> None:
        self._config = config
        self._logger = logger
        self._bus_factory = bus_factory
        self._timeout_seconds = timeout_seconds
        self._runner: asyncio.Runner | None = None
        self._bus: Any = None

    def _run(self, coro: Awaitable[Any]) -> Any:
        if self._runner is None:
            self._runner = asyncio.Runner()
        return self._runner.run(coro)  # type: ignore[arg-type]

    def _reset_bus(self) -> None:
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

    async def _call_dbus(
        self,
        *,
        destination: str,
        path: str,
        interface: str,
        member: str,
        signature: str = "",
        body: list[Any] | None = None,
    ) -> list[Any]:
        try:
            if self._bus is None:
                self._bus = await asyncio.wait_for(
                    self._bus_factory(),
                    timeout=self._timeout_seconds,
                )
            msg = Message(
                destination=destination,
                path=path,
                interface=interface,
                member=member,
                signature=signature,
                body=body or [],
            )
            reply = await asyncio.wait_for(
                self._bus.call(msg),
                timeout=self._timeout_seconds,
            )
        except (TimeoutError, OSError, EOFError, DBusError) as exc:
            self._reset_bus()
            raise MprisBackendError(f"D-Bus call {interface}.{member} failed: {exc}") from exc

        if reply.message_type == MessageType.ERROR:
            err_detail = reply.body[0] if reply.body else "unknown D-Bus error"
            raise MprisBackendError(f"D-Bus call {interface}.{member} returned error: {err_detail}")
        return list(reply.body)

    async def _list_players_async(self) -> list[str]:
        body = await self._call_dbus(
            destination="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus",
            member="ListNames",
        )
        names = body[0] if body and isinstance(body[0], list) else []
        players = [
            str(name)
            for name in names
            if isinstance(name, str)
            and name.startswith(MPRIS_PREFIX)
            and _is_player_eligible(
                name,
                self._config.allowed_players,
                self._config.ignored_players,
            )
        ]
        return sorted(players)

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
        unpacked = unpack_variants(body[0])
        short_name = service_name.removeprefix(MPRIS_PREFIX)
        return parse_mpris_properties(short_name, unpacked)

    async def _get_active_track_async(self) -> MprisTrack | None:
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

    def list_players(self) -> list[str]:
        return list(self._run(self._list_players_async()))

    def read_player_track(self, service_name: str) -> MprisTrack | None:
        return self._run(self._read_player_track_async(service_name))

    def get_active_track(self) -> MprisTrack | None:
        return self._run(self._get_active_track_async())


def create_mpris_backend(config: MprisConfig, logger: logging.Logger) -> MprisBackend:
    """Select and instantiate the configured MPRIS backend."""
    return DbusMprisBackend(config, logger)
