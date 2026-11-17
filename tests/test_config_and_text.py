"""Tests for TOML configuration, device/region spoofing, and Thai/CJK text handling."""

from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from instagrapi.mixins.note import NoteAudience  # noqa: E402
from instagrapi.types import Track  # noqa: E402

from mpris_ig.config import ConfigError, load_config  # noqa: E402
from mpris_ig.logging_utils import log_http_debug  # noqa: E402
from mpris_ig.session import create_client  # noqa: E402
from mpris_ig.text_utils import (  # noqa: E402
    build_search_queries,
    format_note_text,
    normalize_display,
    normalize_match_key,
    score_track_match,
)


def _track(title: str, artist: str, track_id: str = "1") -> Track:
    return Track(
        id=track_id,
        title=title,
        subtitle="",
        display_artist=artist,
        audio_cluster_id=9001,
        highlight_start_times_in_ms=[10000],
        is_explicit=False,
        dash_manifest="<MPD/>",
        has_lyrics=False,
        audio_asset_id=8001,
        duration_in_ms=180000,
        allows_saving=True,
        territory_validity_periods={},
    )


class ConfigAndSpoofTests(unittest.TestCase):
    def test_load_example_config_and_apply_custom_spoof_profile(self):
        cfg = load_config(Path("config.example.toml"))

        self.assertEqual(cfg.device.model, "Pixel 8 Pro")
        self.assertEqual(cfg.device.device, "husky")
        self.assertEqual(cfg.region.locale, "en_US")
        self.assertEqual(cfg.region.country, "US")
        self.assertEqual(cfg.region.country_code, 1)
        self.assertEqual(cfg.region.timezone_offset, 0)
        self.assertEqual(cfg.region.timezone_name, "UTC")
        self.assertEqual(cfg.note.audience_enum, NoteAudience.MUTUAL_FOLLOWERS)
        self.assertFalse(cfg.note.clear_on_stop)
        self.assertFalse(cfg.logging.verbose)
        self.assertEqual(cfg.mpris.allowed_url_prefixes, ("https://music.youtube.com",))
        self.assertEqual(cfg.mpris.max_length_seconds, 420)

        with tempfile.TemporaryDirectory() as tmpdir:
            custom_path = Path(tmpdir) / "custom.toml"
            custom_path.write_text(
                '[device]\nmodel = "SM-S918B"\ndevice = "dm3q"\ncpu = "qcom"\n'
                'manufacturer = "samsung"\nresolution = "1440x3088"\ndpi = "480dpi"\n'
                '[region]\nlocale = "ja_JP"\ncountry = "JP"\ncountry_code = 81\n'
                'timezone_offset = 32400\ntimezone_name = "Asia/Tokyo"\n'
                "[mpris]\nallowed_url_prefixes = []\nmax_length_seconds = 0\n",
                encoding="utf-8",
            )
            custom_cfg = load_config(custom_path)
            self.assertEqual(custom_cfg.mpris.allowed_url_prefixes, ())
            self.assertEqual(custom_cfg.mpris.max_length_seconds, 0)

            cl = create_client(custom_cfg, apply_profile=True)
            settings = cl.get_settings()
            self.assertEqual(settings["locale"], "ja_JP")
            self.assertEqual(settings["country"], "JP")
            self.assertEqual(settings["country_code"], 81)
            self.assertEqual(settings["timezone_name"], "Asia/Tokyo")
            self.assertIn("SM-S918B; dm3q; qcom; ja_JP;", settings["user_agent"])

    def test_invalid_jitter_range_raises_config_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_cfg = Path(tmpdir) / "bad.toml"
            bad_cfg.write_text(
                "[anti_bot]\njitter_min_seconds = 10.0\njitter_max_seconds = 2.0\n",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_config(bad_cfg)

    def test_invalid_text_template_raises_config_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_cfg = Path(tmpdir) / "bad_tpl.toml"
            bad_cfg.write_text(
                '[note]\ntext_template = "{unknown_field}"\n',
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_config(bad_cfg)

    def test_directory_config_path_raises_config_error(self):
        with tempfile.TemporaryDirectory() as tmpdir, self.assertRaises(ConfigError):
            load_config(Path(tmpdir))

    def test_non_list_url_prefixes_raises_config_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_cfg = Path(tmpdir) / "bad_type.toml"
            bad_cfg.write_text(
                '[mpris]\nallowed_url_prefixes = "https://music.youtube.com"\n',
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_config(bad_cfg)


class CjkAndThaiTextTests(unittest.TestCase):
    def test_thai_chinese_japanese_english_normalization_and_queries(self):
        # Thai with combining tone marks, Chinese, Japanese full-width -> NFC/NFKC
        self.assertEqual(normalize_display("แค่\u0e48มีเธอ  "), "แค่\u0e48มีเธอ")
        self.assertEqual(normalize_match_key("\uff21\uff22\uff23 夜曲"), "abc 夜曲")

        queries_th = build_search_queries("แค่มีเธอ (Official MV)", "Three Man Down - Topic")
        self.assertEqual(
            queries_th,
            [
                "แค่มีเธอ (Official MV) Three Man Down",
                "แค่มีเธอ Three Man Down",
                "แค่มีเธอ (Official MV)",
                "แค่มีเธอ",
            ],
        )

        queries_zh = build_search_queries("夜曲 (Official Video)", "周杰倫")
        self.assertIn("夜曲 周杰倫", queries_zh)

    def test_score_track_match_prefers_exact_thai_and_cjk_tracks(self):
        exact_th = _track("ถ้าเธอรักฉันจริง", "Three Man Down", "1")
        wrong_th = _track("ฝันถึงแฟนเก่า", "Three Man Down", "2")
        self.assertGreater(
            score_track_match(exact_th, "ถ้าเธอรักฉันจริง", "Three Man Down"),
            score_track_match(wrong_th, "ถ้าเธอรักฉันจริง", "Three Man Down"),
        )

        exact_zh = _track("晴天", "周杰倫", "3")
        cover_zh = _track("晴天 (Cover)", "Other Artist", "4")
        self.assertGreater(
            score_track_match(exact_zh, "晴天", "周杰倫"),
            score_track_match(cover_zh, "晴天", "周杰倫"),
        )

        exact_jp = _track("夜に駆ける", "YOASOBI", "5")
        self.assertGreaterEqual(score_track_match(exact_jp, "夜に駆ける", "YOASOBI"), 95.0)

    def test_format_note_text_preserves_utf8_and_truncates_safely(self):
        rendered = format_note_text(
            "🎵 {title} - {artist}", title="ถ้าเธอรักฉันจริง", artist="Three Man Down"
        )
        self.assertEqual(rendered, "🎵 ถ้าเธอรักฉันจริง - Three Man Down")
        self.assertLessEqual(len(rendered), 60)

        long_rendered = format_note_text(
            "Now Playing: {title} — {artist}",
            title="这是一首非常非常非常非常长的中文歌曲名字用来测试超过六十个字符时的安全截断效果",
            artist="周杰倫 Jay Chou",
        )
        self.assertLessEqual(len(long_rendered), 60)
        self.assertTrue(long_rendered.endswith("…"))

        # Thai combining mark safety: slicing at max_len=5 must not leave orphan base consonant
        thai_cut = format_note_text("{title}", title="ก้ำกึ่งมาก", artist="", max_len=5)
        self.assertLessEqual(len(thai_cut), 5)
        self.assertTrue(thai_cut.endswith("…"))

        # ZWJ emoji cluster safety: must not split inside 👨‍👩‍👧
        emoji_cut = format_note_text("{title}", title="a👨‍👩‍👧b", artist="", max_len=5)
        self.assertEqual(emoji_cut, "a…")

    def test_score_track_match_rejects_short_substring_and_handles_swapped_artists(self):
        one_char = _track("a", "Other", "10")
        self.assertLess(score_track_match(one_char, "All Too Well", "Taylor Swift"), 25.0)

        swapped = _track("Collab Song", "Artist B, Artist A", "11")
        wrong_artist = _track("Collab Song", "Completely Different", "12")
        self.assertGreater(
            score_track_match(swapped, "Collab Song", "Artist A & Artist B"),
            score_track_match(wrong_artist, "Collab Song", "Artist A & Artist B"),
        )
        self.assertGreater(score_track_match(swapped, "Collab Song", "Artist A & Artist B"), 75.0)

    def test_verbose_http_debug_logs_unescaped_thai_and_cjk_only_in_debug(self):
        cl = mock.MagicMock()
        cl.last_response = mock.Mock(status_code=200, url="https://i.instagram.com/graphql/query")
        cl.last_json = {"text": "กำลังฟัง 周杰倫"}

        logger = logging.getLogger("test_debug_toggle")
        logger.handlers.clear()
        logger.propagate = False

        records: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(self.format(record))

        logger.addHandler(_Capture())
        logger.setLevel(logging.INFO)
        log_http_debug(logger, cl, "test")
        self.assertEqual(records, [])

        logger.setLevel(logging.DEBUG)
        log_http_debug(logger, cl, "test")
        joined = "\n".join(records)
        self.assertIn("กำลังฟัง 周杰倫", joined)
        self.assertNotIn("\\u0e01", joined)


if __name__ == "__main__":
    unittest.main()
