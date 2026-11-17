"""Instagram Music Notes service wrapping instagrapi 3.2.15 GraphQL/REST calls."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TypeVar, cast

import requests
from instagrapi import Client
from instagrapi.exceptions import (
    ChallengeRequired,
    ClientError,
    ClientGraphqlError,
    ClientLoginRequired,
    ClientThrottledError,
    ClientUnauthorizedError,
    FeedbackRequired,
    LoginRequired,
    PleaseWaitFewMinutes,
)
from instagrapi.mixins.note import NoteAudience
from instagrapi.types import Note, Track

from mpris_ig.logging_utils import log_http_debug
from mpris_ig.text_utils import build_search_queries, normalize_display, score_track_match

_T = TypeVar("_T")


class SessionExpiredError(RuntimeError):
    """Raised when Instagram rejects the saved session and requires re-setup."""


class CheckpointRequiredError(RuntimeError):
    """Raised when Instagram triggers a ChallengeRequired checkpoint."""


class RateLimitedError(RuntimeError):
    """Raised on 429 / FeedbackRequired / PleaseWaitFewMinutes responses."""


class NotesApiError(RuntimeError):
    """Raised on other Instagram API failures."""


@dataclass(frozen=True)
class ActiveMusicNote:
    """Active user Note paired with any music metadata found in InboxTray JSON."""

    note: Note
    audio_cluster_id: str = ""
    track_title: str = ""
    artist_name: str = ""


def _find_audio_asset_dict(obj: object) -> dict[str, object] | None:
    """Recursively locate a music asset dictionary, preferring one with a track title."""
    fallback: dict[str, object] | None = None
    if isinstance(obj, dict):
        mapping = cast(dict[str, object], obj)
        if mapping.get("audio_cluster_id") is not None:
            if mapping.get("title"):
                return mapping
            fallback = mapping
        children: Iterable[object] = mapping.values()
    elif isinstance(obj, list):
        children = cast(list[object], obj)
    else:
        return None

    for child in children:
        found = _find_audio_asset_dict(child)
        if found is not None:
            if found.get("title"):
                return found
            if fallback is None:
                fallback = found
    return fallback


def _extract_note_dict_for_user(
    last_json: Mapping[str, object] | None,
    user_id: int | str,
    note_id: str,
) -> dict[str, object] | None:
    if not isinstance(last_json, Mapping):
        return None
    data = last_json.get("data")
    if not isinstance(data, dict):
        return None
    for payload in cast(dict[str, object], data).values():
        if not isinstance(payload, dict) or "inbox_tray_items" not in payload:
            continue
        raw_items = cast(dict[str, object], payload).get("inbox_tray_items")
        if not isinstance(raw_items, list):
            continue
        for raw_item in cast(list[object], raw_items):
            if not isinstance(raw_item, dict):
                continue
            item = cast(dict[str, object], raw_item)
            raw_note = item.get("note_dict")
            if not isinstance(raw_note, dict):
                continue
            note_dict = cast(dict[str, object], raw_note)
            item_id = str(item.get("inbox_tray_item_id") or note_dict.get("note_id") or "")
            author_id = str(note_dict.get("author_id") or "")
            if (note_id and item_id == str(note_id)) or (user_id and author_id == str(user_id)):
                return note_dict
    return None


def extract_active_music_note(
    notes: list[Note],
    last_json: Mapping[str, object] | None,
    user_id: int | str,
) -> ActiveMusicNote | None:
    """Extract the user's active Note plus any music metadata from InboxTray JSON."""
    own_note = next((n for n in notes if str(n.user_id) == str(user_id)), None)
    if own_note is None:
        return None

    note_dict = _extract_note_dict_for_user(last_json, user_id, str(own_note.id))
    audio_dict = _find_audio_asset_dict(note_dict) if note_dict else None
    if not audio_dict:
        return ActiveMusicNote(note=own_note)

    return ActiveMusicNote(
        note=own_note,
        audio_cluster_id=str(audio_dict.get("audio_cluster_id") or "").strip(),
        track_title=normalize_display(str(audio_dict.get("title") or "")),
        artist_name=normalize_display(
            str(audio_dict.get("display_artist") or audio_dict.get("artist_name") or "")
        ),
    )


def _is_login_error(cl: Client, exc: Exception) -> bool:
    if isinstance(exc, (LoginRequired, ClientLoginRequired, ClientUnauthorizedError)):
        return True
    resp: object = getattr(cl, "last_response", None)
    if getattr(resp, "status_code", None) == 401:
        return True
    raw_last_json: object = getattr(cl, "last_json", None)
    if isinstance(raw_last_json, dict):
        last_json = cast(dict[str, object], raw_last_json)
        if str(last_json.get("message", "")).lower() == "login_required":
            return True
    return "login_required" in str(exc).lower()


class InstagramNotesService:
    """High-level Music Notes operations with error classification and debug logging."""

    def __init__(self, cl: Client, logger: logging.Logger) -> None:
        self._cl: Client = cl
        self._logger: logging.Logger = logger

    def _call(self, label: str, fn: Callable[[], _T]) -> _T:
        try:
            result = fn()
        except ChallengeRequired as exc:
            log_http_debug(self._logger, self._cl, f"{label} [ChallengeRequired]")
            raise CheckpointRequiredError(f"Instagram challenge required: {exc}") from exc
        except (FeedbackRequired, PleaseWaitFewMinutes, ClientThrottledError) as exc:
            log_http_debug(self._logger, self._cl, f"{label} [{exc.__class__.__name__}]")
            raise RateLimitedError(f"{exc.__class__.__name__}: {exc}") from exc
        except (
            LoginRequired,
            ClientLoginRequired,
            ClientUnauthorizedError,
            ClientGraphqlError,
            ClientError,
        ) as exc:
            log_http_debug(self._logger, self._cl, f"{label} [{exc.__class__.__name__}]")
            if _is_login_error(self._cl, exc):
                raise SessionExpiredError(
                    "Session expired or invalid (LoginRequired). "
                    + "Re-run `./.venv/bin/python main.py setup`."
                ) from exc
            raise NotesApiError(f"{exc.__class__.__name__}: {exc}") from exc
        except (requests.exceptions.RequestException, KeyError, AssertionError, ValueError) as exc:
            log_http_debug(self._logger, self._cl, f"{label} [{exc.__class__.__name__}]")
            raise NotesApiError(f"{exc.__class__.__name__}: {exc}") from exc

        log_http_debug(self._logger, self._cl, label)
        return result

    def search_tracks(self, query: str) -> list[Track]:
        """Search Instagram music tracks via FbSearchMixin.search_music."""
        return self._call(
            f"cl.search_music({query!r})",
            lambda: self._cl.search_music(query),
        )

    def find_best_track(
        self,
        title: str,
        artist: str = "",
        *,
        min_score: float = 25.0,
    ) -> tuple[Track, float] | None:
        """Search across normalized query fallbacks and return the highest-scoring Track."""
        queries = build_search_queries(title, artist)
        if not queries:
            return None

        best_track: Track | None = None
        best_score = -1.0

        for query in queries:
            tracks = self.search_tracks(query)
            if not tracks:
                continue
            for track in tracks[:10]:
                score = score_track_match(track, title, artist)
                self._logger.debug(
                    "[Match] query=%r | %s — %s | score=%.2f",
                    query,
                    track.title,
                    track.display_artist,
                    score,
                )
                if score > best_score:
                    best_score = score
                    best_track = track
            if best_score >= 80.0:
                break

        if best_track is None or best_score < min_score:
            return None
        return best_track, best_score

    def get_active_music_note(self) -> ActiveMusicNote | None:
        """Fetch Inbox Tray notes and return the active personal note + music metadata."""
        notes = self._call("cl.get_notes() [startup check]", self._cl.get_notes)
        raw_last_json: object = getattr(self._cl, "last_json", None)
        last_json = (
            cast(dict[str, object], raw_last_json) if isinstance(raw_last_json, dict) else None
        )
        return extract_active_music_note(notes, last_json, self._cl.user_id)

    def delete_existing_user_notes(self) -> list[Note]:
        """Fetch Inbox Tray notes and delete any active personal note."""
        notes = self._call("cl.get_notes()", self._cl.get_notes)
        own_notes = [n for n in notes if str(n.user_id) == str(self._cl.user_id)]
        if not own_notes:
            self._logger.debug("No active personal note found to delete.")
            return []

        for note in own_notes:
            self._logger.info(
                "Deleting existing personal note id=%s (text=%r)",
                note.id,
                note.text,
            )
            self._call(
                f"cl.delete_note({note.id})",
                lambda nid=note.id: self._cl.delete_note(int(nid)),
            )
        return own_notes

    def post_music_note(
        self,
        track: Track,
        *,
        text: str = "",
        audience: NoteAudience = NoteAudience.MUTUAL_FOLLOWERS,
    ) -> Note:
        """Create a Music Note via GraphQL (instagrapi acquires alacorn_session_id)."""
        return self._call(
            f"cl.create_music_note({track.title!r})",
            lambda: self._cl.create_music_note(  # pyright: ignore[reportUnknownMemberType]
                track=track,
                text=text,
                audience=audience,
            ),
        )

    def replace_music_note(
        self,
        track: Track,
        *,
        text: str = "",
        audience: NoteAudience = NoteAudience.MUTUAL_FOLLOWERS,
        delete_before_post: bool = False,
    ) -> Note:
        """Post a new Music Note, optionally deleting active personal notes first."""
        if delete_before_post:
            self.delete_existing_user_notes()
        return self.post_music_note(track, text=text, audience=audience)
