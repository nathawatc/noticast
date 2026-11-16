"""Typed TOML configuration loader and validation."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from instagrapi.mixins.note import NoteAudience

DEFAULT_CONFIG_FILE = Path("config.toml")


class ConfigError(ValueError):
    """Raised when configuration file parsing or validation fails."""


@dataclass(frozen=True)
class PathsConfig:
    session_file: Path = Path("session.json")
    state_file: Path = Path(".note_state.json")


@dataclass(frozen=True)
class DeviceConfig:
    android_version: int = 34
    android_release: str = "14"
    dpi: str = "480dpi"
    resolution: str = "1344x2992"
    manufacturer: str = "Google/google"
    device: str = "husky"
    model: str = "Pixel 8 Pro"
    cpu: str = "husky"

    def to_instagrapi_dict(self) -> dict[str, Any]:
        """Return the 8-field device dict expected by instagrapi.Client.set_device."""
        return {
            "android_version": self.android_version,
            "android_release": self.android_release,
            "dpi": self.dpi,
            "resolution": self.resolution,
            "manufacturer": self.manufacturer,
            "device": self.device,
            "model": self.model,
            "cpu": self.cpu,
        }


@dataclass(frozen=True)
class RegionConfig:
    locale: str = "en_US"
    country: str = "US"
    country_code: int = 1
    timezone_offset: int = 0
    timezone_name: str = "UTC"


@dataclass(frozen=True)
class MprisConfig:
    backend: str = "auto"
    poll_interval_seconds: float = 5.0
    allowed_players: tuple[str, ...] = ()
    ignored_players: tuple[str, ...] = ("kdeconnect", "gsconnect")
    allowed_url_prefixes: tuple[str, ...] = ()
    max_length_seconds: int = 0


@dataclass(frozen=True)
class AntiBotConfig:
    debounce_seconds: float = 20.0
    jitter_min_seconds: float = 3.0
    jitter_max_seconds: float = 10.0
    min_post_interval_seconds: float = 150.0
    rate_limit_cooldown_seconds: float = 900.0
    request_delay_range: tuple[float, float] = (1.5, 4.0)


@dataclass(frozen=True)
class NoteConfig:
    audience: str = "mutual_followers"
    text_template: str = ""
    clear_on_stop: bool = False
    delete_before_post: bool = False
    check_existing_on_startup: bool = True
    dry_run: bool = False

    @property
    def audience_enum(self) -> NoteAudience:
        """Map string config value to instagrapi NoteAudience enum."""
        mapping = {
            "mutual_followers": NoteAudience.MUTUAL_FOLLOWERS,
            "close_friends": NoteAudience.CLOSE_FRIENDS,
        }
        return mapping[self.audience]


@dataclass(frozen=True)
class LoggingConfig:
    verbose: bool = False


@dataclass(frozen=True)
class AppConfig:
    paths: PathsConfig = field(default_factory=PathsConfig)
    device: DeviceConfig = field(default_factory=DeviceConfig)
    region: RegionConfig = field(default_factory=RegionConfig)
    mpris: MprisConfig = field(default_factory=MprisConfig)
    anti_bot: AntiBotConfig = field(default_factory=AntiBotConfig)
    note: NoteConfig = field(default_factory=NoteConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    val = raw.get(name, {})
    if not isinstance(val, dict):
        raise ConfigError(f"[{name}] must be a TOML table.")
    return val


def _validate_config(cfg: AppConfig) -> AppConfig:
    if cfg.mpris.backend not in {"auto", "busctl", "playerctl"}:
        raise ConfigError(
            f"mpris.backend must be 'auto', 'busctl', or 'playerctl', got {cfg.mpris.backend!r}"
        )
    if cfg.mpris.poll_interval_seconds <= 0:
        raise ConfigError("mpris.poll_interval_seconds must be > 0.")
    if cfg.mpris.max_length_seconds < 0:
        raise ConfigError("mpris.max_length_seconds must be >= 0.")
    if cfg.anti_bot.debounce_seconds < 0:
        raise ConfigError("anti_bot.debounce_seconds must be >= 0.")
    if cfg.anti_bot.jitter_min_seconds < 0:
        raise ConfigError("anti_bot.jitter_min_seconds must be >= 0.")
    if cfg.anti_bot.jitter_max_seconds < cfg.anti_bot.jitter_min_seconds:
        raise ConfigError("anti_bot.jitter_max_seconds must be >= jitter_min_seconds.")
    if cfg.anti_bot.min_post_interval_seconds < 0:
        raise ConfigError("anti_bot.min_post_interval_seconds must be >= 0.")
    if cfg.anti_bot.rate_limit_cooldown_seconds < 0:
        raise ConfigError("anti_bot.rate_limit_cooldown_seconds must be >= 0.")
    low, high = cfg.anti_bot.request_delay_range
    if low < 0 or high < low:
        raise ConfigError("anti_bot.request_delay_range must satisfy 0 <= min <= max.")
    if cfg.note.audience not in {"mutual_followers", "close_friends"}:
        raise ConfigError(
            "note.audience must be 'mutual_followers' or 'close_friends', "
            f"got {cfg.note.audience!r}"
        )
    return cfg


def load_config(path: Path = DEFAULT_CONFIG_FILE, *, allow_missing: bool = False) -> AppConfig:
    """Load and validate AppConfig from a TOML file."""
    if not path.exists():
        if allow_missing:
            return AppConfig()
        raise ConfigError(
            f"Configuration file '{path}' not found. "
            "Copy config.example.toml to config.toml or pass --config."
        )

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in '{path}': {exc}") from exc

    paths_raw = _section(raw, "paths")
    device_raw = _section(raw, "device")
    region_raw = _section(raw, "region")
    mpris_raw = _section(raw, "mpris")
    anti_bot_raw = _section(raw, "anti_bot")
    note_raw = _section(raw, "note")
    logging_raw = _section(raw, "logging")

    delay_raw = anti_bot_raw.get("request_delay_range", [1.5, 4.0])
    if not isinstance(delay_raw, list) or len(delay_raw) != 2:
        raise ConfigError("anti_bot.request_delay_range must be a 2-element list.")

    cfg = AppConfig(
        paths=PathsConfig(
            session_file=Path(str(paths_raw.get("session_file", "session.json"))),
            state_file=Path(str(paths_raw.get("state_file", ".note_state.json"))),
        ),
        device=DeviceConfig(
            android_version=int(device_raw.get("android_version", 34)),
            android_release=str(device_raw.get("android_release", "14")),
            dpi=str(device_raw.get("dpi", "480dpi")),
            resolution=str(device_raw.get("resolution", "1344x2992")),
            manufacturer=str(device_raw.get("manufacturer", "Google/google")),
            device=str(device_raw.get("device", "husky")),
            model=str(device_raw.get("model", "Pixel 8 Pro")),
            cpu=str(device_raw.get("cpu", "husky")),
        ),
        region=RegionConfig(
            locale=str(region_raw.get("locale", "en_US")),
            country=str(region_raw.get("country", "US")).upper(),
            country_code=int(region_raw.get("country_code", 1)),
            timezone_offset=int(region_raw.get("timezone_offset", 0)),
            timezone_name=str(region_raw.get("timezone_name", "UTC")),
        ),
        mpris=MprisConfig(
            backend=str(mpris_raw.get("backend", "auto")).lower(),
            poll_interval_seconds=float(mpris_raw.get("poll_interval_seconds", 5.0)),
            allowed_players=tuple(str(p) for p in mpris_raw.get("allowed_players", ())),
            ignored_players=tuple(
                str(p) for p in mpris_raw.get("ignored_players", ("kdeconnect", "gsconnect"))
            ),
            allowed_url_prefixes=tuple(
                str(u).strip() for u in mpris_raw.get("allowed_url_prefixes", ()) if str(u).strip()
            ),
            max_length_seconds=int(mpris_raw.get("max_length_seconds", 0)),
        ),
        anti_bot=AntiBotConfig(
            debounce_seconds=float(anti_bot_raw.get("debounce_seconds", 20.0)),
            jitter_min_seconds=float(anti_bot_raw.get("jitter_min_seconds", 3.0)),
            jitter_max_seconds=float(anti_bot_raw.get("jitter_max_seconds", 10.0)),
            min_post_interval_seconds=float(anti_bot_raw.get("min_post_interval_seconds", 150.0)),
            rate_limit_cooldown_seconds=float(
                anti_bot_raw.get("rate_limit_cooldown_seconds", 900.0)
            ),
            request_delay_range=(float(delay_raw[0]), float(delay_raw[1])),
        ),
        note=NoteConfig(
            audience=str(note_raw.get("audience", "mutual_followers")).lower(),
            text_template=str(note_raw.get("text_template", "")),
            clear_on_stop=bool(note_raw.get("clear_on_stop", False)),
            delete_before_post=bool(note_raw.get("delete_before_post", False)),
            check_existing_on_startup=bool(note_raw.get("check_existing_on_startup", True)),
            dry_run=bool(note_raw.get("dry_run", False)),
        ),
        logging=LoggingConfig(
            verbose=bool(logging_raw.get("verbose", False)),
        ),
    )
    return _validate_config(cfg)
