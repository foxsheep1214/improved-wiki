"""Regression tests for post-write disk reconstruction + --delete orphan sweep.

Stdlib `unittest` only — no pytest, no network, no LLM calls.

Covers the 2026-06-25 Orin re-ingest findings:

  Cluster (#3/#4/#5): on a write_phase/write_loop_done resume, file_blocks is []
  so Stage 3.4 re-fired over "0 pages", validation reported false "0 FILE blocks"
  failures, and cache stats were zeroed. _reconstruct_blocks_from_disk rebuilds
  the real page set from files_written_paths so all three see the true pages.

  #2: --delete left source-specific query/comparison pages behind, so a
  re-ingest stacked stale duplicates. _cleanup_orphan_pages now sweeps queries
  and comparisons too (but preserves sources:[] disambiguation comparisons).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _core  # noqa: E402
import _ingest_write  # noqa: E402
import _source_lifecycle  # noqa: E402


def _make_config(tmp: Path) -> _core.Config:
    return _core.Config(
        wiki_root=tmp / "wiki", raw_root=tmp / "raw",
        wiki_dir=tmp / "wiki" / "wiki", runtime_dir=tmp / "rt",
        cache_path=tmp / "rt" / "ingest-cache.json",
        progress_dir=tmp / "rt" / "ingest-progress",
        extract_tmp_dir=tmp / "rt" / "extract-tmp",
        llm_base_url="https://example.invalid", llm_model="m", llm_api_key="",
        llm_protocol="anthropic", caption_api_key="", caption_base_url="x",
        caption_model="c", chunk_size=60000, chunk_overlap=3000,
        source_budget=100000, target_chars=60000, target_tokens=30000,
        max_tokens=8192, conversation_prefix="ab12cd34",
    )


class TestReconstructBlocksFromDisk(unittest.TestCase):
    def test_rebuilds_wiki_dir_relative_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            # wiki_root is tmp/wiki; pages live under wiki_root/wiki/<type>/.
            for rel in ["wiki/concepts/tmp.md", "wiki/entities/orin.md",
                        "wiki/sources/AppNote/x.md"]:
                p = cfg.wiki_root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(f"# {rel}\nbody", encoding="utf-8")
            files_written = ["wiki/concepts/tmp.md", "wiki/entities/orin.md",
                             "wiki/sources/AppNote/x.md"]

            blocks = _ingest_write._reconstruct_blocks_from_disk(cfg, files_written)

            paths = sorted(p for p, _ in blocks)
            # Paths are wiki_dir-relative (no leading "wiki/"), matching the
            # file_blocks convention validation/3.4 expect.
            self.assertEqual(
                paths, ["concepts/tmp.md", "entities/orin.md", "sources/AppNote/x.md"])
            self.assertEqual(len(blocks), 3)
            self.assertTrue(any("concepts/" in p for p, _ in blocks))
            self.assertTrue(all(c for _, c in blocks))  # content non-empty

    def test_skips_missing_files(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            p = cfg.wiki_root / "wiki/concepts/real.md"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x", encoding="utf-8")
            blocks = _ingest_write._reconstruct_blocks_from_disk(
                cfg, ["wiki/concepts/real.md", "wiki/concepts/ghost.md"])
            self.assertEqual([p for p, _ in blocks], ["concepts/real.md"])

    def test_empty_when_nothing_written(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            self.assertEqual(_ingest_write._reconstruct_blocks_from_disk(cfg, []), [])


class TestDeleteSweepsQueriesAndComparisons(unittest.TestCase):
    def _write(self, cfg, rel, sources):
        p = cfg.wiki_root / "wiki" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        src_yaml = "[" + ", ".join(f'"{s}"' for s in sources) + "]"
        p.write_text(f"---\ntype: x\nsources: {src_yaml}\n---\nbody\n", encoding="utf-8")
        return p

    def test_orphan_query_and_comparison_removed_disambiguation_kept(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            stem = "NVIDIA - Jetson AGX Orin Thermal Design Guide - TDG-10943-001"
            src = f"raw/Applicationnote/{stem}.pdf"

            q = self._write(cfg, "queries/ttp-measure.md", [src])           # orphan
            c_in = self._write(cfg, "comparisons/sw-vs-hw.md", [src])        # orphan
            c_dis = self._write(cfg, "comparisons/switch-disambig.md", [])   # sources:[] -> keep
            c_shared = self._write(cfg, "comparisons/shared.md",
                                   [src, "raw/Book/other.pdf"])              # multi-source -> keep
            con = self._write(cfg, "concepts/tmp.md", [src])                 # orphan concept

            removed = _source_lifecycle._cleanup_orphan_pages(cfg.wiki_root, stem)

            self.assertFalse(q.exists(), "orphan query should be deleted")
            self.assertFalse(c_in.exists(), "orphan in-source comparison should be deleted")
            self.assertFalse(con.exists(), "orphan concept should be deleted")
            self.assertTrue(c_dis.exists(), "sources:[] disambiguation must be kept")
            self.assertTrue(c_shared.exists(), "multi-source comparison must be kept")
            self.assertEqual(removed, 3)


class TestPreserveStageCounters(unittest.TestCase):
    """Cache stage counters must not regress on write_phase-resume passes.

    Regression for 2026-06-25 Fardo re-ingest: the resume pass carried empty
    chunk_analyses (2.x short-circuit), so _do_write rebuilt stages with
    chunks_analyzed=0, overwriting the real first-pass count of 4 — tripping
    validate_ingest's '0 chunk(s) analyzed' check (18/19 false-fail). Same
    class as Orin #5 'cache stats zeroed'.
    """

    def test_first_write_unchanged(self):
        new = {"chunks_analyzed": 4, "concepts_generated": 10,
               "coverage_pct": 0.9, "review_items": 7}
        out = _ingest_write._preserve_stage_counters({}, new)
        self.assertEqual(out["chunks_analyzed"], 4)
        self.assertEqual(out["concepts_generated"], 10)
        self.assertEqual(out["review_items"], 7)

    def test_resume_preserves_nonzero_counters(self):
        prev = {"chunks_analyzed": 4, "concepts_generated": 10,
                "images_extracted": 309, "review_items": 7}
        new = {"chunks_analyzed": 0, "concepts_generated": 0,
               "images_extracted": 0, "review_items": 0}
        out = _ingest_write._preserve_stage_counters(prev, new)
        self.assertEqual(out["chunks_analyzed"], 4)
        self.assertEqual(out["concepts_generated"], 10)
        self.assertEqual(out["images_extracted"], 309)
        self.assertEqual(out["review_items"], 7)

    def test_resume_new_higher_wins(self):
        prev = {"chunks_analyzed": 3, "concepts_generated": 5}
        new = {"chunks_analyzed": 6, "concepts_generated": 5}
        out = _ingest_write._preserve_stage_counters(prev, new)
        self.assertEqual(out["chunks_analyzed"], 6)

    def test_coverage_ratios_keep_new_value(self):
        prev = {"coverage_pct": 0.95, "chunks_analyzed": 4}
        new = {"coverage_pct": 0.8, "chunks_analyzed": 0}
        out = _ingest_write._preserve_stage_counters(prev, new)
        self.assertEqual(out["coverage_pct"], 0.8)  # ratio: new wins, not max
        self.assertEqual(out["chunks_analyzed"], 4)  # counter: preserved


if __name__ == "__main__":
    unittest.main()
