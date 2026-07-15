"""Regression tests for Stage 0.2 skip logic and per-book finalization.

Stdlib `unittest` only — no pytest, no network, no LLM calls.

Covers two 2026-06-25 audit findings:

  Finding 1 — batch/queue path skipped Stage 3.7 (embeddings) + 4.1
  (validation) and never set the ingested marker, because that tail lived
  only in ingest_one. _finalize_book now centralizes it; both ingest_one and
  batch_ingest call it. Test: _finalize_book runs embed→validate→mark in order.

  Finding 2 — _stage_0_2_should_skip carried ~60 lines of unreachable code
  (a wikilink-completeness check after an unconditional return). Removed; the
  ingested marker is the single completeness signal. Test: the four
  marker/source-page states resolve to the right skip/resume decision.

  Finding 3 (2026-07-15) — deep-research query bridges (raw/queries/*.md)
  deliberately get no Stage 2.6 source page (wiki/queries/<slug>.md is
  already the canonical artifact). The source-page-existence staleness
  check in _stage_0_2_should_skip doesn't know that, so every call saw
  "no source page" and force-cleared the ingested marker — an endless
  re-ingest loop regenerating duplicate concepts/entities on every run.
  Fixed by special-casing is_query_bridge_source() paths to trust the
  ingested marker alone, skipping the source-page check entirely.
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
import ingest  # noqa: E402
from _ingest_skip import _stage_0_2_should_skip  # noqa: E402
from _stage_3_write import _stage_3_1_wiki_path_for_source  # noqa: E402


def _make_config(tmp: Path) -> _core.Config:
    return _core.Config(
        wiki_root=tmp / "wiki", raw_root=tmp / "raw",
        wiki_dir=tmp / "wiki", runtime_dir=tmp / "rt",
        cache_path=tmp / "rt" / "ingest-cache.json",
        progress_dir=tmp / "rt" / "ingest-progress",
        extract_tmp_dir=tmp / "rt" / "extract-tmp",
        llm_model="m",
        caption_api_key="", caption_base_url="x",
        caption_model="c", chunk_overlap=3000,
        source_budget=100000, target_chars=60000, target_tokens=30000,
        max_tokens=8192, conversation_prefix="ab12cd34",
    )


def _raw_file(tmp: Path) -> Path:
    raw = tmp / "raw" / "Book" / "x.pdf"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"%PDF-1.4 fake")
    return raw


def _raw_query_bridge_file(tmp: Path) -> Path:
    raw = tmp / "raw" / "queries" / "research-x-2026-07-15-000000.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("---\ntype: query\n---\n# x\n", encoding="utf-8")
    return raw


class TestFinalizeBook(unittest.TestCase):
    """_finalize_book = embeddings → ingested marker, in order.

    The post-ingest validation auto-run (formerly between embed and the marker)
    was removed for NashSU alignment; _finalize_book now runs embeddings then
    sets the completion marker."""

    def setUp(self):
        self._orig_embed = ingest.stage_3_7_embed_new_pages
        self.calls: list[str] = []
        ingest.stage_3_7_embed_new_pages = lambda *a, **k: self.calls.append("embed")

    def tearDown(self):
        ingest.stage_3_7_embed_new_pages = self._orig_embed

    def test_runs_embed_then_marks_complete(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            raw = _raw_file(tmp)
            h = _core.file_sha256(raw)

            self.assertFalse(_core.is_stage_done(cfg, h, "ingested"))
            ingest._finalize_book(raw, cfg, ["sources/x.md"], h)

            # Order matters: embeddings must precede the completion marker so a
            # failing/missing embed stack pauses BEFORE the book is marked done.
            self.assertEqual(self.calls, ["embed"])
            self.assertTrue(_core.is_stage_done(cfg, h, "ingested"))

    def test_embed_failure_leaves_book_unmarked(self):
        """No-fallback: if embeddings raise, the book is NOT marked complete."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            raw = _raw_file(tmp)
            h = _core.file_sha256(raw)

            def _boom(*a, **k):
                raise RuntimeError("Ollama down")
            ingest.stage_3_7_embed_new_pages = _boom

            with self.assertRaises(RuntimeError):
                ingest._finalize_book(raw, cfg, ["sources/x.md"], h)
            self.assertFalse(_core.is_stage_done(cfg, h, "ingested"))


class TestStage02ShouldSkip(unittest.TestCase):
    """The four marker/source-page states (Finding 2: no dead code below)."""

    def _setup(self, tmp: Path):
        cfg = _make_config(tmp)
        raw = _raw_file(tmp)
        h = _core.file_sha256(raw)
        return cfg, raw, h

    def _write_source_page(self, cfg, raw):
        sp = _stage_3_1_wiki_path_for_source(raw, cfg)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text("---\ntype: source\n---\n# x\n", encoding="utf-8")
        return sp

    def test_complete_marker_and_page_exists_skips(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg, raw, h = self._setup(tmp)
            self._write_source_page(cfg, raw)
            _core.mark_stage_done(cfg, h, "ingested")
            self.assertTrue(_stage_0_2_should_skip(raw, cfg))

    def test_complete_marker_but_page_deleted_reingest_and_clears_marker(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg, raw, h = self._setup(tmp)
            _core.mark_stage_done(cfg, h, "ingested")  # marker set, no page
            self.assertFalse(_stage_0_2_should_skip(raw, cfg))
            # Stale marker must be cleared so the re-ingest actually re-runs.
            self.assertFalse(_core.is_stage_done(cfg, h, "ingested"))

    def test_page_exists_no_marker_resumes(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg, raw, _h = self._setup(tmp)
            self._write_source_page(cfg, raw)
            # Mid-flight: pages written but ingested not set → do NOT skip.
            self.assertFalse(_stage_0_2_should_skip(raw, cfg))

    def test_fresh_no_page_no_marker_ingests(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg, raw, _h = self._setup(tmp)
            self.assertFalse(_stage_0_2_should_skip(raw, cfg))


class TestStage02ShouldSkipQueryBridge(unittest.TestCase):
    """Query bridges (raw/queries/*.md) never get a Stage 2.6 source page —
    the ingested marker alone must decide skip/resume (Finding 3)."""

    def test_marker_set_skips_even_though_no_source_page_exists(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            raw = _raw_query_bridge_file(tmp)
            h = _core.file_sha256(raw)
            _core.mark_stage_done(cfg, h, "ingested")

            # Sanity: this is exactly the state that used to be misread as a
            # stale marker for a normal source (page never existed here).
            self.assertFalse(_stage_3_1_wiki_path_for_source(raw, cfg).exists())
            self.assertTrue(_stage_0_2_should_skip(raw, cfg))
            # And the marker must survive — no false "stale marker" clear.
            self.assertTrue(_core.is_stage_done(cfg, h, "ingested"))

    def test_no_marker_does_not_skip(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            raw = _raw_query_bridge_file(tmp)
            self.assertFalse(_stage_0_2_should_skip(raw, cfg))


if __name__ == "__main__":
    unittest.main()
