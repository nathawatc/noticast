"""MPRIS D-Bus reader backends with full UTF-8 / Thai / CJK metadata support."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Protocol

from mpris_ig.config import MprisConfig
from mpris_ig.text_utils import normalize_display

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_OBJECT_PATH = "/org/mpris/MediaPlayer2"
MPRIS_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"


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
    full_name = service_name.casefold()

    if any(token.casefold() in short_name or token.casefold() in full_name for token in ignored):
        return False

    if not allowed:
        return True

    return any(token.casefold() in short_name or token.casefold() in full_name for token in allowed)


def _extract_busctl_artists(meta: dict[str, Any]) -> tuple[str, ...]:
    artist_entry = meta.get("xesam:artist")
    if not isinstance(artist_entry, dict):
        return ()
    raw_val = artist_entry.get("data")
    if isinstance(raw_val, list):
        cleaned = [normalize_display(str(a)) for a in raw_val if str(a).strip()]
        return tuple(a for a in cleaned if a)
    if isinstance(raw_val, str) and raw_val.strip():
        return (normalize_display(raw_val),)
    return ()


def _extract_busctl_str(meta: dict[str, Any], key: str) -> str:
    entry = meta.get(key)
    if not isinstance(entry, dict):
        return ""
    raw_val = entry.get("data")
    if raw_val is None:
        return ""
    return normalize_display(str(raw_val))


def _extract_busctl_int(meta: dict[str, Any], key: str) -> int:
    entry = meta.get(key)
    if not isinstance(entry, dict):
        return 0
    raw_val = entry.get("data")
    try:
        return int(raw_val) if raw_val is not None else 0
    except TypeError, ValueError:
        return 0


def parse_busctl_properties(player_name: str, payload: dict[str, Any]) -> MprisTrack | None:
    """Parse JSON from `busctl --user --json=short call ... Properties.GetAll`."""
    data = payload.get("data")
    if isinstance(data, list):
        props = data[0] if data and isinstance(data[0], dict) else {}
    elif isinstance(data, dict):
        props = data
    else:
        return None

    status_entry = props.get("PlaybackStatus")
    status = (
        normalize_display(str(status_entry.get("data", "")))
        if isinstance(status_entry, dict)
        else ""
    )

    meta_entry = props.get("Metadata")
    meta = meta_entry.get("data") if isinstance(meta_entry, dict) else {}
    if not isinstance(meta, dict):
        meta = {}

    title = _extract_busctl_str(meta, "xesam:title")
    artists = _extract_busctl_artists(meta)
    album = _extract_busctl_str(meta, "xesam:album")
    length_us = _extract_busctl_int(meta, "mpris:length")
    url = _extract_busctl_str(meta, "xesam:url")

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


class BusctlMprisBackend:
    """Zero-dependency MPRIS reader powered by systemd `busctl --user --json=short`."""

    def __init__(
        self,
        config: MprisConfig,
        logger: logging.Logger,
        *,
        runner: Any = subprocess.run,
    ) -> None:
        self._config = config
        self._logger = logger
        self._runner = runner

    def _run_busctl_json(self, args: list[str]) -> dict[str, Any]:
        cmd = ["busctl", "--user", "--json=short", *args]
        proc = self._runner(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5.0,
            check=False,
        )
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            raise MprisBackendError(f"busctl failed ({proc.returncode}): {stderr}")
        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise MprisBackendError(f"Invalid JSON from busctl: {exc}") from exc
        if not isinstance(parsed, dict):
            raise MprisBackendError("Unexpected non-object JSON from busctl.")
        return parsed

    def list_players(self) -> list[str]:
        payload = self._run_busctl_json(
            [
                "call",
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "ListNames",
            ]
        )
        data = payload.get("data")
        names = data[0] if isinstance(data, list) and data and isinstance(data[0], list) else []
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

    def read_player_track(self, service_name: str) -> MprisTrack | None:
        payload = self._run_busctl_json(
            [
                "call",
                service_name,
                MPRIS_OBJECT_PATH,
                "org.freedesktop.DBus.Properties",
                "GetAll",
                "s",
                MPRIS_PLAYER_IFACE,
            ]
        )
        short_name = service_name.removeprefix(MPRIS_PREFIX)
        return parse_busctl_properties(short_name, payload)

    def get_active_track(self) -> MprisTrack | None:
        try:
            players = self.list_players()
        except MprisBackendError as exc:
            self._logger.debug("[MPRIS] Failed to list D-Bus names: %s", exc)
            return None

        fallback_paused: MprisTrack | None = None
        for service in players:
            try:
                track = self.read_player_track(service)
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


class PlayerctlMprisBackend:
    """Optional MPRIS backend using `playerctl` CLI if installed."""

    def __init__(
        self,
        config: MprisConfig,
        logger: logging.Logger,
        *,
        runner: Any = subprocess.run,
    ) -> None:
        self._config = config
        self._logger = logger
        self._runner = runner

    def get_active_track(self) -> MprisTrack | None:
        sep = "\x1f"
        fmt = sep.join(
            [
                "{{playerName}}",
                "{{status}}",
                "{{title}}",
                "{{artist}}",
                "{{album}}",
                "{{xesam:url}}",
                "{{mpris:length}}",
            ]
        )
        cmd = ["playerctl", "--all-players", "metadata", "--format", fmt]
        proc = self._runner(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5.0,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None

        fallback: MprisTrack | None = None
        for line in proc.stdout.splitlines():
            parts = line.split(sep)
            if len(parts) != 7:
                continue
            player, status, title, artist, album, url, length_raw = (
                normalize_display(p) for p in parts
            )
            try:
                length_us = int(length_raw) if length_raw else 0
            except ValueError:
                length_us = 0
            if not _is_player_eligible(
                f"{MPRIS_PREFIX}{player}",
                self._config.allowed_players,
                self._config.ignored_players,
            ):
                continue
            track = MprisTrack(
                player_name=player,
                status=status,
                title=title,
                artists=(artist,) if artist else (),
                album=album,
                length_us=length_us,
                url=url,
            )
            if not track_matches_filters(track, self._config, self._logger):
                continue
            if status == "Playing" and title:
                return track
            if fallback is None and title:
                fallback = track
        return fallback


def create_mpris_backend(config: MprisConfig, logger: logging.Logger) -> MprisBackend:
    """Select and instantiate the configured MPRIS backend."""
    choice = config.backend
    if choice == "playerctl":
        if not shutil.which("playerctl"):
            raise MprisBackendError(
                "mpris.backend='playerctl' requested, but `playerctl` is not installed."
            )
        return PlayerctlMprisBackend(config, logger)

    if choice == "busctl":
        if not shutil.which("busctl"):
            raise MprisBackendError(
                "mpris.backend='busctl' requested, but `busctl` is not installed."
            )
        return BusctlMprisBackend(config, logger)

    # "auto": prefer busctl (native systemd JSON, handles Thai/CJK cleanly), else playerctl
    if shutil.which("busctl"):
        return BusctlMprisBackend(config, logger)
    if shutil.which("playerctl"):
        return PlayerctlMprisBackend(config, logger)
    raise MprisBackendError("No MPRIS backend found. Install systemd `busctl` or `playerctl`.")
