"""Regression tests for Stage 0.2 skip logic and per-book finalization.

Stdlib `unittest` only — no pytest, no network, no LLM calls.

Covers two 2026-06-25 audit findings:

  Finding 1 — batch/queue path skipped Stage 3.7 (embeddings) + 4.1
  (validation) and never set the stage_4_1 marker, because that tail lived
  only in ingest_one. _finalize_book now centralizes it; both ingest_one and
  batch_ingest call it. Test: _finalize_book runs embed→validate→mark in order.

  Finding 2 — _stage_0_2_should_skip carried ~60 lines of unreachable code
  (a wikilink-completeness check after an unconditional return). Removed; the
  stage_4_1 marker is the single completeness signal. Test: the four
  marker/source-page states resolve to the right skip/resume decision.
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
        llm_base_url="https://example.invalid", llm_model="m", llm_api_key="",
        llm_protocol="anthropic", caption_api_key="", caption_base_url="x",
        caption_model="c", chunk_size=60000, chunk_overlap=3000,
        source_budget=100000, target_chars=60000, target_tokens=30000,
        max_tokens=8192, conversation_prefix="ab12cd34",
    )


def _raw_file(tmp: Path) -> Path:
    raw = tmp / "raw" / "Book" / "x.pdf"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"%PDF-1.4 fake")
    return raw


class TestFinalizeBook(unittest.TestCase):
    """_finalize_book = embeddings → validate → stage_4_1 marker, in order."""

    def setUp(self):
        self._orig_embed = ingest.stage_3_7_embed_new_pages
        self._orig_validate = ingest.stage_4_1_validate_ingest
        self.calls: list[str] = []
        ingest.stage_3_7_embed_new_pages = lambda *a, **k: self.calls.append("embed")
        ingest.stage_4_1_validate_ingest = lambda *a, **k: self.calls.append("validate")

    def tearDown(self):
        ingest.stage_3_7_embed_new_pages = self._orig_embed
        ingest.stage_4_1_validate_ingest = self._orig_validate

    def test_runs_embed_validate_then_marks_complete(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            raw = _raw_file(tmp)
            h = _core.file_sha256(raw)

            self.assertFalse(_core.is_stage_done(cfg, h, "stage_4_1"))
            ingest._finalize_book(raw, cfg, ["sources/x.md"], h)

            # Order matters: embeddings must precede the completion marker so a
            # failing/missing embed stack pauses BEFORE the book is marked done.
            self.assertEqual(self.calls, ["embed", "validate"])
            self.assertTrue(_core.is_stage_done(cfg, h, "stage_4_1"))

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
            self.assertFalse(_core.is_stage_done(cfg, h, "stage_4_1"))


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
            _core.mark_stage_done(cfg, h, "stage_4_1")
            self.assertTrue(_stage_0_2_should_skip(raw, cfg))

    def test_complete_marker_but_page_deleted_reingest_and_clears_marker(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg, raw, h = self._setup(tmp)
            _core.mark_stage_done(cfg, h, "stage_4_1")  # marker set, no page
            self.assertFalse(_stage_0_2_should_skip(raw, cfg))
            # Stale marker must be cleared so the re-ingest actually re-runs.
            self.assertFalse(_core.is_stage_done(cfg, h, "stage_4_1"))

    def test_page_exists_no_marker_resumes(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg, raw, _h = self._setup(tmp)
            self._write_source_page(cfg, raw)
            # Mid-flight: pages written but stage_4_1 not set → do NOT skip.
            self.assertFalse(_stage_0_2_should_skip(raw, cfg))

    def test_fresh_no_page_no_marker_ingests(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg, raw, _h = self._setup(tmp)
            self.assertFalse(_stage_0_2_should_skip(raw, cfg))


if __name__ == "__main__":
    unittest.main()
