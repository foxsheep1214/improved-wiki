"""Regression: write_loop_done resume must reconstruct enrich candidates with
the SAME rel_path convention as the fresh write loop.

Bug (2026-07-01): the fresh write loop feeds enrich_wikilinks_batch
wiki_dir-relative paths (``concepts/foo.md`` — _stage_3_write strips ``wiki/``),
but the write_loop_done resume rebuilt them from files_written_paths, which are
wiki_root-relative and carry the ``wiki/`` prefix (``wiki/concepts/foo.md``).
The enrichment prompt is keyed by these paths, so the two conventions produced
different prompt hashes → the conversation router fired a spurious SECOND
enrichment handoff for the same ingest. reconstruct_enrich_candidates strips the
prefix so resume matches the fresh convention; this test locks that in.

Stdlib unittest only — no network/LLM.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _ingest_write import reconstruct_enrich_candidates  # noqa: E402

_LISTING = {"index.md", "log.md", "overview.md", "schema.md"}


class TestEnrichCandidatePathParity(unittest.TestCase):
    def test_strips_wiki_prefix_to_match_fresh_loop(self):
        wiki_dir = Path("/proj/wiki")
        files_written = [
            "wiki/concepts/foo.md",
            "wiki/entities/bar.md",
            "wiki/sources/Book/baz.md",
        ]
        got = reconstruct_enrich_candidates(files_written, wiki_dir, _LISTING)
        # Fresh loop convention: rel_path is wiki_dir-relative (no "wiki/"),
        # full_path is wiki_dir / rel_path.
        expected = [
            ("concepts/foo.md", wiki_dir / "concepts/foo.md"),
            ("entities/bar.md", wiki_dir / "entities/bar.md"),
            ("sources/Book/baz.md", wiki_dir / "sources/Book/baz.md"),
        ]
        self.assertEqual(got, expected)

    def test_full_path_unchanged_vs_wiki_root_join(self):
        # wiki_dir/rel must resolve to the same file as wiki_root/p (the old
        # code's join), so the on-disk target is unchanged by the fix.
        wiki_root = Path("/proj")
        wiki_dir = wiki_root / "wiki"
        p = "wiki/concepts/foo.md"
        (rel, full_path), = reconstruct_enrich_candidates([p], wiki_dir, _LISTING)
        self.assertEqual(full_path, wiki_root / p)
        self.assertFalse(rel.startswith("wiki/"))

    def test_listing_pages_excluded(self):
        wiki_dir = Path("/proj/wiki")
        files_written = ["wiki/index.md", "wiki/log.md", "wiki/concepts/foo.md"]
        got = reconstruct_enrich_candidates(files_written, wiki_dir, _LISTING)
        self.assertEqual(got, [("concepts/foo.md", wiki_dir / "concepts/foo.md")])

    def test_paths_without_prefix_pass_through(self):
        # Defensive: if a path somehow lacks the wiki/ prefix, keep it as-is.
        wiki_dir = Path("/proj/wiki")
        got = reconstruct_enrich_candidates(["concepts/foo.md"], wiki_dir, _LISTING)
        self.assertEqual(got, [("concepts/foo.md", wiki_dir / "concepts/foo.md")])


if __name__ == "__main__":
    unittest.main()
