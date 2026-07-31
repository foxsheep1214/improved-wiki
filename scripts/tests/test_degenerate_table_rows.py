"""Regression tests for degenerate minerU table-row collapse.

Live failure (2026-07-02): minerU OCR emitted a single 49,412-char markdown
line — a `| 9 | POWER LOSS |`-style row whose separator carried 8,179 empty
`---` cells (a sibling book had a 24K-char one). Such lines bloat chunks,
break Read-tool line granularity for downstream agents, and carry zero
content. _collapse_degenerate_table_rows rewrites only lines that are
>2000 chars, pipe-delimited, and >=95% empty cells; everything else passes
through byte-identical.

Stdlib unittest only — no pytest, no network, no LLM calls.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _stage_1_1_scanned import _collapse_degenerate_table_rows  # noqa: E402


class TestDegenerateTableRows(unittest.TestCase):
    def test_empty_separator_line_collapsed_with_marker(self):
        """8000-empty-cell separator row collapses to the marker cell."""
        sep = "| " + " | ".join(["---"] * 8000) + " |"
        text = f"prose before\n{sep}\nprose after"
        out = _collapse_degenerate_table_rows(text)
        lines = out.split("\n")
        self.assertEqual(lines[0], "prose before")
        self.assertEqual(lines[2], "prose after")
        self.assertEqual(lines[1], "| …[degenerate row: 8000 empty cells removed] |")

    def test_sparse_data_row_keeps_content_cells(self):
        """Observed live shape: a few content cells drowning in empty cells."""
        cells = ["9", "POWER LOSS"] + [""] * 8000
        row = "| " + " | ".join(cells) + " |"
        out = _collapse_degenerate_table_rows(row)
        self.assertEqual(
            out,
            "| 9 | POWER LOSS | …[degenerate row: 8000 empty cells removed] |",
        )

    def test_normal_table_passes_through_byte_identical(self):
        """A normal 10-col table (under thresholds) is untouched."""
        header = "| " + " | ".join(f"col{i}" for i in range(10)) + " |"
        sep = "| " + " | ".join(["---"] * 10) + " |"
        body = "| " + " | ".join(f"v{i}" for i in range(10)) + " |"
        text = f"# Doc\n\n{header}\n{sep}\n{body}\n\nprose\n"
        self.assertEqual(_collapse_degenerate_table_rows(text), text)

    def test_ordinary_prose_unchanged(self):
        text = "Some prose.\n\n- a list\n- another item\n\nMore prose $E=mc^2$.\n"
        self.assertEqual(_collapse_degenerate_table_rows(text), text)

    def test_long_non_table_line_unchanged(self):
        """A >2000-char line without pipe delimiters is not a table row."""
        text = "x" * 3000 + "\nshort line"
        self.assertEqual(_collapse_degenerate_table_rows(text), text)

    def test_long_table_row_with_real_cells_unchanged(self):
        """A wide row whose cells mostly HAVE content stays byte-identical."""
        row = "| " + " | ".join(f"cell{i}" for i in range(400)) + " |"
        self.assertGreater(len(row), 2000)
        self.assertEqual(_collapse_degenerate_table_rows(row), row)


if __name__ == "__main__":
    unittest.main()
