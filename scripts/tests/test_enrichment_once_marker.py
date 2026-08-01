"""Regression: post-write wikilink enrichment is one projection per ingest."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _core  # noqa: E402
import _ingest_write as ingest_write  # noqa: E402


def _config(root: Path) -> _core.Config:
    return _core.Config(
        wiki_root=root,
        raw_root=root / "raw",
        wiki_dir=root / "wiki",
        runtime_dir=root / ".llm-wiki",
        cache_path=root / ".llm-wiki" / "ingest-cache.json",
        progress_dir=root / ".llm-wiki" / "ingest-progress",
        extract_tmp_dir=root / ".llm-wiki" / "extract-tmp",
        llm_model="test",
        caption_api_key="",
        caption_base_url="http://127.0.0.1",
        caption_model="test",
        chunk_overlap=3000,
        source_budget=100000,
        target_chars=60000,
        target_tokens=30000,
        max_tokens=8192,
    )


class TestEnrichmentOnceMarker(unittest.TestCase):
    def setUp(self):
        self._original_enrich = ingest_write.enrich_wikilinks_batch

    def tearDown(self):
        ingest_write.enrich_wikilinks_batch = self._original_enrich

    def test_empty_answer_is_terminal_and_does_not_reprompt(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)
            page = cfg.wiki_dir / "concepts" / "no-safe-match.md"
            page.parent.mkdir(parents=True)
            page.write_text("# No safe match\n", encoding="utf-8")
            calls = []

            def empty_answer(*args, **kwargs):
                calls.append(1)
                return {}

            ingest_write.enrich_wikilinks_batch = empty_answer
            first = ingest_write.run_wikilink_enrichment_once(
                cfg,
                "a" * 64,
                enrich_enabled=True,
                enrich_candidates=[("concepts/no-safe-match.md", page)],
                existing_slugs=["concepts/other"],
                write_phase_done=False,
            )
            self.assertEqual(first, 0)
            self.assertEqual(len(calls), 1)
            self.assertTrue(
                _core.is_stage_done(cfg, "a" * 64, "enrichment_done"))

            def boom(*args, **kwargs):
                raise AssertionError("completed enrichment must not reprompt")

            ingest_write.enrich_wikilinks_batch = boom
            second = ingest_write.run_wikilink_enrichment_once(
                cfg,
                "a" * 64,
                enrich_enabled=True,
                enrich_candidates=[("concepts/no-safe-match.md", page)],
                existing_slugs=["concepts/other"],
                write_phase_done=False,
            )
            self.assertEqual(second, 0)

    def test_legacy_write_phase_backfills_without_calling_enricher(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)

            def boom(*args, **kwargs):
                raise AssertionError("legacy completed write phase must not rerun")

            ingest_write.enrich_wikilinks_batch = boom
            result = ingest_write.run_wikilink_enrichment_once(
                cfg,
                "b" * 64,
                enrich_enabled=True,
                enrich_candidates=[],
                existing_slugs=[],
                write_phase_done=True,
            )
            self.assertEqual(result, 0)
            payload = _core.get_stage_payload(
                cfg, "b" * 64, "enrichment_done")
            self.assertEqual(payload["status"], "legacy-write-phase")


if __name__ == "__main__":
    unittest.main()
