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
import _ingest_chunks as chunks  # noqa: E402
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
            prompt = generation._stage_2_4_build_prompt(
                self.ANALYSIS,
                "source text",
                0,
                cfg.raw_root / "book.pdf",
                cfg,
            )
        self.assertIn("Key Method", prompt)
        self.assertIn("Key System", prompt)
        self.assertNotIn("Background Term", prompt)
        self.assertIn("There is no page-count target", prompt)
        self.assertNotIn("Supplementary foundational pages", prompt)
        self.assertNotIn("EVERY concept", prompt)

    def test_inventory_does_not_reserve_mentioned_slug(self):
        analyses = [
            self.ANALYSIS,
            {
                "concepts_found": [{
                    "name": "Background Term",
                    "importance": "core",
                    "definition": "Materially developed later.",
                    "key_details": [],
                }],
                "entities_found": [],
                "schema_typed_candidates": [],
            },
        ]
        meta = [(0, "a", "", ""), (1, "b", "", "")]
        inventory = chunks._build_gen_inventory(meta, analyses)
        self.assertEqual(inventory[_core.slugify("Background Term")], 1)

    def test_stats_extract_only_page_eligible_concepts(self):
        concepts, entities = generation._stage_2_4_extract_names([self.ANALYSIS])
        self.assertEqual(concepts, ["Key Method"])
        self.assertEqual(entities, ["Key System"])


class TestGenerationTruncationRepair(unittest.TestCase):
    def test_same_type_existing_page_is_generated_at_exact_update_path(self):
        analysis = {
            "concepts_found": [{
                "name": "Key Method",
                "importance": "core",
                "definition": "Materially expanded by this source.",
                "key_details": [],
            }],
            "entities_found": [],
            "schema_typed_candidates": [],
            "formulas": [],
        }
        response = (
            "---FILE:wiki/concepts/established-key-method.md---\n"
            "---\ntype: concept\ntitle: Key Method\n---\nupdated\n"
            "---END FILE---\n"
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
                blocks = generation.stage_2_4_generate_chunk(
                    analysis,
                    0,
                    [],
                    cfg.raw_root / "book.pdf",
                    cfg,
                    chunk_text="source text",
                    existing_refs={
                        "Key Method": [
                            "concepts/established-key-method",
                        ],
                    },
                )
        finally:
            generation.call_anthropic_protocol = original

        self.assertEqual(
            [path for path, _ in blocks],
            ["concepts/established-key-method.md"],
        )
        self.assertEqual(len(calls), 1)
        self.assertIn(
            "(slug: concepts/established-key-method) "
            "[core; UPDATE EXISTING PAGE]",
            calls[0],
        )

    def test_cross_type_existing_page_is_link_only_without_llm_call(self):
        analysis = {
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
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _config(tmp)
            cfg.raw_root.mkdir(parents=True)
            cfg.wiki_dir.mkdir(parents=True)
            blocks = generation.stage_2_4_generate_chunk(
                analysis,
                0,
                [],
                cfg.raw_root / "book.pdf",
                cfg,
                chunk_text="source text",
                existing_refs={
                    "Key Method": ["entities/key-method"],
                },
            )
        self.assertEqual(blocks, [])

    def test_chunk_generation_repairs_exact_unclosed_file(self):
        analysis = {
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
        truncated = (
            "---FILE:wiki/concepts/key-method.md---\n"
            "---\ntype: concept\ntitle: Key Method\n---\npartial"
        )
        repaired = (
            "---FILE:wiki/concepts/key-method.md---\n"
            "---\ntype: concept\ntitle: Key Method\n---\ncomplete\n"
            "---END FILE---\n"
        )
        calls: list[str] = []

        def _spy(prompt, config, max_tokens=None, label=None):
            index = len(calls)
            calls.append(prompt)
            return (
                (truncated, "end_turn")
                if index == 0
                else (repaired, "end_turn")
            )

        original = generation.call_anthropic_protocol
        generation.call_anthropic_protocol = _spy
        try:
            with tempfile.TemporaryDirectory() as d:
                tmp = Path(d)
                cfg = _config(tmp)
                cfg.raw_root.mkdir(parents=True)
                cfg.wiki_dir.mkdir(parents=True)
                blocks = generation.stage_2_4_generate_chunk(
                    analysis,
                    0,
                    [],
                    cfg.raw_root / "book.pdf",
                    cfg,
                    chunk_text="source text",
                )
        finally:
            generation.call_anthropic_protocol = original

        self.assertEqual(len(calls), 2)
        self.assertEqual([path for path, _ in blocks], [
            "concepts/key-method.md",
        ])
        self.assertIn("complete", blocks[0][1])
        self.assertIn("- wiki/concepts/key-method.md", calls[1])
        self.assertNotIn("per-concept", calls[1].lower())


if __name__ == "__main__":
    unittest.main()
