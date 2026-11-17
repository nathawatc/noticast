# AGENTS.md

Technical reference and contributor/agent guidelines for **noticast** (`v0.1.0`).

## Status & Scope

- **Stage**: `v0.1.0` internal testing; interfaces and defaults may evolve.
- **Environment Rule**: Always work inside a Python `3.14+` virtual environment
  (`.venv`). Never install packages globally.
- **Privacy Rule**: Never commit `session.json`, `config.toml`,
  `.note_state.json`, or personal device identifiers / real timezone / locale
  defaults into `config.example.toml`, `src/noticast/config.py`, or `tests/`.

## Architecture (`src/noticast/`)

```text
main.py                    # Launcher adding src/ to sys.path
src/noticast/
├── cli.py                 # Argument parsing, --config / -v / -q, dispatch
├── config.py              # Pydantic schema + tomllib loader + path resolution
├── logging_utils.py       # INFO/DEBUG logger + UTF-8 HTTP/JSON debug output
├── text_utils.py          # NFC/NFKC normalization, rapidfuzz, grapheme 60-char
├── state.py               # .note_state.json cache + atomic temp-file writer
├── session.py             # Client factory, device/region spoof, 2FA, logout
├── notes_service.py       # instagrapi 3.2.15 wrapper, match cache, error handling
├── mpris.py               # DbusMprisBackend (dbus-fast) with player priority
├── anti_bot.py            # TrackDebouncer (24h TTL), CooldownGuard, random_jitter
└── modes/
    ├── clear.py           # One-shot Note + state cleanup
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

- **MPRIS Backend**: `DbusMprisBackend` queries the session bus natively via
  `dbus-fast` (`ListNames` + `Properties.GetAll` on
  `org.mpris.MediaPlayer2.Player`), preserving full UTF-8 metadata,
  respecting `allowed_players` order before alphabetical fallback, and waking
  on `PropertiesChanged` / `NameOwnerChanged` signals with polling fallback.
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
     `debounce_seconds` before matching, and expires its last-posted guard after
     24 hours (`86400s`).
  3. `CooldownGuard` enforces `min_post_interval_seconds` (recommended
     `>= 120s`), rate-limit cooldowns, and capped exponential backoff on
     transient `NotesApiError`.
  4. `random_jitter(jitter_min_seconds, jitter_max_seconds)` runs before
     searching, followed by a post-jitter MPRIS re-check to skip tracks paused
     or skipped mid-wait.
  5. `clear_on_stop = false` keeps the latest Note active on pause/stop.

## Thai / CJK / English Text Handling

- All file I/O and JSON debug dumps use UTF-8 (`ensure_ascii=False`).
- `normalize_display()` applies `NFC`; `normalize_match_key()` applies `NFKC` +
  `casefold()`.
- `score_track_match()` combines exact/substring matching (including 2-char
  non-ASCII and word-bounded short titles) with `rapidfuzz.fuzz.token_set_ratio`.
- `format_note_text()` uses `grapheme` UAX #29 clusters so truncation to 60
  characters never slices across combining marks or ZWJ emojis.

## Verification & Tooling

Type checking uses `basedpyright` configured in `pyrightconfig.json`
(`typeCheckingMode = "recommended"`).

```bash
source .venv/bin/activate
ruff check
ruff format --check main.py src tests
basedpyright
python -m unittest discover -s tests -v
rumdl check
```

## Known Limitations / Improvement Ideas

- `cl.get_notes()` (`Note` Pydantic model in `instagrapi 3.2.15`) strips music
  fields; `extract_active_music_note()` defensively scans `cl.last_json` and may
  need adjustment if Instagram changes the `InboxTrayRequest` GraphQL response
  schema.
- Live end-to-end Instagram behavior depends on external server responses and
  requires manual verification across different accounts/regions.
