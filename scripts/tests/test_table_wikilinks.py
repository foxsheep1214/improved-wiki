"""Regression tests for wikilink aliases inside Markdown table cells."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _wikilinks import (  # noqa: E402
    WIKILINK_RE,
    escape_markdown_table_wikilink_aliases,
    split_wikilink_inner,
)


class TestTableAliasEscaping(unittest.TestCase):
    def test_escapes_alias_pipe_in_table_only(self):
        text = (
            "See [[concepts/motor|motor]] in prose.\n\n"
            "| Dimension | Value |\n"
            "|---|---|\n"
            "| Kind | [[concepts/motor|Brushless motor]] |\n"
        )
        out, count = escape_markdown_table_wikilink_aliases(text)
        self.assertEqual(count, 1)
        self.assertIn("[[concepts/motor|motor]] in prose", out)
        self.assertIn("[[concepts/motor\\|Brushless motor]]", out)

    def test_existing_escape_is_idempotent(self):
        text = (
            "| Dimension | Value |\n"
            "|---|---|\n"
            "| Kind | [[concepts/motor\\|Brushless motor]] |\n"
        )
        once, count = escape_markdown_table_wikilink_aliases(text)
        twice, second_count = escape_markdown_table_wikilink_aliases(once)
        self.assertEqual(once, text)
        self.assertEqual(twice, text)
        self.assertEqual(count, 0)
        self.assertEqual(second_count, 0)

    def test_table_without_outer_pipes_is_supported(self):
        text = (
            "Dimension | Value\n"
            "---|---\n"
            "Kind | [[concepts/motor|Brushless motor]]\n"
        )
        out, count = escape_markdown_table_wikilink_aliases(text)
        self.assertEqual(count, 1)
        self.assertIn("[[concepts/motor\\|Brushless motor]]", out)

    def test_table_example_inside_code_fence_is_untouched(self):
        text = (
            "```markdown\n"
            "| Dimension | Value |\n"
            "|---|---|\n"
            "| Kind | [[concepts/motor|Brushless motor]] |\n"
            "```\n"
        )
        out, count = escape_markdown_table_wikilink_aliases(text)
        self.assertEqual(out, text)
        self.assertEqual(count, 0)


class TestSharedParsing(unittest.TestCase):
    def test_regex_accepts_plain_and_escaped_alias_separator(self):
        plain = WIKILINK_RE.search("[[concepts/motor|Motor]]")
        escaped = WIKILINK_RE.search("[[concepts/motor\\|Motor]]")
        self.assertEqual(plain.groups(), ("concepts/motor", "Motor"))
        self.assertEqual(escaped.groups(), ("concepts/motor", "Motor"))

    def test_inner_split_reports_separator_style(self):
        self.assertEqual(
            split_wikilink_inner("concepts/motor|Motor"),
            ("concepts/motor", "Motor", "|"),
        )
        self.assertEqual(
            split_wikilink_inner("concepts/motor\\|Motor"),
            ("concepts/motor", "Motor", r"\|"),
        )
        self.assertEqual(
            split_wikilink_inner("concepts/motor"),
            ("concepts/motor", None, ""),
        )


if __name__ == "__main__":
    unittest.main()
