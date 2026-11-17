"""Typed TOML configuration loader and Pydantic validation."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Annotated, Any, Literal

from instagrapi.mixins.note import NoteAudience
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

DEFAULT_CONFIG_FILE = Path("config.toml")


class ConfigError(ValueError):
    """Raised when configuration file parsing or validation fails."""


def _lower_str(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value


def _clean_str_tuple(value: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(item.strip() for item in value if item.strip())


class _FrozenConfigModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class PathsConfig(_FrozenConfigModel):
    session_file: Path = Path("session.json")
    state_file: Path = Path(".note_state.json")


class DeviceConfig(_FrozenConfigModel):
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
        return self.model_dump()


class RegionConfig(_FrozenConfigModel):
    locale: str = "en_US"
    country: Annotated[str, AfterValidator(str.upper)] = "US"
    country_code: int = 1
    timezone_offset: int = 0
    timezone_name: str = "UTC"


class MprisConfig(_FrozenConfigModel):
    backend: Annotated[Literal["auto", "dbus"], BeforeValidator(_lower_str)] = "auto"
    poll_interval_seconds: Annotated[float, Field(gt=0)] = 5.0
    allowed_players: tuple[str, ...] = ()
    ignored_players: tuple[str, ...] = ("kdeconnect", "gsconnect")
    allowed_url_prefixes: tuple[str, ...] = ()
    max_length_seconds: Annotated[int, Field(ge=0)] = 0

    @field_validator("allowed_players", "ignored_players", "allowed_url_prefixes", mode="after")
    @classmethod
    def _strip_empty_tokens(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_str_tuple(value)


class AntiBotConfig(_FrozenConfigModel):
    debounce_seconds: Annotated[float, Field(ge=0)] = 20.0
    jitter_min_seconds: Annotated[float, Field(ge=0)] = 3.0
    jitter_max_seconds: Annotated[float, Field(ge=0)] = 10.0
    min_post_interval_seconds: Annotated[float, Field(ge=0)] = 150.0
    rate_limit_cooldown_seconds: Annotated[float, Field(ge=0)] = 900.0
    request_delay_range: tuple[float, float] = (1.5, 4.0)

    @model_validator(mode="after")
    def _check_ranges(self) -> AntiBotConfig:
        if self.jitter_max_seconds < self.jitter_min_seconds:
            raise ValueError("anti_bot.jitter_max_seconds must be >= jitter_min_seconds.")
        low, high = self.request_delay_range
        if low < 0 or high < low:
            raise ValueError("anti_bot.request_delay_range must satisfy 0 <= min <= max.")
        return self


class NoteConfig(_FrozenConfigModel):
    audience: Annotated[
        Literal["mutual_followers", "close_friends"],
        BeforeValidator(_lower_str),
    ] = "mutual_followers"
    text_template: str = ""
    clear_on_stop: bool = False
    delete_before_post: bool = False
    check_existing_on_startup: bool = True
    dry_run: bool = False

    @field_validator("text_template", mode="after")
    @classmethod
    def _validate_text_template(cls, value: str) -> str:
        if value:
            try:
                value.format(title="title", artist="artist")
            except (KeyError, ValueError, IndexError) as exc:
                raise ValueError(f"Invalid note.text_template {value!r}: {exc}") from exc
        return value

    @property
    def audience_enum(self) -> NoteAudience:
        """Map string config value to instagrapi NoteAudience enum."""
        mapping = {
            "mutual_followers": NoteAudience.MUTUAL_FOLLOWERS,
            "close_friends": NoteAudience.CLOSE_FRIENDS,
        }
        return mapping[self.audience]


class LoggingConfig(_FrozenConfigModel):
    verbose: bool = False


class AppConfig(_FrozenConfigModel):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    device: DeviceConfig = Field(default_factory=DeviceConfig)
    region: RegionConfig = Field(default_factory=RegionConfig)
    mpris: MprisConfig = Field(default_factory=MprisConfig)
    anti_bot: AntiBotConfig = Field(default_factory=AntiBotConfig)
    note: NoteConfig = Field(default_factory=NoteConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def load_config(path: Path = DEFAULT_CONFIG_FILE) -> AppConfig:
    """Load and validate AppConfig from a TOML file."""
    if not path.exists():
        raise ConfigError(
            f"Configuration file '{path}' not found. "
            "Copy config.example.toml to config.toml or pass --config."
        )

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        return AppConfig.model_validate(raw)
    except (tomllib.TOMLDecodeError, ValidationError) as exc:
        raise ConfigError(f"Invalid configuration in '{path}': {exc}") from exc
