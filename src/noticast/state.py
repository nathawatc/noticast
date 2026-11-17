"""Persistent local state cache for the latest posted Music Note."""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from collections.abc import Callable
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


def atomic_replace_via_temp(
    path: Path,
    write_fn: Callable[[Path], object],
    *,
    mode: int = stat.S_IRUSR | stat.S_IWUSR,
) -> None:
    """Write via a restricted temp file in `path.parent` and atomically swap into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with contextlib.suppress(OSError):
            os.chmod(tmp_path, mode)
        write_fn(tmp_path)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


def save_note_state(path: Path, state: NoteState) -> None:
    """Atomically persist NoteState as UTF-8 JSON."""
    payload = state.model_dump_json(indent=2) + "\n"
    atomic_replace_via_temp(path, lambda tmp: tmp.write_text(payload, encoding="utf-8"))
