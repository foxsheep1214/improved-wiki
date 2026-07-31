"""Regression tests for Stage 3.4 bounded page previews.

The old ``content[:1500]`` slice fabricated endings such as ``[[con`` and a
half table cell.  The review model then reported intact wiki pages as
truncated.  A bounded preview must preserve complete lines, label the omitted
middle, and show the real file tail.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _stage_3_4_review as review  # noqa: E402


class TestReviewPreviewBoundaries(unittest.TestCase):
    def test_long_page_uses_complete_lines_and_real_tail(self):
        content = (
            "# Page\n\n"
            + "complete opening line\n"
            + "[[concepts/a-very-long-link-that-must-not-be-sliced]]\n" * 30
            + "\n## See also\n\n- [[concepts/complete-tail-link]]\n"
        )

        preview = review._review_preview(content, 420)

        self.assertIn("REVIEW PREVIEW GAP", preview)
        self.assertIn("磁盘文件并未在此处结束", preview)
        self.assertTrue(preview.endswith("- [[concepts/complete-tail-link]]\n"))
        self.assertEqual(preview.count("[["), preview.count("]]"))

    def test_short_page_is_passed_through_unchanged(self):
        content = "# Complete\n\nA short page.\n"
        self.assertEqual(review._review_preview(content, 1500), content)

    def test_table_row_is_not_cut_mid_cell(self):
        long_row = "| " + "value | " * 80 + "\n"
        content = "# Comparison\n\n| A | B |\n|---|---|\n" + long_row + "\nDone.\n"

        preview = review._review_preview(content, 300)

        self.assertNotIn("value | value | value | value | value", preview)
        self.assertTrue(preview.endswith("Done.\n"))


if __name__ == "__main__":
    unittest.main()
