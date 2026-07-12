"""Tests for _enrich_wikilinks line-level skips (fix 2026-07-12).

The enrich inserter must never place a [[wikilink]] on a heading line (^#)
or inside a fenced code block — the same line-level policy as the write-time
normalizer in _stage_3_write. Previously a term whose first occurrence sat in
an H1 or a code fence got linked there, corrupting the page.

Run:  python3 scripts/tests/test_enrich_heading_fence_skip.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _enrich_wikilinks import _replace_first_outside_links  # noqa: E402


class TestHeadingSkip(unittest.TestCase):
    def test_heading_occurrence_skipped_body_occurrence_linked(self):
        body = "# ohms law\n\nThe ohms law states V=IR.\n"
        out = _replace_first_outside_links(body, "ohms law", "[[concepts/ohms-law]]")
        self.assertEqual(out, "# ohms law\n\nThe [[concepts/ohms-law]] states V=IR.\n")

    def test_all_heading_levels_skipped(self):
        body = "## ohms law\n### ohms law\n"
        self.assertIsNone(
            _replace_first_outside_links(body, "ohms law", "[[x]]"))

    def test_only_heading_occurrence_returns_none(self):
        body = "# ohms law\n\nother text\n"
        self.assertIsNone(
            _replace_first_outside_links(body, "ohms law", "[[x]]"))


class TestCodeFenceSkip(unittest.TestCase):
    def test_fenced_occurrence_skipped_later_body_linked(self):
        body = "```python\nohms law\n```\n\nohms law in prose.\n"
        out = _replace_first_outside_links(body, "ohms law", "[[concepts/ohms-law]]")
        self.assertEqual(
            out, "```python\nohms law\n```\n\n[[concepts/ohms-law]] in prose.\n")

    def test_only_fenced_occurrence_returns_none(self):
        body = "```\nohms law\n```\nno match here\n"
        self.assertIsNone(
            _replace_first_outside_links(body, "ohms law", "[[x]]"))

    def test_tilde_fence_also_skipped(self):
        body = "~~~\nohms law\n~~~\nohms law here.\n"
        out = _replace_first_outside_links(body, "ohms law", "[[t]]")
        self.assertEqual(out, "~~~\nohms law\n~~~\n[[t]] here.\n")


class TestExistingLinkGuard(unittest.TestCase):
    """Pre-existing behavior must be preserved by the line-based rewrite."""

    def test_substring_of_existing_link_not_rewrapped(self):
        body = "See [[concepts/lead-(pd)-free-design]] and lead effects.\n"
        out = _replace_first_outside_links(body, "lead", "[[concepts/lead]]")
        self.assertEqual(
            out, "See [[concepts/lead-(pd)-free-design]] and [[concepts/lead]] effects.\n")

    def test_no_occurrence_returns_none(self):
        self.assertIsNone(_replace_first_outside_links("plain body", "absent", "[[x]]"))

    def test_only_first_occurrence_replaced(self):
        body = "term here and term there\n"
        out = _replace_first_outside_links(body, "term", "[[t]]")
        self.assertEqual(out, "[[t]] here and term there\n")


if __name__ == "__main__":
    unittest.main()
