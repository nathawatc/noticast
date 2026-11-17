"""Unicode-safe text normalization, query building, and CJK/Thai track matching."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from instagrapi.types import Track

MAX_NOTE_TEXT_LENGTH = 60

_WHITESPACE_RE = re.compile(r"\s+")
_PAREN_NOISE_RE = re.compile(
    r"\s*[\(\[【「](?:[^()\[\]【】」]*?(?:official|mv|music video|lyric|audio|live|"
    r"ver\.?|version|feat\.?|ft\.?|remaster|sped up|slowed)[^()\[\]【】」]*)"
    r"[\)\]】」]",
    re.IGNORECASE,
)
_TRAILING_FEAT_RE = re.compile(r"\s+(?:feat\.?|ft\.?)\s+.+$", re.IGNORECASE)
_TOPIC_SUFFIX_RE = re.compile(r"\s*-\s*Topic$", re.IGNORECASE)


def normalize_display(text: str) -> str:
    """Normalize text to Unicode NFC and collapse surrounding/redundant whitespace."""
    normalized = unicodedata.normalize("NFC", text or "")
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def normalize_match_key(text: str) -> str:
    """Normalize text for Thai/CJK/English comparison without stripping diacritics."""
    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def clean_artist_name(artist: str) -> str:
    """Strip common YouTube Music '- Topic' suffixes and normalize NFC."""
    cleaned = _TOPIC_SUFFIX_RE.sub("", normalize_display(artist))
    return normalize_display(cleaned)


def strip_title_noise(title: str) -> str:
    """Strip MV/Official/feat suffixes from a track title while keeping CJK/Thai."""
    cleaned = normalize_display(title)
    cleaned = _PAREN_NOISE_RE.sub("", cleaned)
    cleaned = _TRAILING_FEAT_RE.sub("", cleaned)
    return normalize_display(cleaned)


def build_search_queries(title: str, artist: str = "") -> list[str]:
    """Build deduplicated Instagram music search queries from cleanest to broadest."""
    norm_title = normalize_display(title)
    norm_artist = clean_artist_name(artist)
    clean_title = strip_title_noise(norm_title)

    candidates: list[str] = []
    if norm_title and norm_artist:
        candidates.append(f"{norm_title} {norm_artist}")
    if clean_title and norm_artist and clean_title != norm_title:
        candidates.append(f"{clean_title} {norm_artist}")
    if norm_title:
        candidates.append(norm_title)
    if clean_title and clean_title != norm_title:
        candidates.append(clean_title)

    seen: set[str] = set()
    unique: list[str] = []
    for query in candidates:
        key = normalize_match_key(query)
        if key and key not in seen:
            seen.add(key)
            unique.append(query)
    return unique


def format_note_text(
    template: str,
    *,
    title: str,
    artist: str,
    max_len: int = MAX_NOTE_TEXT_LENGTH,
) -> str:
    """Render an optional note text template and truncate within Instagram's 60-char limit."""
    if not template:
        return ""
    rendered = normalize_display(
        template.format(
            title=normalize_display(title),
            artist=clean_artist_name(artist),
        )
    )
    if len(rendered) <= max_len:
        return rendered
    if max_len <= 1:
        return rendered[:max_len]

    cut = max_len - 1
    # Never slice in the middle of a Thai/Unicode combining character cluster.
    while cut > 0 and unicodedata.category(rendered[cut]) in ("Mn", "Mc"):
        cut -= 1
    return f"{rendered[:cut].rstrip()}…"


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.replace(" ", ""), b.replace(" ", "")).ratio()


def score_track_match(track: Track, target_title: str, target_artist: str = "") -> float:
    """Score how well an Instagram Track matches an MPRIS title and artist (0..100+)."""
    cand_title = normalize_match_key(track.title)
    cand_artist = normalize_match_key(track.display_artist)
    goal_title = normalize_match_key(strip_title_noise(target_title) or target_title)
    raw_goal_title = normalize_match_key(target_title)
    goal_artist = normalize_match_key(clean_artist_name(target_artist))

    score = 0.0

    # Title matching (handles exact, substring, and character-bigram similarity for CJK/Thai)
    if cand_title and cand_title in (raw_goal_title, goal_title):
        score += 60.0
    elif cand_title and (
        goal_title in cand_title or cand_title in goal_title or raw_goal_title in cand_title
    ):
        score += 42.0
    else:
        score += 35.0 * max(
            _similarity(cand_title, goal_title),
            _similarity(cand_title, raw_goal_title),
        )

    # Artist matching
    if goal_artist:
        if cand_artist == goal_artist:
            score += 40.0
        elif goal_artist in cand_artist or cand_artist in goal_artist:
            score += 28.0
        else:
            score += 22.0 * _similarity(cand_artist, goal_artist)
    elif cand_artist and cand_artist in raw_goal_title:
        # Handle players that embed "Artist - Title" into xesam:title with empty artist
        score += 20.0

    return round(score, 2)
