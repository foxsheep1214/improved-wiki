"""Structured-data verbatim preservation (NashSU v0.6.9 parity).

NashSU 0.6.9 added the same rule to three ingest prompts (ingest.ts:2196 /
2321 / 2833): tables, SQL DDL / CREATE TABLE, schema definitions, API
signatures, and configuration blocks must survive ingest VERBATIM in fenced
code blocks or Markdown tables — never reduced to prose. Exact field names,
types, constraints, keys, and indexes are the reason the source was ingested.

improved-wiki's Stage 2.2 emits YAML rather than NashSU's free-form markdown
analysis, so the rule needs a slot to write into. ``structured_data`` is that
slot, modelled exactly on ``formulas``: a dedicated verbatim list that Stage
2.4 re-injects with a REUSE-EXACTLY directive, so a table outside the
budget-trimmed source excerpt is never reconstructed from memory.

Stdlib unittest only.
"""
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
import _stage_2_context as ctx  # noqa: E402
from _stage_2_analyze import (  # noqa: E402
    ChunkAnalysisValidationError,
    normalize_and_validate_chunk_analysis,
)

_TABLE = (
    "| Pin | Name | Type | Function |\n"
    "|-----|------|------|----------|\n"
    "| 1   | VDD  | P    | Supply   |"
)


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


def _valid_analysis() -> dict:
    return {
        "chunk_index": 1,
        "chunk_total": 1,
        "entities_found": [],
        "concepts_found": [],
        "claims": [],
        "formulas": [],
        "connections_to_existing_wiki": [],
        "schema_typed_candidates": [],
        "updated_global_digest": "A digest long enough to clear the minimum "
                                 "substantive-string gate for this field.",
    }


class TestStage22Schema(unittest.TestCase):
    """``structured_data`` is a first-class validated Stage 2.2 field."""

    def test_absent_field_normalizes_to_empty_list(self):
        # Backward compatibility: checkpoints written before this field exists
        # must still restore.
        out = normalize_and_validate_chunk_analysis(_valid_analysis())
        self.assertEqual(out["structured_data"], [])

    def test_valid_entries_survive_normalization(self):
        analysis = _valid_analysis()
        analysis["structured_data"] = [
            {"label": "Table 3 — Pin functions (p.12)", "content": _TABLE},
        ]
        out = normalize_and_validate_chunk_analysis(analysis)
        self.assertEqual(out["structured_data"][0]["content"], _TABLE)
        self.assertIn("Table 3", out["structured_data"][0]["label"])

    def test_non_list_is_rejected(self):
        analysis = _valid_analysis()
        analysis["structured_data"] = _TABLE
        with self.assertRaises(ChunkAnalysisValidationError):
            normalize_and_validate_chunk_analysis(analysis)

    def test_non_mapping_item_is_rejected(self):
        analysis = _valid_analysis()
        analysis["structured_data"] = [_TABLE]
        with self.assertRaises(ChunkAnalysisValidationError):
            normalize_and_validate_chunk_analysis(analysis)

    def test_empty_content_is_rejected(self):
        analysis = _valid_analysis()
        analysis["structured_data"] = [{"label": "Table 3", "content": "   "}]
        with self.assertRaises(ChunkAnalysisValidationError):
            normalize_and_validate_chunk_analysis(analysis)

    def test_empty_label_is_rejected(self):
        analysis = _valid_analysis()
        analysis["structured_data"] = [{"label": "", "content": _TABLE}]
        with self.assertRaises(ChunkAnalysisValidationError):
            normalize_and_validate_chunk_analysis(analysis)


class TestStage22Prompt(unittest.TestCase):
    """Stage 2.2 asks for the verbatim block and declares the YAML key."""

    def _prompt(self, tmp: Path) -> str:
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

    def test_prompt_declares_structured_data_key(self):
        with tempfile.TemporaryDirectory() as d:
            prompt = self._prompt(Path(d))
        self.assertIn("structured_data:", prompt)

    def test_prompt_names_the_structured_shapes(self):
        with tempfile.TemporaryDirectory() as d:
            prompt = self._prompt(Path(d))
        for shape in ("CREATE TABLE", "API signature", "register"):
            self.assertIn(shape, prompt)

    def test_prompt_forbids_prose_reduction(self):
        with tempfile.TemporaryDirectory() as d:
            prompt = self._prompt(Path(d))
        self.assertIn("never paraphrase it into prose", prompt)


class TestStage24Grounding(unittest.TestCase):
    """Stage 2.4 receives the verbatim blocks with a REUSE-EXACTLY directive."""

    def test_collect_block_renders_label_and_content(self):
        block = generation._collect_structured_data_block([
            {"structured_data": [
                {"label": "Table 3 — Pin functions", "content": _TABLE},
            ]},
        ])
        self.assertIn("Table 3 — Pin functions", block)
        self.assertIn("| 1   | VDD  | P    | Supply   |", block)
        self.assertIn("REUSE EXACTLY", block)

    def test_collect_block_is_empty_without_structured_data(self):
        self.assertEqual(generation._collect_structured_data_block([{}]), "")

    def test_collect_block_deduplicates_repeated_content(self):
        block = generation._collect_structured_data_block([
            {"structured_data": [{"label": "Table 3", "content": _TABLE}]},
            {"structured_data": [{"label": "Table 3 again", "content": _TABLE}]},
        ])
        self.assertEqual(block.count("| 1   | VDD  | P    | Supply   |"), 1)

    def test_collect_block_honours_cap(self):
        analyses = [{"structured_data": [
            {"label": f"Table {i}", "content": f"| a |\n|---|\n| {i} |"}
            for i in range(10)
        ]}]
        block = generation._collect_structured_data_block(analyses, cap=3)
        self.assertEqual(block.count("Table "), 3)

    def test_generation_prompt_embeds_the_block(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _config(Path(d))
            (cfg.raw_root / "Book").mkdir(parents=True, exist_ok=True)
            analysis = {
                "concepts_found": [],
                "entities_found": [],
                "structured_data": [
                    {"label": "Table 3 — Pin functions", "content": _TABLE},
                ],
            }
            prompt = generation._stage_2_4_build_all_prompt(
                [analysis], cfg.raw_root / "Book" / "ds.pdf", cfg,
                source_context="some text")
        self.assertIn("| 1   | VDD  | P    | Supply   |", prompt)

    def test_generation_prompt_carries_the_preservation_rule(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _config(Path(d))
            (cfg.raw_root / "Book").mkdir(parents=True, exist_ok=True)
            prompt = generation._stage_2_4_build_all_prompt(
                [{"concepts_found": [], "entities_found": []}],
                cfg.raw_root / "Book" / "ds.pdf", cfg,
                source_context="some text")
        self.assertIn("Preserve structured source data verbatim", prompt)


class TestSourcePageRule(unittest.TestCase):
    """The mandatory source summary must not prose-ify structured data."""

    def test_source_page_guidance_carries_the_rule(self):
        section = generation._source_page_guidance_section("book")
        self.assertIn("structured source data verbatim", section)


class TestContextDegradation(unittest.TestCase):
    """``structured_data`` is dropped at the same tier as ``formulas``.

    Both are re-fed to Stage 2.4 verbatim by their own collector, so keeping
    them inside the analyses context under budget pressure is pure duplication.
    """

    def test_dropped_with_formulas_and_not_before(self):
        for level, dropped in ctx._DETAIL_LEVELS:
            if "formulas" in dropped:
                self.assertIn("structured_data", dropped, level)
            else:
                self.assertNotIn("structured_data", dropped, level)


if __name__ == "__main__":
    unittest.main()
