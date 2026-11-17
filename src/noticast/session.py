"""Session lifecycle management: client creation, device/region spoofing, login, logout."""

from __future__ import annotations

import contextlib
import getpass
import json
import os
import stat
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from instagrapi import Client
from instagrapi.exceptions import (
    BadCredentials,
    BadPassword,
    ChallengeRequired,
    ClientError,
    ClientLoginRequired,
    ClientUnauthorizedError,
    LoginRequired,
    PleaseWaitFewMinutes,
    TwoFactorRequired,
)

from noticast.config import AppConfig, DeviceConfig, RegionConfig
from noticast.logging_utils import sanitize_last_json


class SessionNotReadyError(RuntimeError):
    """Raised when session.json is missing or lacks valid authenticated state."""


class ChallengeCodeHandler(Protocol):
    """Callable protocol matching instagrapi's challenge_code_handler signature."""

    def __call__(self, username: str, choice: object = None) -> str: ...


def interactive_challenge_handler(username: str, choice: object = None) -> str:
    """Prompt the user for an Email/SMS challenge verification code."""
    print(f"\n[Challenge] Instagram requested verification for '{username}' via {choice}.")
    return input("Enter challenge verification code (leave blank to abort): ").strip()


def apply_device_and_region(
    cl: Client,
    device_cfg: DeviceConfig,
    region_cfg: RegionConfig,
) -> None:
    """Apply configured Android hardware profile and regional/timezone settings."""
    cl.set_locale(region_cfg.locale)
    cl.set_country(region_cfg.country.upper())
    cl.set_country_code(region_cfg.country_code)
    cl.set_timezone_offset(  # pyright: ignore[reportUnknownMemberType]
        region_cfg.timezone_offset, timezone_name=region_cfg.timezone_name
    )
    cl.set_device(device_cfg.to_instagrapi_dict())  # pyright: ignore[reportUnknownMemberType]
    base_headers = cast(dict[str, str], cl.base_headers)
    cl.private.headers.update(base_headers)


def _load_session_settings(cl: Client, session_path: Path) -> None:
    try:
        cl.load_settings(session_path)  # pyright: ignore[reportUnknownMemberType]
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SessionNotReadyError(
            f"Session file '{session_path}' is unreadable or corrupted ({exc}). "
            + "Re-run `noticast setup` (or `python main.py setup`)."
        ) from exc


def create_client(
    config: AppConfig,
    *,
    reuse_existing: bool = False,
    apply_profile: bool = True,
    with_challenge_flow: bool = True,
    challenge_handler: ChallengeCodeHandler = interactive_challenge_handler,
) -> Client:
    """Create and configure an instagrapi Client instance from AppConfig."""
    cl = Client(with_challenge_flow=with_challenge_flow)
    cl.delay_range = list(config.anti_bot.request_delay_range)
    cl.challenge_code_handler = challenge_handler

    session_path = config.paths.session_file
    if reuse_existing and session_path.exists():
        print(f"Loading existing device/session settings from {session_path}...")
        _load_session_settings(cl, session_path)

    if apply_profile:
        apply_device_and_region(cl, config.device, config.region)

    return cl


def load_authenticated_client(config: AppConfig) -> Client:
    """Load session.json into a Client without triggering a password login."""
    session_path = config.paths.session_file
    if not session_path.exists():
        raise SessionNotReadyError(
            f"Session file '{session_path}' not found. "
            + "Run `noticast setup` (or `python main.py setup`) first."
        )

    cl = Client(with_challenge_flow=False)
    cl.delay_range = list(config.anti_bot.request_delay_range)
    _load_session_settings(cl, session_path)
    if not cl.user_id:
        raise SessionNotReadyError(
            f"'{session_path}' does not contain an authenticated user_id. "
            + "Re-run `noticast setup` (or `python main.py setup`)."
        )
    return cl


def authenticate_and_save(
    cl: Client,
    username: str,
    password: str,
    session_path: Path,
    input_fn: Callable[[str], str] = input,
) -> Path:
    """Authenticate the client (including 2FA retry) and dump session settings."""
    try:
        logged_in = cl.login(username, password)
    except TwoFactorRequired:
        print("\n[2FA Required] Enter your 6-digit authenticator/SMS code or 8-digit backup code.")
        verification_code = input_fn("Verification code: ").strip()
        if not verification_code:
            raise ValueError("Verification code cannot be empty when 2FA is required.") from None
        logged_in = cl.login(username, password, verification_code=verification_code)

    if not logged_in:
        raise RuntimeError("Instagram login returned False without raising an exception.")

    session_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=session_path.parent,
        prefix=f".{session_path.name}.",
        suffix=".tmp",
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with contextlib.suppress(OSError):
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        cl.dump_settings(tmp_path)
        os.replace(tmp_path, session_path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise
    return session_path


def _print_sanitized_error_details(cl: Client) -> None:
    summary = sanitize_last_json(cl)
    if summary:
        print("Last API details:")
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))


def setup_session_interactive(
    config: AppConfig,
    *,
    keep_device: bool = False,
    input_fn: Callable[[str], str] = input,
    getpass_fn: Callable[[str], str] = getpass.getpass,
) -> int:
    """Interactive session setup supporting 2FA, challenge flow, and spoof profile."""
    session_path = config.paths.session_file
    print("=== Instagram Session Setup ===")

    reuse_existing = False
    if session_path.exists():
        if keep_device:
            reuse_existing = True
        else:
            answer = (
                input_fn(f"{session_path} already exists. Reuse its device settings? [y/N]: ")
                .strip()
                .lower()
            )
            reuse_existing = answer in ("y", "yes")

    apply_profile = not reuse_existing
    if apply_profile:
        print(
            f"Device profile: {config.device.manufacturer} {config.device.model} "
            + f"({config.device.device}) | Android {config.device.android_release} | "
            + f"{config.device.resolution} {config.device.dpi}"
        )
        print(
            f"Region profile: locale={config.region.locale}, "
            + f"country={config.region.country}, +{config.region.country_code}, "
            + f"{config.region.timezone_name} ({config.region.timezone_offset:+}s)"
        )

    username = input_fn("Instagram username: ").strip()
    if not username:
        print("Error: Username is required.", file=sys.stderr)
        return 1

    password = getpass_fn("Instagram password: ")
    if not password:
        print("Error: Password is required.", file=sys.stderr)
        return 1

    try:
        cl = create_client(
            config,
            reuse_existing=reuse_existing,
            apply_profile=apply_profile,
            with_challenge_flow=True,
        )
    except SessionNotReadyError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1

    try:
        saved_path = authenticate_and_save(cl, username, password, session_path, input_fn=input_fn)
    except TwoFactorRequired as exc:
        print(f"\n2FA verification failed: {exc}", file=sys.stderr)
        _print_sanitized_error_details(cl)
        return 1
    except ChallengeRequired as exc:
        print(
            f"\nChallenge required and could not be completed automatically: {exc}",
            file=sys.stderr,
        )
        _print_sanitized_error_details(cl)
        print(
            "Approve the login attempt in the official Instagram app/web, "
            + "then re-run `noticast setup` (or `python main.py setup`).",
            file=sys.stderr,
        )
        return 1
    except (BadCredentials, BadPassword) as exc:
        print(f"\nAuthentication rejected: {exc}", file=sys.stderr)
        _print_sanitized_error_details(cl)
        return 1
    except PleaseWaitFewMinutes as exc:
        print(
            f"\nRate limited by Instagram ({exc}). Wait a few minutes before retrying.",
            file=sys.stderr,
        )
        return 1
    except (ClientError, ValueError, RuntimeError) as exc:
        print(f"\nLogin failed ({exc.__class__.__name__}): {exc}", file=sys.stderr)
        _print_sanitized_error_details(cl)
        return 1

    print(f"\nSession saved to {saved_path} (user_id={cl.user_id}).")
    return 0


def logout_session(
    config: AppConfig,
    *,
    force: bool = False,
    client_factory: Callable[[], Client] = Client,
) -> int:
    """Log out the saved Instagram session on the server and remove session.json."""
    session_path = config.paths.session_file
    if not session_path.exists():
        print(f"No session file found at {session_path}; nothing to log out.")
        return 0

    cl = client_factory()
    cl.with_challenge_flow = False
    try:
        _load_session_settings(cl, session_path)
    except SessionNotReadyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if force:
            session_path.unlink(missing_ok=True)
            print(f"Removed local {session_path} (--force).")
            return 0
        return 1

    if not cl.user_id:
        session_path.unlink(missing_ok=True)
        print(f"Removed {session_path} (no active user_id was stored).")
        return 0

    uid = cl.user_id
    try:
        logged_out = cl.logout()
    except (LoginRequired, ClientLoginRequired, ClientUnauthorizedError) as exc:
        session_path.unlink(missing_ok=True)
        print(
            f"Session for user_id={uid} was already expired ({exc}); removed local {session_path}."
        )
        return 0
    except ClientError as exc:
        print(f"Remote logout failed for user_id={uid}: {exc}", file=sys.stderr)
        _print_sanitized_error_details(cl)
        if force:
            session_path.unlink(missing_ok=True)
            print(f"Removed local {session_path} (--force).")
            return 0
        print(
            "Local session file was kept. Re-run with `--force` to delete it locally.",
            file=sys.stderr,
        )
        return 1

    if logged_out or force:
        session_path.unlink(missing_ok=True)
        print(f"Logged out user_id={uid} and removed {session_path}.")
        return 0

    print(
        f"Instagram returned logout=False for user_id={uid}; kept {session_path}.",
        file=sys.stderr,
    )
    return 1
