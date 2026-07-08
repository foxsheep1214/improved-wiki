"""Regression tests for the Stage 2.2→2.4 chunk-pipeline resume (cache) path.

Stdlib `unittest` only — no pytest, no network, no LLM calls.

Run:
    python3 -m unittest tests.test_chunk_pipeline_resume   # from scripts/
    python3 scripts/tests/test_chunk_pipeline_resume.py      # from skill root

Maps to the 2026-06-25 concept/entity/query loss bug:

  On a ``stage_2_3_done`` cache-resume, ``_run_chunk_pipeline`` restored
  ``file_blocks`` by re-parsing ``raw_response`` — but raw_response was
  "\n".join(parsed FILE-block BODIES), bodies WITHOUT the ---FILE:...---
  wrappers, so parse_file_blocks() returned [] and silently dropped every
  concept/entity page. 2.6 then wrote only the source page; 2.7/2.9 had no
  concepts to work on. Fix: persist ``file_blocks`` directly and restore it;
  raw_response was removed. Guard: if the marker is set but no ``file_blocks``
  artifact exists, invalidate the marker and re-run instead of returning [].
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
import _ingest_chunks  # noqa: E402


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


_SENTINEL = "recompute-path-reached"


class _RecomputeReached(Exception):
    pass


class TestUnmarkStageDone(unittest.TestCase):
    """unmark_stage_done clears the marker (and payload) so a stage re-runs."""

    def test_unmark_clears_marker_and_payload(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            h = "deadbeef" * 8
            _core.mark_stage_done(cfg, h, "stage_2_3_done", payload={"x": 1})
            self.assertTrue(_core.is_stage_done(cfg, h, "stage_2_3_done"))
            self.assertEqual(_core.get_stage_payload(cfg, h, "stage_2_3_done"), {"x": 1})
            _core.unmark_stage_done(cfg, h, "stage_2_3_done")
            self.assertFalse(_core.is_stage_done(cfg, h, "stage_2_3_done"))
            self.assertEqual(_core.get_stage_payload(cfg, h, "stage_2_3_done"), {})

    def test_unmark_absent_marker_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            h = "feedface" * 8
            _core.unmark_stage_done(cfg, h, "stage_2_3_done")  # must not raise
            self.assertFalse(_core.is_stage_done(cfg, h, "stage_2_3_done"))


class TestChunkPipelineResume(unittest.TestCase):
    """The stage_2_3_done cache branch of _run_chunk_pipeline."""

    def setUp(self):
        # Stub the recompute entry point so the cache-branch decision is
        # observable without any LLM/network work: if the pipeline falls
        # through to recompute, _stage_2_1_chunk_text raises _RecomputeReached.
        self._orig_chunk_text = _ingest_chunks._stage_2_1_chunk_text

        def _boom(*_a, **_k):
            raise _RecomputeReached(_SENTINEL)

        _ingest_chunks._stage_2_1_chunk_text = _boom

    def tearDown(self):
        _ingest_chunks._stage_2_1_chunk_text = self._orig_chunk_text

    def _raw_file(self, tmp: Path) -> Path:
        raw = tmp / "raw" / "book.pdf"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"%PDF-1.4 fake content for hashing")
        return raw

    def test_restore_returns_persisted_file_blocks(self):
        """Persisted file_blocks are restored verbatim — NOT re-derived. The
        recompute path must not be reached."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            raw = self._raw_file(tmp)
            h = _core.file_sha256(raw)

            persisted = [
                ["concepts/state-of-charge.md", "---\ntype: concept\n---\nbody A"],
                ["entities/bms-ic.md", "---\ntype: entity\n---\nbody B"],
            ]
            progress = {
                "chunk_analyses": [{"concepts_found": ["soc"], "entities_found": ["ic"]}],
                "analysis": {"method": "x"},
                "incremental_associations": {},
                "file_blocks": persisted,
            }
            _core.mark_stage_done(cfg, h, "stage_2_3_done")

            ca, analysis, file_blocks, assoc, _gd = _ingest_chunks._run_chunk_pipeline(
                "extracted text " * 50, {"key_concepts": ["soc"]}, raw, cfg,
                "template", progress, verbose=False)

            self.assertEqual(file_blocks, persisted)
            self.assertEqual(len(ca), 1)
            self.assertEqual(analysis, {"method": "x"})
            # Marker must remain — this was a clean restore.
            self.assertTrue(_core.is_stage_done(cfg, h, "stage_2_3_done"))

    def test_legit_empty_file_blocks_restored_as_empty(self):
        """An explicitly-empty file_blocks (every concept already overlaps an
        existing wiki page) is a VALID restore — present-but-empty, trusted."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            raw = self._raw_file(tmp)
            h = _core.file_sha256(raw)

            progress = {
                "chunk_analyses": [{"concepts_found": ["soc"]}],
                "analysis": {},
                "incremental_associations": {"soc": ["concepts/soc.md"]},
                "file_blocks": [],  # key PRESENT, explicitly empty
            }
            _core.mark_stage_done(cfg, h, "stage_2_3_done")

            _ca, _an, file_blocks, _assoc, _gd = _ingest_chunks._run_chunk_pipeline(
                "extracted text " * 50, {"key_concepts": ["soc"]}, raw, cfg,
                "template", progress, verbose=False)

            self.assertEqual(file_blocks, [])
            self.assertTrue(_core.is_stage_done(cfg, h, "stage_2_3_done"))

    def test_missing_file_blocks_invalidates_marker_and_reruns(self):
        """Marker set but NO file_blocks artifact (old/partial cache): rather
        than silently return [] (the 2026-06-25 loss), invalidate the marker
        and fall through to recompute."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            raw = self._raw_file(tmp)
            h = _core.file_sha256(raw)

            progress = {
                "chunk_analyses": [{"concepts_found": ["soc"]}],
                "analysis": {},
                "incremental_associations": {},
                # NO "file_blocks" key — the pre-fix artifact shape.
            }
            _core.mark_stage_done(cfg, h, "stage_2_3_done")

            with self.assertRaises(_RecomputeReached):
                _ingest_chunks._run_chunk_pipeline(
                    "extracted text " * 50, {"key_concepts": ["soc"]}, raw, cfg,
                    "template", progress, verbose=False)

            # The marker must have been cleared so the re-run actually redoes
            # generation (and re-persists a real file_blocks artifact).
            self.assertFalse(_core.is_stage_done(cfg, h, "stage_2_3_done"))


class TestPrefetchBoundary(unittest.TestCase):
    """analyze_only (prefetch) runs Stage 2.2 then stops at the 2.2/2.3 boundary,
    BEFORE any wiki-dependent work. A later spine call reuses the cached 2.2."""

    def setUp(self):
        # Real chunking + heading resolution are irrelevant here; stub the heavy
        # LLM/analysis bits so the cache/boundary decision is observable offline.
        self._orig = {
            "chunk_text": _ingest_chunks._stage_2_1_chunk_text,
            "analyze": _ingest_chunks._analyze_all_chunks,
            "heading": _ingest_chunks._stage_2_2_resolve_chunk_heading_path,
            "verify": _ingest_chunks._verify_stage_2_2_chunks,
            "generate": _ingest_chunks._generate_from_analyses,
        }
        _ingest_chunks._stage_2_1_chunk_text = lambda *_a, **_k: ["chunk-0 text"]
        _ingest_chunks._stage_2_2_resolve_chunk_heading_path = lambda *_a, **_k: ""
        _ingest_chunks._verify_stage_2_2_chunks = lambda *_a, **_k: None
        self._fake_ca = [{"concepts_found": [{"name": "soc"}], "entities_found": []}]
        _ingest_chunks._analyze_all_chunks = lambda *_a, **_k: (self._fake_ca, "")

    def tearDown(self):
        _ingest_chunks._stage_2_1_chunk_text = self._orig["chunk_text"]
        _ingest_chunks._analyze_all_chunks = self._orig["analyze"]
        _ingest_chunks._stage_2_2_resolve_chunk_heading_path = self._orig["heading"]
        _ingest_chunks._verify_stage_2_2_chunks = self._orig["verify"]
        _ingest_chunks._generate_from_analyses = self._orig["generate"]

    def _raw_file(self, tmp: Path) -> Path:
        raw = tmp / "raw" / "book.pdf"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"%PDF-1.4 prefetch boundary test")
        return raw

    def test_prefetch_stops_before_wiki_dependent_stage_2_3(self):
        """analyze_only=True caches 2.2 + raises PrepareStopAfter('1.5') WITHOUT
        entering the wiki-dependent generation tail."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            raw = self._raw_file(tmp)
            h = _core.file_sha256(raw)

            # If 2.3+ is ever reached, this boom fires instead of PrepareStopAfter.
            def _boom(*_a, **_k):
                raise AssertionError("wiki-dependent 2.3+ ran during prefetch")
            _ingest_chunks._generate_from_analyses = _boom

            with self.assertRaises(_core.PrepareStopAfter):
                _ingest_chunks._run_chunk_pipeline(
                    "extracted " * 50, {"key_concepts": ["soc"]}, raw, cfg,
                    "template", None, verbose=False, analyze_only=True)

            # 2.2 cached for the later spine run.
            self.assertTrue(_core.is_stage_done(cfg, h, "stage_2_2_done"))
            self.assertFalse(_core.is_stage_done(cfg, h, "stage_2_3_done"))
            self.assertEqual(_core.load_progress(cfg, h)["chunk_analyses"], self._fake_ca)

    def test_spine_reuses_cached_2_2_and_runs_generation(self):
        """After prefetch cached 2.2, a normal (analyze_only=False) call restores
        chunk_analyses + the persisted roll-up digest (no re-analysis) and
        proceeds to the generation tail."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            raw = self._raw_file(tmp)
            h = _core.file_sha256(raw)

            # Seed the prefetch cache (2.2 done, 2.3 NOT done) with the rolled-up
            # digest the prefetch persists on 2.2 completion (5 required keys).
            valid_digest = {"book_meta": {"granularity": "book"}, "outline": [],
                            "key_concepts": ["soc"], "key_claims": [],
                            "key_entities": []}
            _core.save_progress(cfg, h, {"chunk_analyses": self._fake_ca,
                                         "global_digest": valid_digest})
            _core.mark_stage_done(cfg, h, "stage_2_2_done")

            # Re-analysis must NOT happen on the spine resume.
            _ingest_chunks._analyze_all_chunks = lambda *_a, **_k: (
                _ for _ in ()).throw(AssertionError("re-analyzed cached 2.2"))
            # Capture the generation-tail call instead of doing real 2.3/2.4 work.
            calls = {}
            def _fake_gen(ca, *_a, **_k):
                calls["ca"] = ca
                return ca, {"method": "stub"}, [("concepts/soc.md", "body")], {}
            _ingest_chunks._generate_from_analyses = _fake_gen

            ca, analysis, blocks, assoc, _gd = _ingest_chunks._run_chunk_pipeline(
                "extracted " * 50, {}, raw, cfg,
                "template", _core.load_progress(cfg, h), verbose=False)

            self.assertEqual(calls["ca"], self._fake_ca)  # generation got cached 2.2
            self.assertEqual(analysis, {"method": "stub"})
            self.assertEqual(blocks, [("concepts/soc.md", "body")])
            self.assertEqual(_gd, valid_digest)  # roll-up digest restored

    def test_spine_invalidates_pre_rollup_cache_and_reruns_2_2(self):
        """A cached 2.2 WITHOUT a persisted roll-up digest (pre-roll-up cache)
        is invalidated and re-analyzed instead of silently feeding an empty
        digest to 2.4/2.6/2.7/2.9."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            raw = self._raw_file(tmp)
            h = _core.file_sha256(raw)

            # Pre-roll-up cache: chunk_analyses persisted, no global_digest.
            _core.save_progress(cfg, h, {"chunk_analyses": self._fake_ca})
            _core.mark_stage_done(cfg, h, "stage_2_2_done")

            class _Reanalyzed(Exception):
                pass
            _ingest_chunks._analyze_all_chunks = lambda *_a, **_k: (
                _ for _ in ()).throw(_Reanalyzed())

            with self.assertRaises(_Reanalyzed):
                _ingest_chunks._run_chunk_pipeline(
                    "extracted " * 50, {}, raw, cfg,
                    "template", _core.load_progress(cfg, h), verbose=False)

            # Marker invalidated so the fresh 2.2 run persists a valid digest.
            self.assertFalse(_core.is_stage_done(cfg, h, "stage_2_2_done"))


if __name__ == "__main__":
    unittest.main()
