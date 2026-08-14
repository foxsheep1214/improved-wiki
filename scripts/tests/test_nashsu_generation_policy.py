"""NashSU key-item selection and no-quota generation policy."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _core  # noqa: E402
import _stage_2_analyze as analyze  # noqa: E402
import _stage_2_4_generation as generation  # noqa: E402


def _config(tmp: Path) -> _core.Config:
    return _core.Config(
        wiki_root=tmp,
        raw_root=tmp / "raw",
        wiki_dir=tmp / "wiki",
        runtime_dir=tmp / "rt",
        cache_path=tmp / "rt" / "ingest-cache.json",
        progress_dir=tmp / "rt" / "ingest-progress",
        extract_tmp_dir=tmp / "rt" / "extract-tmp",
        llm_model="m",
        caption_api_key="",
        caption_base_url="x",
        caption_model="c",
        chunk_overlap=3000,
        source_budget=100000,
        target_chars=60000,
        target_tokens=30000,
        max_tokens=8192,
        conversation_prefix="ab12cd34",
    )


def _analysis_prompt(tmp: Path) -> str:
    cfg = _config(tmp)
    cfg.wiki_dir.mkdir(parents=True)
    return analyze._stage_2_2_build_prompt(
        chunk_text="A focused source section.",
        chunk_index=0,
        chunk_total=1,
        global_digest={},
        file_path=cfg.raw_root / "book.pdf",
        config=cfg,
        existing_slugs=[],
    )


class TestAnalysisPolicy(unittest.TestCase):
    def test_prompt_requests_key_items_without_quotas(self):
        with tempfile.TemporaryDirectory() as d:
            prompt = _analysis_prompt(Path(d))
        self.assertIn("Be thorough but concise", prompt)
        self.assertIn("new or materially updated key concepts/entities", prompt)
        self.assertIn("There is no numeric target", prompt)
        self.assertNotIn("Minimum 3 claims", prompt)
        self.assertNotIn("Every concept this chunk", prompt)
        self.assertNotIn("When in doubt, LIST", prompt)
        self.assertNotIn("2-3 verbatim", prompt)

    def test_empty_details_and_quotes_are_valid(self):
        raw = {
            "chunk_index": 1,
            "chunk_total": 1,
            "entities_found": [],
            "concepts_found": [{
                "name": "Key Method",
                "importance": "core",
                "definition": "A method central to the source.",
                "key_details": [],
            }],
            "claims": [],
            "formulas": [],
            "connections_to_existing_wiki": [],
            "schema_typed_candidates": [],
            "updated_global_digest": {
                "book_meta": {},
                "outline": [],
                "key_entities": [],
                "key_concepts": [],
                "key_claims": [],
            },
        }
        got = analyze.normalize_and_validate_chunk_analysis(raw)
        self.assertEqual(got["concepts_found"][0]["key_details"], [])
        self.assertNotIn("source_quotes", got)


class TestGenerationSelection(unittest.TestCase):
    ANALYSIS = {
        "concepts_found": [
            {
                "name": "Key Method",
                "importance": "core",
                "definition": "Central method.",
                "key_details": [],
            },
            {
                "name": "Background Term",
                "importance": "mentioned",
                "definition": "Only named in passing.",
                "key_details": [],
            },
        ],
        "entities_found": [{
            "name": "Key System",
            "significance": "Central system.",
        }],
        "schema_typed_candidates": [],
        "formulas": [],
    }

    def test_mentioned_concept_is_not_in_generation_prompt(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _config(tmp)
            cfg.wiki_dir.mkdir(parents=True)
            cfg.raw_root.mkdir(parents=True)
            prompt = generation._stage_2_4_build_all_prompt(
                [self.ANALYSIS],
                cfg.raw_root / "book.pdf",
                cfg,
                source_context="source text",
            )
        self.assertIn("Key Method", prompt)
        self.assertIn("Key System", prompt)
        self.assertNotIn("Background Term", prompt)
        self.assertIn("There is no page-count target", prompt)
        self.assertIn("NO_KEY_PAGES", prompt)
        self.assertIn("mandatory source page", prompt.lower())
        self.assertNotIn("Supplementary foundational pages", prompt)
        self.assertNotIn("EVERY concept", prompt)

    def test_stats_extract_only_page_eligible_concepts(self):
        concepts, entities = generation._stage_2_4_extract_names([self.ANALYSIS])
        self.assertEqual(concepts, ["Key Method"])
        self.assertEqual(entities, ["Key System"])


class TestGenerationIntegrity(unittest.TestCase):
    ANALYSIS = {
        "concepts_found": [{
            "name": "Key Method",
            "importance": "core",
            "definition": "Central method.",
            "key_details": [],
        }],
        "entities_found": [],
        "schema_typed_candidates": [],
        "formulas": [],
    }

    @staticmethod
    def _source_block() -> str:
        return (
            "---FILE:wiki/sources/book.md---\n"
            "---\ntype: source\ntitle: Book\n---\n# Book\nsummary\n"
            "---END FILE---\n"
        )

    def test_zero_optional_pages_still_requires_source_block(self):
        def _spy(prompt, config, max_tokens=None, label=None):
            return self._source_block() + "NO_KEY_PAGES\n", "end_turn"

        original = generation.call_anthropic_protocol
        generation.call_anthropic_protocol = _spy
        try:
            with tempfile.TemporaryDirectory() as d:
                tmp = Path(d)
                cfg = _config(tmp)
                cfg.raw_root.mkdir(parents=True)
                cfg.wiki_dir.mkdir(parents=True)
                blocks, slugs, stop_reason = generation.stage_2_4_generate_all(
                    [self.ANALYSIS],
                    cfg.raw_root / "book.pdf",
                    cfg,
                    source_context="source text",
                )
        finally:
            generation.call_anthropic_protocol = original

        self.assertEqual([path for path, _ in blocks], ["sources/book.md"])
        self.assertEqual(slugs, ["book"])
        self.assertEqual(stop_reason, "end_turn")

    def test_sentinel_without_source_block_fails(self):
        def _spy(prompt, config, max_tokens=None, label=None):
            return "NO_KEY_PAGES", "end_turn"

        original = generation.call_anthropic_protocol
        generation.call_anthropic_protocol = _spy
        try:
            with tempfile.TemporaryDirectory() as d:
                tmp = Path(d)
                cfg = _config(tmp)
                cfg.raw_root.mkdir(parents=True)
                cfg.wiki_dir.mkdir(parents=True)
                with self.assertRaisesRegex(RuntimeError, "mandatory source"):
                    generation.stage_2_4_generate_all(
                        [self.ANALYSIS],
                        cfg.raw_root / "book.pdf",
                        cfg,
                        source_context="source text",
                    )
        finally:
            generation.call_anthropic_protocol = original

    def test_same_type_existing_page_is_generated_at_exact_update_path(self):
        response = (
            "---FILE:wiki/concepts/established-key-method.md---\n"
            "---\ntype: concept\ntitle: Key Method\n---\nupdated\n"
            "---END FILE---\n"
            + self._source_block()
        )
        calls: list[str] = []

        def _spy(prompt, config, max_tokens=None, label=None):
            calls.append(prompt)
            return response, "end_turn"

        original = generation.call_anthropic_protocol
        generation.call_anthropic_protocol = _spy
        try:
            with tempfile.TemporaryDirectory() as d:
                tmp = Path(d)
                cfg = _config(tmp)
                cfg.raw_root.mkdir(parents=True)
                cfg.wiki_dir.mkdir(parents=True)
                blocks, _, _ = generation.stage_2_4_generate_all(
                    [self.ANALYSIS],
                    cfg.raw_root / "book.pdf",
                    cfg,
                    source_context="source text",
                    existing_refs={
                        "Key Method": ["concepts/established-key-method"],
                    },
                )
        finally:
            generation.call_anthropic_protocol = original

        self.assertEqual(
            [path for path, _ in blocks],
            ["concepts/established-key-method.md", "sources/book.md"],
        )
        self.assertEqual(len(calls), 1)
        self.assertIn(
            "(slug: concepts/established-key-method) "
            "[core; UPDATE EXISTING PAGE]",
            calls[0],
        )

    def test_cross_type_existing_page_is_link_only(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _config(tmp)
            cfg.raw_root.mkdir(parents=True)
            cfg.wiki_dir.mkdir(parents=True)
            prompt = generation._stage_2_4_build_all_prompt(
                [self.ANALYSIS],
                cfg.raw_root / "book.pdf",
                cfg,
                source_context="source text",
                existing_refs={"Key Method": ["entities/key-method"]},
            )
        self.assertIn("CROSS-TYPE ASSOCIATION [[entities/key-method]]", prompt)
        self.assertIn("do NOT create a duplicate page", prompt)


if __name__ == "__main__":
    unittest.main()
