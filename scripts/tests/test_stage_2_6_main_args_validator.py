"""Stage 2.6 Main-Arguments coverage validator (audit 2026-07-02, A9).

The source page's Main Arguments section is the wiki's claim ledger; H2 showed
front-prefix truncation historically produced ledgers covering only the
opening chapters (何友 baseline: 13 claims for a 19+-chapter book). The
validator warns loudly — non-fatal print, never a raise — when the claim
entry count falls below one per technical chapter of the 2.1 outline.

Stdlib unittest only.
"""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _stage_2_6_source_page as s26  # noqa: E402


_OUTLINE_5CH = [
    {"chapter": 0, "title": "前言"},
    {"chapter": 1, "title": "雷达基础", "key_topics": ["方程"]},
    {"chapter": 2, "title": "MTI 与脉冲多普勒"},
    {"chapter": 3, "title": "跟踪滤波"},
    {"chapter": 4, "title": "附录 A 符号表"},
]  # → 3 technical chapters


def _page(main_args_body: str) -> str:
    return (
        "---FILE:wiki/sources/book.md---\n---\ntype: source\n---\n\n"
        "## Book Summary\n\nA book.\n\n"
        "## Main Arguments & Findings\n\n" + main_args_body + "\n"
        "## Connections to Existing Wiki\n\n- [[concepts/foo]] extends\n"
        "- [[concepts/bar]] strengthens\n"
        "---END FILE---\n"
    )


class TechnicalChapterCount(unittest.TestCase):
    def test_front_and_back_matter_excluded(self):
        self.assertEqual(s26._stage_2_6_technical_chapter_count(_OUTLINE_5CH), 3)

    def test_english_non_technical_titles_excluded(self):
        outline = [{"title": "Preface"}, {"title": "Radar Equation"},
                   {"title": "References"}, {"title": "Index of Terms"}]
        self.assertEqual(s26._stage_2_6_technical_chapter_count(outline), 2)

    def test_plain_string_entries_tolerated(self):
        self.assertEqual(
            s26._stage_2_6_technical_chapter_count(["前言", "第一章 概论"]), 1)

    def test_non_list_outline_counts_zero(self):
        self.assertEqual(s26._stage_2_6_technical_chapter_count(None), 0)
        self.assertEqual(s26._stage_2_6_technical_chapter_count("oops"), 0)


class MainArgumentsCount(unittest.TestCase):
    def test_counts_claim_labels_only_within_section(self):
        body = ("- **Claim:** SCV reaches 20 dB.\n  - **Evidence:** fig 2.6\n"
                "- **Claim:** MTI improvement factor is limited.\n"
                "- **Claim**: PD beats MTI at high PRF.\n")
        self.assertEqual(s26._stage_2_6_main_arguments_count(_page(body)), 3)

    def test_chinese_heading_and_label(self):
        page = ("## 主要论点与发现\n\n- **论点：**杂波谱展宽限制改善因子。\n"
                "- **论点：**动目标显示需时钟稳定度。\n\n## 其它\n- bullet\n")
        self.assertEqual(s26._stage_2_6_main_arguments_count(page), 2)

    def test_fallback_counts_top_level_bullets_not_sub_bullets(self):
        body = ("- claim one, no bold label\n  - evidence: ch 2\n"
                "- claim two\n  - evidence: ch 3\n")
        self.assertEqual(s26._stage_2_6_main_arguments_count(_page(body)), 2)

    def test_missing_section_counts_zero(self):
        self.assertEqual(
            s26._stage_2_6_main_arguments_count("## Book Summary\n\nhi\n"), 0)


class ValidatorWarning(unittest.TestCase):
    def _run(self, response, outline) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            s26._stage_2_6_validate_main_arguments(response, outline)
        return buf.getvalue()

    def test_warns_when_entries_below_technical_chapter_count(self):
        out = self._run(_page("- **Claim:** only one claim.\n"), _OUTLINE_5CH)
        self.assertIn("[stage 2.6][WARN]", out)
        self.assertIn("1 claim entry", out)
        self.assertIn("3 technical chapter", out)

    def test_silent_when_coverage_sufficient(self):
        body = ("- **Claim:** a.\n- **Claim:** b.\n- **Claim:** c.\n")
        self.assertEqual(self._run(_page(body), _OUTLINE_5CH), "")

    def test_silent_without_outline(self):
        out = self._run(_page(""), [])
        self.assertEqual(out, "")

    def test_never_raises_on_garbage_input(self):
        # Non-fatal contract: weird inputs must not raise.
        self._run("", None)
        self._run("no sections at all", [{"no_title": True}, 42])


if __name__ == "__main__":
    unittest.main()
