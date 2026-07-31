"""Stage 2.1 must parse fenced blocks without shifting later chunk boundaries.

OCR output can contain a fence-looking language marker such as `````asm``
inside an already open `````txt`` block. It is content, not a valid closing
fence. Pairing consecutive marker lines makes every later pair off by one and
can turn hundreds of kilobytes of ordinary prose into one protected range.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _stage_2_analyze as s2  # noqa: E402


class FencedRangeParsing(unittest.TestCase):
    def test_info_marker_inside_fence_does_not_shift_later_pairs(self):
        text = (
            "before\n"
            "```txt\n"
            "alpha\n"
            "```asm\n"
            "beta\n"
            "```\n"
            "\n"
            "outside\n"
            "```txt\n"
            "gamma\n"
            "```\n"
            "after\n"
        )

        ranges = s2._stage_2_1_find_protected_ranges(text)

        self.assertEqual(2, len(ranges))
        outside_pos = text.index("outside")
        self.assertFalse(any(start <= outside_pos < end for start, end in ranges))
        self.assertIn("```asm", text[ranges[0][0]:ranges[0][1]])
        self.assertIn("gamma", text[ranges[1][0]:ranges[1][1]])

    def test_close_requires_same_marker_family_and_sufficient_length(self):
        text = (
            "````python\n"
            "alpha\n"
            "~~~\n"
            "```\n"
            "beta\n"
            "````\n"
            "outside\n"
        )

        ranges = s2._stage_2_1_find_protected_ranges(text)

        self.assertEqual(1, len(ranges))
        protected = text[ranges[0][0]:ranges[0][1]]
        self.assertIn("~~~", protected)
        self.assertIn("```\n", protected)
        self.assertNotIn("outside", protected)

    def test_invalid_internal_marker_cannot_create_oversized_chunk(self):
        outside = "outside paragraph with a clean boundary.\n\n" * 1800
        text = (
            "```txt\n"
            "alpha\n"
            "```asm\n"
            "beta\n"
            "```\n"
            + outside
            + "```txt\n"
            "gamma\n"
            "```\n"
        )

        ranges = s2._stage_2_1_find_protected_ranges(text)
        self.assertLess(max(end - start for start, end in ranges), 100)

        chunks = s2._stage_2_1_chunk_text(
            text,
            target_chars=5_000,
            overlap_chars=3_000,
            target_tokens=5_000,
        )
        self.assertGreater(len(chunks), 5)
        self.assertLess(max(map(len, chunks)), 10_000)


if __name__ == "__main__":
    unittest.main()
