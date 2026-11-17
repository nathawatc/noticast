"""Interactive Manual mode (PoC-style track search, top-5 selection, and note post)."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Sequence

from instagrapi.mixins.note import NoteAudience
from instagrapi.types import Track

from mpris_ig.config import AppConfig
from mpris_ig.notes_service import (
    CheckpointRequiredError,
    InstagramNotesService,
    NotesApiError,
    RateLimitedError,
    SessionExpiredError,
)
from mpris_ig.session import SessionNotReadyError, load_authenticated_client
from mpris_ig.state import NoteState, save_note_state
from mpris_ig.text_utils import MAX_NOTE_TEXT_LENGTH, normalize_display

TOP_TRACKS_LIMIT = 5


def select_track(tracks: Sequence[Track], input_fn: Callable[[str], str] = input) -> Track:
    """Display up to 5 tracks and prompt the user for numeric selection."""
    top_tracks = list(tracks[:TOP_TRACKS_LIMIT])
    if not top_tracks:
        raise ValueError("No tracks found for that search query.")

    print(f"\nTop {len(top_tracks)} search results:")
    for idx, track in enumerate(top_tracks, start=1):
        starts = track.highlight_start_times_in_ms or [0]
        print(
            f"  [{idx}] {track.title} — {track.display_artist} "
            + f"(audio_cluster_id={track.audio_cluster_id}, "
            + f"audio_asset_id={track.audio_asset_id}, "
            + f"highlight_ms={starts[0]})"
        )

    while True:
        raw = input_fn(f"\nSelect track [1-{len(top_tracks)}]: ").strip()
        if raw.isdigit():
            choice = int(raw)
            if 1 <= choice <= len(top_tracks):
                return top_tracks[choice - 1]
        print(f"Invalid selection. Please enter a number between 1 and {len(top_tracks)}.")


def prompt_note_options(
    default_audience: NoteAudience,
    input_fn: Callable[[str], str] = input,
) -> tuple[str, NoteAudience]:
    """Prompt for optional note text (<=60 chars) and target audience."""
    while True:
        note_text = normalize_display(
            input_fn(f"Optional note text (max {MAX_NOTE_TEXT_LENGTH} chars, Enter for none): ")
        )
        if len(note_text) <= MAX_NOTE_TEXT_LENGTH:
            break
        print(f"Note text is {len(note_text)} chars; Instagram limit is {MAX_NOTE_TEXT_LENGTH}.")

    default_code = "1" if default_audience == NoteAudience.CLOSE_FRIENDS else "0"
    while True:
        aud_raw = input_fn(
            f"Audience [0 = Mutual Followers, 1 = Close Friends] (default {default_code}): "
        ).strip()
        if not aud_raw:
            return note_text, default_audience
        if aud_raw == "0":
            return note_text, NoteAudience.MUTUAL_FOLLOWERS
        if aud_raw == "1":
            return note_text, NoteAudience.CLOSE_FRIENDS
        print("Invalid audience. Enter 0 or 1.")


def run_manual(
    config: AppConfig,
    logger: logging.Logger,
    *,
    dry_run: bool = False,
    input_fn: Callable[[str], str] = input,
    service_factory: Callable[[AppConfig, logging.Logger], InstagramNotesService] | None = None,
) -> int:
    """Run interactive manual Music Note search & post flow."""
    try:
        if service_factory is not None:
            service = service_factory(config, logger)
        else:
            cl = load_authenticated_client(config)
            logger.info("Loaded session for user_id=%s (no login performed).", cl.user_id)
            service = InstagramNotesService(cl, logger)
    except SessionNotReadyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    query = normalize_display(input_fn("Search song title / artist: "))
    if not query:
        print("Error: Search query cannot be empty.", file=sys.stderr)
        return 1

    try:
        tracks = service.search_tracks(query)
        if not tracks:
            print(f"No tracks found for query: {query!r}")
            return 1

        selected_track = select_track(tracks, input_fn=input_fn)
        note_text, audience = prompt_note_options(config.note.audience_enum, input_fn=input_fn)

        if dry_run or config.note.dry_run:
            print(
                f"\n[Dry Run] Would post: {selected_track.title} — "
                + f"{selected_track.display_artist} "
                + f"(audio_cluster_id={selected_track.audio_cluster_id}, "
                + f"text={note_text!r}, audience={audience.name})"
            )
            return 0

        created_note = service.replace_music_note(
            selected_track,
            text=note_text,
            audience=audience,
            delete_before_post=config.note.delete_before_post,
        )
        save_note_state(
            config.paths.state_file,
            NoteState(
                note_id=str(created_note.id),
                mpris_title=selected_track.title,
                mpris_artist=selected_track.display_artist,
                ig_title=selected_track.title,
                ig_artist=selected_track.display_artist,
                audio_cluster_id=str(selected_track.audio_cluster_id),
            ),
        )
    except (SessionExpiredError, CheckpointRequiredError) as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    except RateLimitedError as exc:
        print(f"\nRate limited by Instagram: {exc}", file=sys.stderr)
        return 1
    except NotesApiError as exc:
        print(f"\nInstagram API error: {exc}", file=sys.stderr)
        return 1

    print("\nMusic Note posted successfully!")
    print(f"  Note ID    : {created_note.id}")
    print(f"  Track      : {selected_track.title} — {selected_track.display_artist}")
    print(f"  Cluster ID : {selected_track.audio_cluster_id}")
    print(f"  Text       : {created_note.text!r}")
    print(f"  Audience   : {audience.name} ({created_note.audience})")
    print(f"  Note Style : {created_note.note_style}")
    return 0
