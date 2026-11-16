"""Lightweight local state cache for the latest posted Music Note."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from mpris_ig.text_utils import normalize_display


@dataclass(frozen=True)
class NoteState:
    note_id: str
    mpris_title: str
    mpris_artist: str
    ig_title: str
    ig_artist: str
    audio_cluster_id: str


def load_note_state(path: Path) -> NoteState | None:
    """Load saved NoteState from disk, returning None if absent or invalid."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    note_id = str(raw.get("note_id") or "").strip()
    if not note_id:
        return None
    return NoteState(
        note_id=note_id,
        mpris_title=normalize_display(str(raw.get("mpris_title") or "")),
        mpris_artist=normalize_display(str(raw.get("mpris_artist") or "")),
        ig_title=normalize_display(str(raw.get("ig_title") or "")),
        ig_artist=normalize_display(str(raw.get("ig_artist") or "")),
        audio_cluster_id=str(raw.get("audio_cluster_id") or "").strip(),
    )


def save_note_state(path: Path, state: NoteState) -> None:
    """Persist NoteState as UTF-8 JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(state), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
