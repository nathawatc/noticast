"""UTF-8 safe logging configuration and verbose HTTP/JSON diagnostics."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from instagrapi import Client


def configure_logging(*, verbose: bool = False) -> logging.Logger:
    """Configure application logger with INFO or DEBUG verbosity."""
    logger = logging.getLogger("mpris_ig")
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


def sanitize_last_json(cl: Client) -> dict[str, Any]:
    """Extract non-sensitive diagnostic fields from client.last_json."""
    last_json = getattr(cl, "last_json", None) or {}
    if not isinstance(last_json, dict) or not last_json:
        return {}
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

    resp = getattr(cl, "last_response", None)
    if resp is not None:
        status_code = getattr(resp, "status_code", "N/A")
        url = getattr(resp, "url", "N/A")
        logger.debug("[HTTP] %s -> HTTP %s | %s", label, status_code, url)
    else:
        logger.debug("[HTTP] %s -> <no response>", label)

    last_json = getattr(cl, "last_json", None)
    if last_json:
        formatted = json.dumps(last_json, indent=2, ensure_ascii=False, default=str)
        logger.debug("[JSON] %s:\n%s", label, formatted)
    elif resp is not None:
        text = getattr(resp, "text", "") or ""
        if text:
            logger.debug("[BODY] %s (truncated): %s", label, text[:1000])
