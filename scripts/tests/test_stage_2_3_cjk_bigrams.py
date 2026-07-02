"""Stage 2.3 CJK character-bigram matching (audit 2026-07-02, A4/H1 layer 2).

_stage_2_title_words keeps only [a-z0-9], so pure-CJK titles tokenized to the
EMPTY set and the Jaccard branch (both sides non-empty) never fired — Chinese
concepts had no non-exact dedup path at all (匹配滤波 ×5 pages coexisting,
每晚新书放大). Fix: a SEPARATE CJK bigram token set + its own Jaccard branch,
so pure/mostly-CJK titles match while mixed CJK+Latin titles keep their
existing ASCII-token matches undiluted. Acronym guard and exact slug-form
matching are unchanged (see test_stage_2_3_acronym_guard.py).

Stdlib unittest only.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _stage_2_3_incremental as s23  # noqa: E402
from _stage_2_base import _stage_2_title_cjk_bigrams  # noqa: E402


def _write_page(wiki: Path, type_dir: str, slug: str, title: str) -> None:
    (wiki / type_dir).mkdir(parents=True, exist_ok=True)
    (wiki / type_dir / f"{slug}.md").write_text(
        f"---\ntype: {type_dir[:-1]}\ntitle: \"{title}\"\n---\n\nbody\n",
        encoding="utf-8",
    )


class CjkBigramTokens(unittest.TestCase):
    def test_bigrams_over_cjk_run(self):
        self.assertEqual(
            _stage_2_title_cjk_bigrams("匹配滤波器理论"),
            {"匹配", "配滤", "滤波", "波器", "器理", "理论"},
        )

    def test_length_one_run_keeps_single_char(self):
        self.assertEqual(_stage_2_title_cjk_bigrams("波 beam"), {"波"})

    def test_ascii_only_title_yields_empty_set(self):
        self.assertEqual(_stage_2_title_cjk_bigrams("Kalman Filter"), set())

    def test_mixed_title_bigrams_ignore_latin(self):
        self.assertEqual(
            _stage_2_title_cjk_bigrams("匹配滤波器 (Matched Filter)"),
            {"匹配", "配滤", "滤波", "波器"},
        )


class Stage23CjkNearDupeDetection(unittest.TestCase):
    def test_cjk_near_dupe_now_matched(self):
        """匹配滤波器理论 must match the existing 匹配滤波器-matched-filter page
        (previously: empty ASCII token set → only exact slug match possible)."""
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d)
            _write_page(wiki, "concepts", "匹配滤波器-matched-filter",
                        "匹配滤波器 (Matched Filter)")
            chunks = [{"concepts_found": [{"name": "匹配滤波器理论"}],
                       "entities_found": []}]
            assoc = s23.stage_2_3_detect_incremental_associations(wiki, chunks)
            self.assertIn("匹配滤波器理论", assoc)
            self.assertIn("匹配滤波器-matched-filter", assoc["匹配滤波器理论"])

    def test_unrelated_cjk_titles_not_matched(self):
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d)
            _write_page(wiki, "concepts", "匹配滤波器-matched-filter",
                        "匹配滤波器 (Matched Filter)")
            chunks = [{"concepts_found": [{"name": "脉冲压缩原理"}],
                       "entities_found": []}]
            assoc = s23.stage_2_3_detect_incremental_associations(wiki, chunks)
            self.assertNotIn("脉冲压缩原理", assoc)

    def test_partial_cjk_overlap_below_threshold_not_matched(self):
        """Sharing a common prefix (雷达) alone must not collapse two concepts."""
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d)
            _write_page(wiki, "concepts", "雷达发射机", "雷达发射机")
            chunks = [{"concepts_found": [{"name": "雷达距离方程"}],
                       "entities_found": []}]
            assoc = s23.stage_2_3_detect_incremental_associations(wiki, chunks)
            self.assertNotIn("雷达距离方程", assoc)

    def test_mixed_title_ascii_match_undiluted(self):
        """A mixed CJK+Latin name must still match an English-titled page via
        the ASCII branch — the CJK tokens live in a separate set and must not
        drag the combined Jaccard below threshold."""
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d)
            _write_page(wiki, "concepts", "kalman-filter", "Kalman Filter")
            chunks = [{"concepts_found": [{"name": "卡尔曼滤波 Kalman Filter"}],
                       "entities_found": []}]
            assoc = s23.stage_2_3_detect_incremental_associations(wiki, chunks)
            self.assertIn("卡尔曼滤波 Kalman Filter", assoc)
            self.assertIn("kalman-filter", assoc["卡尔曼滤波 Kalman Filter"])

    def test_cjk_suffix_variant_matched(self):
        """卡尔曼滤波 vs 卡尔曼滤波器 (器-suffix variant) — the audit's live
        duplicate pair — must now be flagged."""
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d)
            _write_page(wiki, "concepts", "卡尔曼滤波器", "卡尔曼滤波器")
            chunks = [{"concepts_found": [{"name": "卡尔曼滤波"}],
                       "entities_found": []}]
            assoc = s23.stage_2_3_detect_incremental_associations(wiki, chunks)
            self.assertIn("卡尔曼滤波", assoc)
            self.assertIn("卡尔曼滤波器", assoc["卡尔曼滤波"])


if __name__ == "__main__":
    unittest.main()
