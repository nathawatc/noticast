"""Persistent local state cache for the latest posted Music Note."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Annotated, ClassVar

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from noticast.text_utils import normalize_display


def _strip_str(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


class NoteState(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    note_id: Annotated[str, BeforeValidator(_strip_str), Field(min_length=1)]
    mpris_title: Annotated[str, AfterValidator(normalize_display)] = ""
    mpris_artist: Annotated[str, AfterValidator(normalize_display)] = ""
    ig_title: Annotated[str, AfterValidator(normalize_display)] = ""
    ig_artist: Annotated[str, AfterValidator(normalize_display)] = ""
    audio_cluster_id: Annotated[str, AfterValidator(str.strip)] = ""


def load_note_state(path: Path) -> NoteState | None:
    """Load saved NoteState from disk, returning None if absent or invalid."""
    if not path.exists():
        return None
    try:
        return NoteState.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError, ValidationError:
        return None


def save_note_state(path: Path, state: NoteState) -> None:
    """Atomically persist NoteState as UTF-8 JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = state.model_dump_json(indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise
