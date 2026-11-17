"""Unified CLI entrypoint for setup, logout, manual, and daemon modes."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from mpris_ig.config import DEFAULT_CONFIG_FILE, AppConfig, ConfigError, load_config
from mpris_ig.logging_utils import configure_logging
from mpris_ig.modes.daemon import run_daemon
from mpris_ig.modes.manual import run_manual
from mpris_ig.session import logout_session, setup_session_interactive


class _CliNamespace(argparse.Namespace):
    config: Path = DEFAULT_CONFIG_FILE
    verbose: bool = False
    quiet: bool = False
    command: str = ""
    keep_device: bool = False
    force: bool = False
    dry_run: bool = False
    once: bool = False
    poll_interval: float | None = None
    debounce: float | None = None


def build_parser() -> argparse.ArgumentParser:
    """Build top-level CLI parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="noticast",
        description="Post Instagram Music Notes manually or automatically via MPRIS.",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_FILE,
        help=f"Path to TOML config file (default: {DEFAULT_CONFIG_FILE}).",
    )
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging (HTTP responses, JSON payloads, MPRIS/match scores).",
    )
    verbosity.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Disable DEBUG logging even if logging.verbose = true in config.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    setup_p = subparsers.add_parser(
        "setup", help="Authenticate once (supports 2FA) and save session.json."
    )
    setup_p.add_argument(
        "--keep-device",
        action="store_true",
        help="Keep saved device/region when reusing an existing session file.",
    )

    logout_p = subparsers.add_parser(
        "logout", help="Log out saved session on Instagram and delete session.json."
    )
    logout_p.add_argument(
        "--force",
        action="store_true",
        help="Remove local session.json even if remote API logout fails.",
    )

    manual_p = subparsers.add_parser(
        "manual", help="Mode 1: interactive song search and Music Note post."
    )
    manual_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Search and select a track without posting to Instagram.",
    )

    daemon_p = subparsers.add_parser(
        "daemon", help="Mode 2: watch MPRIS players and update Music Notes automatically."
    )
    daemon_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect MPRIS tracks and match on Instagram without posting.",
    )
    daemon_p.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll cycle and exit.",
    )
    daemon_p.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        help="Override mpris.poll_interval_seconds from config.",
    )
    daemon_p.add_argument(
        "--debounce",
        type=float,
        default=None,
        help="Override anti_bot.debounce_seconds from config.",
    )

    return parser


def _apply_cli_overrides(config: AppConfig, args: _CliNamespace) -> AppConfig:
    updated = config
    if args.command == "daemon":
        if args.poll_interval is not None:
            if args.poll_interval <= 0:
                raise ConfigError("--poll-interval must be > 0.")
            updated = updated.model_copy(
                update={
                    "mpris": updated.mpris.model_copy(
                        update={"poll_interval_seconds": args.poll_interval}
                    )
                }
            )
        if args.debounce is not None:
            if args.debounce < 0:
                raise ConfigError("--debounce must be >= 0.")
            updated = updated.model_copy(
                update={
                    "anti_bot": updated.anti_bot.model_copy(
                        update={"debounce_seconds": args.debounce}
                    )
                }
            )
    return updated


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, load config, configure logging, and dispatch subcommand."""
    args = build_parser().parse_args(argv, namespace=_CliNamespace())

    try:
        config = load_config(args.config)
        config = _apply_cli_overrides(config, args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    verbose = config.logging.verbose
    if args.verbose:
        verbose = True
    elif args.quiet:
        verbose = False

    logger = configure_logging(verbose=verbose)

    try:
        if args.command == "setup":
            return setup_session_interactive(config, keep_device=args.keep_device)
        if args.command == "logout":
            return logout_session(config, force=args.force)
        if args.command == "manual":
            return run_manual(config, logger, dry_run=args.dry_run)
        if args.command == "daemon":
            return run_daemon(config, logger, dry_run=args.dry_run, once=args.once)
    except KeyboardInterrupt, EOFError:
        print("\nCancelled.", file=sys.stderr)
        return 130

    return 1
