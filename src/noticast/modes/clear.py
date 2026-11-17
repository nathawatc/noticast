"""Clear mode: delete active personal Instagram Note(s) and local state."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable

from noticast.config import AppConfig
from noticast.notes_service import InstagramNotesService, NotesServiceError, open_notes_service
from noticast.session import SessionNotReadyError


def run_clear(
    config: AppConfig,
    logger: logging.Logger,
    *,
    dry_run: bool = False,
    service_factory: Callable[[AppConfig, logging.Logger], InstagramNotesService] | None = None,
) -> int:
    """Delete active personal Instagram Note(s) and remove cached local state."""
    try:
        service = (
            service_factory(config, logger)
            if service_factory is not None
            else open_notes_service(config, logger)
        )
        if dry_run or config.note.dry_run:
            active = service.get_active_music_note()
            if active is None:
                print("No active personal Note found.")
            else:
                print(f"[Dry Run] Would delete personal Note id={active.note.id}.")
            return 0

        deleted = service.delete_existing_user_notes()
    except (SessionNotReadyError, NotesServiceError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    config.paths.state_file.unlink(missing_ok=True)
    if deleted:
        print(f"Deleted {len(deleted)} active personal Note(s).")
    else:
        print("No active personal Note found.")
    return 0
