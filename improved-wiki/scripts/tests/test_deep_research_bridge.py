"""Tests for the deep-research bridge — ingest accepting a wiki/queries page.

NashSU parity: ``deep-research.ts`` hands ``wiki/queries/<page>`` straight to
``autoIngest``. The improved-wiki pipeline is raw-root-centric (~20
``relative_to(raw_root)`` sites), so ``ingest._bridge_wiki_queries_to_raw``
copies the page into ``raw/queries/`` and returns the copy. These tests pin
that bridge behaviour without running the full pipeline.

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


class TestBridgeWikiQueriesToRaw(unittest.TestCase):
    def test_wiki_queries_page_is_copied_under_raw_queries(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            q = cfg.wiki_dir / "queries" / "research-gan-driver.md"
            q.parent.mkdir(parents=True, exist_ok=True)
            q.write_text("# Research: GaN driver\n", encoding="utf-8")

            out = ingest._bridge_wiki_queries_to_raw(q, cfg)

            self.assertTrue(out.is_relative_to(cfg.raw_root))
            self.assertEqual(out, cfg.raw_root / "queries" / "research-gan-driver.md")
            self.assertTrue(out.exists())
            self.assertIn("Research: GaN driver", out.read_text(encoding="utf-8"))

    def test_non_queries_path_is_returned_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            raw = cfg.raw_root / "Book" / "foo.pdf"
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_bytes(b"%PDF-1.4")
            out = ingest._bridge_wiki_queries_to_raw(raw, cfg)
            self.assertEqual(out, raw)  # no copy, same path
            self.assertFalse((cfg.raw_root / "queries").exists())

    def test_bridge_is_idempotent_on_re_ingest(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            q = cfg.wiki_dir / "queries" / "x.md"
            q.parent.mkdir(parents=True, exist_ok=True)
            q.write_text("v1", encoding="utf-8")
            first = ingest._bridge_wiki_queries_to_raw(q, cfg)
            self.assertEqual(first.read_text(encoding="utf-8"), "v1")
            q.write_text("v2", encoding="utf-8")
            second = ingest._bridge_wiki_queries_to_raw(q, cfg)
            self.assertEqual(first, second)  # same dest path
            self.assertEqual(second.read_text(encoding="utf-8"), "v2")  # overwritten


if __name__ == "__main__":
    unittest.main()
