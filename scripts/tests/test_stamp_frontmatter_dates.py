"""Tests for stamp_frontmatter_dates — frontmatter round-trip cleanliness.

Regression: stamp_frontmatter_dates used ``content[3:end]`` to slice the
frontmatter body, but index 3 is the ``\n`` after the opening ``---`` — so
the body carried a leading newline and re-serialization produced
``---\\n\\ntype: source`` (a blank line after the fence). Called twice
(Stage 2.0 then Stage 3 write) it stacked two blank lines, breaking YAML
frontmatter parsing.
"""
import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _stage_3_write as s3  # noqa: E402


FM = """---
type: source
title: "X"
domain: general
created: 2026-06-20
updated: 2026-06-20
---

## Body
text"""


class TestStampFrontmatterDates(unittest.TestCase):
    def test_no_blank_line_after_opening_fence(self):
        out = s3.stamp_frontmatter_dates(FM, "2026-06-20")
        self.assertTrue(out.startswith("---\ntype: source"),
                        f"expected no blank line after ---; got: {out[:30]!r}")

    def test_idempotent_no_stacked_blank_lines(self):
        once = s3.stamp_frontmatter_dates(FM, "2026-06-20")
        twice = s3.stamp_frontmatter_dates(once, "2026-06-20")
        self.assertTrue(twice.startswith("---\ntype: source"),
                        f"second stamp stacked blank lines: {twice[:30]!r}")
        head = twice.split("\n---", 1)[0]
        self.assertNotIn("\n\n\n", head)

    def test_dates_stamped(self):
        out = s3.stamp_frontmatter_dates(
            "---\ntype: source\ntitle: X\n---\nbody", "2026-06-20")
        self.assertIn("created: 2026-06-20", out)
        self.assertIn("updated: 2026-06-20", out)

    def test_preserves_body(self):
        out = s3.stamp_frontmatter_dates(FM, "2026-06-20")
        self.assertIn("## Body\ntext", out)


if __name__ == "__main__":
    unittest.main()
