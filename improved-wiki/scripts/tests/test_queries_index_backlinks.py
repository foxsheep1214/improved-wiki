"""Tests for the A7 comparison backlink fix (audit H5, 2026-07-02).

comparisons were zero-inlink islands: 2.9 runs after the 2.6 source page so
nothing linked the comparison pages. Covers stage_2_9_append_source_backlinks
(source page gets a `## Comparisons` section while blocks are in memory).
No network.

(The former TestQueriesIndexBlock class was removed 2026-07-12 with Stage 2.7
— ingest no longer generates query pages or maintains queries/index.md.)

Run:  python3 scripts/tests/test_queries_index_backlinks.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _stage_2_9_comparison import stage_2_9_append_source_backlinks  # noqa: E402

_SOURCE_BLOCK = ("sources/book.md", '---\ntype: source\ntitle: "Book"\n---\n\n# Book\n\nbody\n')
_COMP_BLOCK = ("comparisons/a-vs-b.md", '---\ntype: comparison\ntitle: "A vs B"\n---\nbody')


class TestComparisonSourceBacklinks(unittest.TestCase):
    def test_appends_comparisons_section_to_source_block(self):
        blocks = [_SOURCE_BLOCK, ("concepts/a.md", "x"), _COMP_BLOCK]
        result = stage_2_9_append_source_backlinks(blocks, [_COMP_BLOCK])
        source = dict(result)["sources/book.md"]
        self.assertIn("## Comparisons", source)
        self.assertIn("- [[comparisons/a-vs-b]] — A vs B", source)
        # Other blocks untouched; input list not mutated.
        self.assertEqual(dict(result)["concepts/a.md"], "x")
        self.assertNotIn("## Comparisons", _SOURCE_BLOCK[1])

    def test_wiki_prefixed_source_path_matched(self):
        blocks = [("wiki/sources/book.md", _SOURCE_BLOCK[1]), _COMP_BLOCK]
        result = stage_2_9_append_source_backlinks(blocks, [_COMP_BLOCK])
        self.assertIn("## Comparisons", dict(result)["wiki/sources/book.md"])

    def test_no_comp_blocks_is_noop(self):
        blocks = [_SOURCE_BLOCK]
        self.assertEqual(stage_2_9_append_source_backlinks(blocks, []), blocks)

    def test_missing_source_block_leaves_blocks_unchanged(self):
        blocks = [("concepts/a.md", "x"), _COMP_BLOCK]
        self.assertEqual(stage_2_9_append_source_backlinks(blocks, [_COMP_BLOCK]), blocks)

    def test_untitled_comparison_falls_back_to_stem(self):
        comp = ("comparisons/x-vs-y.md", "no frontmatter body")
        result = stage_2_9_append_source_backlinks([_SOURCE_BLOCK, comp], [comp])
        self.assertIn("- [[comparisons/x-vs-y]] — x-vs-y", dict(result)["sources/book.md"])


if __name__ == "__main__":
    unittest.main()
