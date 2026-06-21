"""Regression test for _stage_3_2_canonicalize_sources_field.

Bug (observed 2026-06-21 on "Flexible Electronics, Volume 1 ... - Khanna.pdf"):
a raw filename containing commas was split on every comma when parsing the
existing ``sources:`` array, producing corrupted fragments, and then the full
canonical path was re-appended because no fragment matched → a 4-item array
like ``["raw/Book/Flexible Electronics", "Volume 1 Mechanical Background",
"Materials and Manufacturing - 2019 - Khanna.pdf", "raw/Book/...full...pdf"]``.

Root cause: naive ``src_text.split(",")`` ignored double-quoted strings.
Fix: use the quote-aware ``parse_frontmatter_array`` from ``_frontmatter_array``.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _stage_3_write import _stage_3_2_canonicalize_sources_field


def _sources_list(content: str) -> str:
    m = re.search(r"^sources:\s*\[(.*)\]", content, re.MULTILINE)
    return m.group(1) if m else ""


class TestCanonicalizeSources(unittest.TestCase):
    def test_comma_in_filename_stays_single_entry(self):
        canonical = ("raw/Book/Flexible Electronics, Volume 1 Mechanical "
                     "Background, Materials and Manufacturing - 2019 - Khanna.pdf")
        content = (
            "---\n"
            "type: source\n"
            f'sources: ["{canonical}"]\n'
            "---\n# Title\nbody\n"
        )
        result = _stage_3_2_canonicalize_sources_field(content, canonical)
        src = _sources_list(result)
        # The full path must remain one quoted entry.
        self.assertIn(f'"{canonical}"', src)
        # No fragment should appear as a separate quoted entry.
        self.assertNotIn('"Volume 1 Mechanical Background"', src)
        self.assertNotIn('"Materials and Manufacturing - 2019 - Khanna.pdf"', src)

    def test_canonical_source_appended_when_missing(self):
        canonical = "raw/Book/Title, With Comma.pdf"
        content = '---\ntype: source\nsources: ["raw/Book/Other.pdf"]\n---\n# T\n'
        result = _stage_3_2_canonicalize_sources_field(content, canonical)
        src = _sources_list(result)
        self.assertIn('"raw/Book/Other.pdf"', src)
        self.assertIn(f'"{canonical}"', src)

    def test_existing_comma_source_not_duplicated(self):
        canonical = "raw/Book/Title, With Comma.pdf"
        content = f'---\ntype: source\nsources: ["{canonical}"]\n---\n# T\n'
        result = _stage_3_2_canonicalize_sources_field(content, canonical)
        # Must appear exactly once (already-present detection must work for
        # comma-containing paths via the quote-aware parser).
        self.assertEqual(result.count(f'"{canonical}"'), 1)


if __name__ == "__main__":
    unittest.main()
