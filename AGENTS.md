# AGENTS.md

Technical reference and contributor/agent guidelines for **noticast** (`v0.0.1`,
currently in internal testing).

## Status & Scope

- **Stage**: Early internal testing (`v0.0.1`); interfaces and defaults may
  evolve.
- **Environment Rule**: Always work inside a Python `3.14+` virtual environment
  (`.venv`). Never install packages globally.
- **Privacy Rule**: Never commit `session.json`, `config.toml`,
  `.note_state.json`, or personal device identifiers / real timezone / locale
  defaults into `config.example.toml`, `src/mpris_ig/config.py`, or `tests/`.

## Architecture (`src/mpris_ig/`)

```text
main.py                    # Launcher adding src/ to sys.path
src/mpris_ig/
├── cli.py                 # Argument parsing, --config / -v / -q, dispatch
├── config.py              # Dataclass schema + tomllib loader + validation
├── logging_utils.py       # INFO/DEBUG logger + UTF-8 HTTP/JSON debug output
├── text_utils.py          # NFC/NFKC normalization, CJK/Thai scoring, 60-char
├── state.py               # .note_state.json cache (note_id + track info)
├── session.py             # Client factory, device/region spoof, 2FA, logout
├── notes_service.py       # instagrapi 3.2.15 wrapper + error classification
├── mpris.py               # BusctlMprisBackend / PlayerctlMprisBackend
├── anti_bot.py            # TrackDebouncer, CooldownGuard, random_jitter
└── modes/
    ├── manual.py          # Mode 1: interactive search + top-5 pick + post
    └── daemon.py          # Mode 2: signal-safe MPRIS loop + startup sync
```

## Critical `instagrapi 3.2.15` Notes

1. **Private API vs GraphQL Migration**:
   - Track search uses `cl.search_music(query)` (`music/audio_global_search/` in
     `FbSearchMixin`), **not** `cl.music_search()` (does not exist in `3.2.15`).
   - Fetching Notes uses `cl.get_notes()` (`InboxTrayRequest` /
     `xdt_get_inbox_tray_items` GraphQL), **not** `notes/get_notes/`.
   - Posting Music Notes uses `cl.create_music_note(...)`
     (`CreateInboxTrayItemRequest` / `xdt_create_inbox_tray_item` GraphQL) with
     `NoteAudience` enum (`MUTUAL_FOLLOWERS = 0`, `CLOSE_FRIENDS = 1`) and
     `alacorn_session_id` from `cl.notes_music_browser()`.
   - Overwriting an active Note directly (`delete_before_post = false`, default)
     avoids two extra API calls (`get_notes` + `delete_note`) per track change.
2. **Session Discipline**:
   - Only `setup` calls `cl.login(...)` (CAA flow + `TwoFactorRequired` retry +
     `challenge_code_handler`).
   - `manual` and `daemon` call `load_authenticated_client()` (`cl.load_settings`
     only, `with_challenge_flow=False`) and **never** perform fresh logins.
   - Fatal auth errors (`SessionExpiredError`, `CheckpointRequiredError`)
     immediately stop the daemon (`exit 1`).
   - Rate-limit errors (`RateLimitedError` from `429`, `PleaseWaitFewMinutes`,
     `FeedbackRequired`) trigger `CooldownGuard.record_rate_limit()` instead of
     retrying in a loop.

## MPRIS & Anti-Bot Details

- **MPRIS Backend**: `BusctlMprisBackend` invokes `busctl --user --json=short`
  (`ListNames` + `Properties.GetAll` on `org.mpris.MediaPlayer2.Player`) with
  strict UTF-8 decoding so non-ASCII titles are preserved without GVariant
  octal escapes. `PlayerctlMprisBackend` is available if `playerctl` is
  installed.
- **Track Filters**:
  - `allowed_url_prefixes`: Case-insensitive prefix match against `xesam:url`
    (`[]` disables).
  - `max_length_seconds`: Rejects tracks with
    `mpris:length >= max_length_seconds * 1_000_000` when `length_us > 0` (`0`
    disables).
- **Daemon Flow**:
  1. Optional one-time startup check (`check_existing_on_startup = true`)
     inspects `cl.get_notes()` + `cl.last_json` (for remote music metadata or
     `audio_cluster_id`) and compares with `.note_state.json` (`note_id`
     verified) to avoid re-posting the active song.
  2. `TrackDebouncer` requires a `"Playing"` track to remain unchanged for
     `debounce_seconds` before matching.
  3. `CooldownGuard` enforces `min_post_interval_seconds` (recommended
     `>= 120s`).
  4. `random_jitter(jitter_min_seconds, jitter_max_seconds)` runs before
     searching, followed by a post-jitter MPRIS re-check to skip tracks paused
     or skipped mid-wait.
  5. `clear_on_stop = false` keeps the latest Note active on pause/stop.

## Thai / CJK / English Text Handling

- All file I/O, subprocess calls, and JSON debug dumps use UTF-8
  (`ensure_ascii=False`).
- `normalize_display()` applies `NFC`; `normalize_match_key()` applies `NFKC` +
  `casefold()`.
- `score_track_match()` uses exact/substring matching plus character-bigram
  Dice similarity (works without spaces for Chinese/Japanese and preserves Thai
  vowels/tone marks).
- `format_note_text()` avoids slicing across `Mn`/`Mc` Unicode combining marks
  when truncating to 60 characters.

## Verification & Tooling

```bash
source .venv/bin/activate
ruff check
ruff format --check
pyright
python -m unittest discover -s tests -v
```

## Known Limitations / Improvement Ideas

- Polling via `busctl` subprocess works reliably with zero extra deps, but
  could be replaced/complemented with an event-driven `dbus-fast`
  (`PropertiesChanged`) backend in the future.
- `cl.get_notes()` (`Note` Pydantic model in `instagrapi 3.2.15`) strips music
  fields; `extract_active_music_note()` defensively scans `cl.last_json` and may
  need adjustment if Instagram changes the `InboxTrayRequest` GraphQL response
  schema.
- Live end-to-end Instagram behavior depends on external server responses and
  requires manual verification across different accounts/regions.
