"""Aggregate context excerpts must be visibly and structurally bounded."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _stage_3_write import _project_aggregate_context  # noqa: E402


class TestAggregateContextProjection(unittest.TestCase):
    def test_short_context_is_unchanged(self):
        text = "Short [[concepts/complete-link|link]] context."
        self.assertEqual(_project_aggregate_context(text, 100), text)

    def test_long_context_never_leaves_half_open_wikilink(self):
        text = "A" * 70 + " [[concepts/long-target|visible text]] tail"
        got = _project_aggregate_context(text, 90)
        self.assertIn("[Source excerpt truncated", got)
        self.assertEqual(got.count("[["), got.count("]]"))
        self.assertNotIn("[[concepts/long", got)

    def test_prefers_complete_paragraph_boundary(self):
        text = "A" * 65 + "\n\n" + "B" * 80
        got = _project_aggregate_context(text, 100)
        self.assertTrue(got.startswith("A" * 65))
        self.assertNotIn("B" * 20, got)
        self.assertTrue(got.endswith(
            "[Source excerpt truncated at a content boundary.]"))


if __name__ == "__main__":
    unittest.main()
