"""Tests for ingest accepting a wiki/queries page directly.

NashSU parity: ``deep-research.ts`` hands ``wiki/queries/<page>`` straight to
``autoIngest``, which is path-agnostic. improved-wiki now matches this
(2026-07-16): ``ingest.py`` accepts ``wiki/queries/<page>`` as a source
without copying it into ``raw/queries/`` first (the old bridge step,
``ingest._bridge_wiki_queries_to_raw``, was removed — the ~20
``relative_to(raw_root)`` call sites it existed to work around now route
through ``_core.canonical_source_path``/``source_cache_key``, which handle a
``wiki/queries/`` path natively). These tests pin the CLI-gate and
identity-derivation behavior for that path, without running the full
pipeline.

Run:  python3 scripts/tests/test_deep_research_bridge.py
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


def _make_config(tmp: Path) -> _core.Config:
    return _core.Config(
        wiki_root=tmp, raw_root=tmp / "raw",
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


class TestIsIngestableSourcePath(unittest.TestCase):
    """The CLI gate (ingest.py main()) that replaced the old bridge copy."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cfg = _make_config(Path(self.tmpdir.name))

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_wiki_queries_page_is_accepted_without_copying(self):
        q = self.cfg.wiki_dir / "queries" / "research-gan-driver.md"
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_text("# Research: GaN driver\n", encoding="utf-8")

        self.assertTrue(ingest._is_ingestable_source_path(q, self.cfg))
        # No bridge copy is ever created.
        self.assertFalse((self.cfg.raw_root / "queries").exists())

    def test_normal_raw_source_is_accepted(self):
        raw = self.cfg.raw_root / "Book" / "foo.pdf"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"%PDF-1.4")
        self.assertTrue(ingest._is_ingestable_source_path(raw, self.cfg))

    def test_path_outside_raw_and_wiki_queries_is_rejected(self):
        outside = self.cfg.wiki_root.parent / "elsewhere" / "x.md"
        self.assertFalse(ingest._is_ingestable_source_path(outside, self.cfg))

    def test_other_wiki_subdir_is_rejected(self):
        concept = self.cfg.wiki_dir / "concepts" / "x.md"
        self.assertFalse(ingest._is_ingestable_source_path(concept, self.cfg))


class TestSourceIdentityForQueryPage(unittest.TestCase):
    """canonical_source_path / source_cache_key on a wiki/queries/ path —
    the two functions that replaced the bridge's ~20 relative_to(raw_root)
    call sites."""

    def setUp(self):
        self.cfg = _make_config(Path("/proj"))

    def test_canonical_source_path_uses_wiki_queries_prefix(self):
        q = self.cfg.wiki_dir / "queries" / "research-x.md"
        self.assertEqual(_core.canonical_source_path(q, self.cfg), "wiki/queries/research-x.md")

    def test_cache_key_matches_legacy_bridge_format(self):
        # A directly-ingested wiki/queries/x.md and a pre-2026-07-16
        # raw/queries/x.md bridge copy of the same logical source must
        # resolve to the SAME cache key, so --delete/validate_ingest.py
        # bookkeeping doesn't fork into two formats across the cutover.
        direct = self.cfg.wiki_dir / "queries" / "x.md"
        legacy_bridge = self.cfg.raw_root / "queries" / "x.md"
        self.assertEqual(_core.source_cache_key(direct, self.cfg), "queries/x.md")
        self.assertEqual(
            _core.source_cache_key(direct, self.cfg),
            _core.source_cache_key(legacy_bridge, self.cfg))

    def test_canonical_source_path_normal_raw_source_unaffected(self):
        raw = self.cfg.raw_root / "Book" / "foo.pdf"
        self.assertEqual(_core.canonical_source_path(raw, self.cfg), "raw/Book/foo.pdf")


if __name__ == "__main__":
    unittest.main()
