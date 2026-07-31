"""Regression tests for minerU HTML-table → Markdown conversion.

Parity gap fixed 2026-07-01: minerU emits tables as raw HTML (<table>…</table>)
in md_content, but NashSU (mineru.ts convertHtmlTablesToMarkdown) normalizes them
to Markdown tables at extraction time so the generation LLM and wiki pages never
carry raw HTML. This ports that conversion and mirrors NashSU's own test cases
(mineru.test.ts): entity decode + pipe escaping, malformed numeric entities left
intact, fenced code blocks skipped, and <img> inside cells → ![alt](src).

Stdlib unittest only — no pytest, no network, no LLM calls.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _stage_1_1_scanned import _convert_html_tables_to_markdown  # noqa: E402


class TestHtmlTableToMarkdown(unittest.TestCase):
    def test_basic_table_entities_and_pipe_escape(self):
        """NashSU mineru.test.ts: entity decode (&amp;→&) + pipe escape (1|2→1\\|2)."""
        md = "\n".join([
            "# Parsed",
            "<table>",
            "<tr><th>Name</th><th>Value</th></tr>",
            "<tr><td>A&amp;B</td><td>1|2</td></tr>",
            "</table>",
        ])
        out = _convert_html_tables_to_markdown(md)
        self.assertIn("| Name | Value |\n| --- | --- |\n| A&B | 1\\|2 |", out)
        self.assertNotIn("<table>", out)

    def test_malformed_numeric_entities_survive(self):
        """Out-of-range numeric/hex entities are left untouched, not crashed."""
        md = "\n".join([
            "<table>",
            "<tr><td>&#65;</td><td>&#9999999999;</td><td>&#x41;</td><td>&#xFFFFFFF;</td></tr>",
            "</table>",
        ])
        out = _convert_html_tables_to_markdown(md)
        self.assertIn("| A | &#9999999999; | A | &#xFFFFFFF; |", out)

    def test_fenced_code_block_not_converted(self):
        """Raw-HTML table examples inside ``` fences must survive verbatim."""
        code = "\n".join([
            "```html",
            "<table><tr><td>Keep raw</td></tr></table>",
            "```",
        ])
        md = f"{code}\n\n<table><tr><td>Convert me</td></tr></table>"
        out = _convert_html_tables_to_markdown(md)
        self.assertIn(code, out)
        self.assertIn("| Convert me |", out)

    def test_image_inside_cell_becomes_markdown_ref(self):
        """<img> inside a cell → ![alt](src) so downstream caption-inlining sees it."""
        md = "\n".join([
            "<table>",
            "<tr><th>Figure</th><th>Note</th></tr>",
            '<tr><td><img src="images/chart.png" alt="Chart"></td><td>A</td></tr>',
            "</table>",
        ])
        out = _convert_html_tables_to_markdown(md)
        self.assertIn("| ![Chart](images/chart.png) | A |", out)

    def test_ragged_rows_are_padded(self):
        """Rows with fewer cells (e.g. after rowspan flatten) are right-padded."""
        md = "\n".join([
            "<table>",
            "<tr><td>a</td><td>b</td><td>c</td></tr>",
            "<tr><td>x</td></tr>",
            "</table>",
        ])
        out = _convert_html_tables_to_markdown(md)
        self.assertIn("| a | b | c |", out)
        self.assertIn("| x |  |  |", out)

    def test_no_table_is_noop(self):
        """Plain markdown / LaTeX passes through unchanged (idempotent on 2nd run)."""
        md = "## Title\n\nInline $E=mc^2$ and a list:\n\n- one\n- two\n"
        self.assertEqual(_convert_html_tables_to_markdown(md), md)

    def test_malformed_table_left_as_is(self):
        """A <table> with no parseable rows is returned unchanged."""
        md = "<table>garbage no rows</table>"
        self.assertEqual(_convert_html_tables_to_markdown(md), md)


if __name__ == "__main__":
    unittest.main()
