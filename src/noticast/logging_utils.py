"""UTF-8 safe logging configuration and verbose HTTP/JSON diagnostics."""

from __future__ import annotations

import json
import logging
import sys
from typing import cast

from instagrapi import Client


def configure_logging(*, verbose: bool = False) -> logging.Logger:
    """Configure application logger with INFO or DEBUG verbosity."""
    logger = logging.getLogger("noticast")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    for handler in logger.handlers:
        handler.setLevel(logging.DEBUG if verbose else logging.INFO)

    return logger


def sanitize_last_json(cl: Client) -> dict[str, object]:
    """Extract non-sensitive diagnostic fields from client.last_json."""
    raw_last_json: object = getattr(cl, "last_json", None)
    if not isinstance(raw_last_json, dict) or not raw_last_json:
        return {}
    last_json = cast(dict[object, object], raw_last_json)
    safe_keys = (
        "status",
        "message",
        "error_type",
        "feedback_message",
        "challenge",
        "step_name",
        "two_factor_info",
    )
    return {k: last_json[k] for k in safe_keys if k in last_json}


def log_http_debug(logger: logging.Logger, cl: Client, label: str) -> None:
    """Log HTTP response metadata and UTF-8 JSON when DEBUG verbosity is enabled."""
    if not logger.isEnabledFor(logging.DEBUG):
        return

    resp: object = getattr(cl, "last_response", None)
    if resp is not None:
        status_code: object = getattr(resp, "status_code", "N/A")
        url: object = getattr(resp, "url", "N/A")
        logger.debug("[HTTP] %s -> HTTP %s | %s", label, status_code, url)
    else:
        logger.debug("[HTTP] %s -> <no response>", label)

    last_json: object = getattr(cl, "last_json", None)
    if last_json:
        formatted = json.dumps(last_json, indent=2, ensure_ascii=False, default=str)
        logger.debug("[JSON] %s:\n%s", label, formatted)
    elif resp is not None:
        raw_text: object = getattr(resp, "text", "")
        text = raw_text if isinstance(raw_text, str) else ""
        if text:
            logger.debug("[BODY] %s (truncated): %s", label, text[:1000])
