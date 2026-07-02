"""Tests for the A7 backlink fixes (audit H5, 2026-07-02).

queries and comparisons were zero-inlink islands: 2.9 runs after the 2.6
source page so nothing linked the comparison pages, and queries/index.md was
a lint stub. Covers stage_2_9_append_source_backlinks (source page gets a
`## Comparisons` section while blocks are in memory) and
_stage_2_7_queries_index_block (real queries/index.md listing block —
created when missing/stub, appended otherwise). No network.

Run:  python3 scripts/tests/test_queries_index_backlinks.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _ingest_prepare as prep  # noqa: E402
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


_Q1 = ("queries/why-x.md", '---\ntype: query\ntitle: "Why X?"\n---\nbody')
_Q2 = ("wiki/queries/how-y.md", '---\ntype: query\ntitle: "How Y?"\n---\nbody')

_LINT_STUB = ('---\ntype: query\ntitle: "index"\ntags: [stub, lint]\nrelated: []\n'
              'sources: []\n---\n\n# index\n\nCreated by Wiki Lint as a placeholder.\n')


class TestQueriesIndexBlock(unittest.TestCase):
    def _config(self, tmp):
        return SimpleNamespace(wiki_dir=Path(tmp))

    def test_creates_fresh_index_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            block = prep._stage_2_7_queries_index_block([_Q1, _Q2], self._config(tmp))
        self.assertIsNotNone(block)
        path, content = block
        self.assertEqual(path, "queries/index.md")
        self.assertIn("# Queries Index", content)
        self.assertIn("- [[queries/why-x]] — Why X?", content)
        self.assertIn("- [[queries/how-y]] — How Y?", content)

    def test_replaces_lint_stub(self):
        with tempfile.TemporaryDirectory() as tmp:
            qdir = Path(tmp) / "queries"
            qdir.mkdir(parents=True)
            (qdir / "index.md").write_text(_LINT_STUB, encoding="utf-8")
            _, content = prep._stage_2_7_queries_index_block([_Q1], self._config(tmp))
        self.assertNotIn("stub", content)
        self.assertIn("# Queries Index", content)
        self.assertIn("- [[queries/why-x]] — Why X?", content)

    def test_appends_only_new_slugs_to_real_index(self):
        existing = "# Queries Index\n\n- [[queries/why-x]] — Why X?\n"
        with tempfile.TemporaryDirectory() as tmp:
            qdir = Path(tmp) / "queries"
            qdir.mkdir(parents=True)
            (qdir / "index.md").write_text(existing, encoding="utf-8")
            _, content = prep._stage_2_7_queries_index_block([_Q1, _Q2], self._config(tmp))
        self.assertEqual(content.count("[[queries/why-x]]"), 1)
        self.assertIn("- [[queries/how-y]] — How Y?", content)

    def test_returns_none_when_all_already_listed(self):
        existing = "# Queries Index\n\n- [[queries/why-x]] — Why X?\n"
        with tempfile.TemporaryDirectory() as tmp:
            qdir = Path(tmp) / "queries"
            qdir.mkdir(parents=True)
            (qdir / "index.md").write_text(existing, encoding="utf-8")
            block = prep._stage_2_7_queries_index_block([_Q1], self._config(tmp))
        self.assertIsNone(block)

    def test_returns_none_without_query_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            block = prep._stage_2_7_queries_index_block(
                [("concepts/a.md", "x"), ("queries/index.md", "y")], self._config(tmp))
        self.assertIsNone(block)


if __name__ == "__main__":
    unittest.main()
