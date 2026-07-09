"""Redundancy fix B (2026-07-09): skip byte-identical duplicate FILE blocks in
one write loop.

delegate-mode.md documented "the pipeline can generate 2-3 redundant
source-page merge LLM-task prompts during a single ingest" and told the
operator to reuse the first merge result BY HAND. A duplicate block with
byte-identical content re-merges a page against our own just-written output —
a pure waste of an LLM merge handoff. Now enforced in code: identical
(path, content) seen earlier in THIS write loop → skip. A duplicate path with
DIFFERENT content is NOT redundant — that is the designed same-slug collision
merge and must still go through.

Stdlib unittest only.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _ingest_write import _is_redundant_duplicate_write  # noqa: E402


class DuplicateBlockSkip(unittest.TestCase):
    def test_identical_repeat_is_redundant(self):
        p = Path("/w/wiki/sources/Book/x.md")
        written = {p: "# Source page\ncontent"}
        self.assertTrue(
            _is_redundant_duplicate_write(p, "# Source page\ncontent", written))

    def test_different_content_same_path_is_designed_collision_merge(self):
        p = Path("/w/wiki/concepts/matched-filter.md")
        written = {p: "chunk-1 version"}
        self.assertFalse(
            _is_redundant_duplicate_write(p, "chunk-3 version", written))

    def test_first_write_never_skipped(self):
        p = Path("/w/wiki/concepts/new-page.md")
        self.assertFalse(_is_redundant_duplicate_write(p, "anything", {}))


if __name__ == "__main__":
    unittest.main()
