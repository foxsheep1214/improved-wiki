"""Stage 2.6 Main-Arguments coverage validator (audit 2026-07-02, A9;
upgraded to hard gate 2026-07-08).

The source page's Main Arguments section is the wiki's claim ledger; H2 showed
front-prefix truncation historically produced ledgers covering only the
opening chapters (何友 baseline: 13 claims for a 19+-chapter book). The
validator now RAISES (hard gate) when the claim entry count falls below one
per technical chapter of the 2.1 outline — previously warn-only, but warns
were ignored by the agent, producing thin Main Arguments.

Stdlib unittest only.
"""
from __future__ import annotations

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


class ValidatorHardGate(unittest.TestCase):
    """A9 upgraded to hard gate (2026-07-08): raises instead of warns."""

    def test_raises_when_entries_below_technical_chapter_count(self):
        with self.assertRaises(RuntimeError) as cm:
            s26._stage_2_6_validate_main_arguments(
                _page("- **Claim:** only one claim.\n"), _OUTLINE_5CH)
        self.assertIn("1 claim entry", str(cm.exception))
        self.assertIn("3 technical chapter", str(cm.exception))

    def test_silent_when_coverage_sufficient(self):
        body = ("- **Claim:** a.\n- **Claim:** b.\n- **Claim:** c.\n")
        # Should not raise
        s26._stage_2_6_validate_main_arguments(_page(body), _OUTLINE_5CH)

    def test_silent_without_outline(self):
        # Empty outline → 0 chapters → no gate
        s26._stage_2_6_validate_main_arguments(_page(""), [])

    def test_garbage_input_does_not_raise(self):
        # No outline → 0 chapters → no gate; weird inputs are tolerated
        s26._stage_2_6_validate_main_arguments("", None)
        # [{"no_title": True}, 42] → 1 technical chapter (42 is not filtered)
        # so a page with 0 claims would raise — test with enough claims instead
        body = "- **Claim:** a.\n"
        s26._stage_2_6_validate_main_arguments(
            "## Main Arguments\n\n" + body, [{"no_title": True}, 42])


class EvidenceQualityGate(unittest.TestCase):
    """C1 (2026-07-08): evidence-anchor quality hard gate."""

    def _page(self, body: str) -> str:
        return (
            "## Main Arguments & Findings\n\n" + body + "\n"
            "## Connections to Existing Wiki\n\n- [[concepts/foo]]\n"
        )

    def test_raises_when_majority_of_evidence_lacks_anchors(self):
        body = (
            "- **Claim:** a.\n  - **Evidence:** Ch.3\n"
            "- **Claim:** b.\n  - **Evidence:** this section\n"
            "- **Claim:** c.\n  - **Evidence:** Ch.7\n"
        )
        with self.assertRaises(RuntimeError) as cm:
            s26._stage_2_6_validate_evidence_quality(self._page(body))
        self.assertIn("evidence quality LOW", str(cm.exception))

    def test_silent_when_evidence_has_specific_anchors(self):
        body = (
            "- **Claim:** a.\n  - **Evidence:** §2.3.4\n"
            "- **Claim:** b.\n  - **Evidence:** 式(3.6)\n"
            "- **Claim:** c.\n  - **Evidence:** Figure 4.2\n"
        )
        s26._stage_2_6_validate_evidence_quality(self._page(body))

    def test_silent_when_too_few_evidence_lines_to_judge(self):
        body = "- **Claim:** a.\n  - **Evidence:** Ch.3\n"
        s26._stage_2_6_validate_evidence_quality(self._page(body))

    def test_silent_when_no_main_arguments_section(self):
        s26._stage_2_6_validate_evidence_quality("## Book Summary\n\nhi\n")


if __name__ == "__main__":
    unittest.main()
