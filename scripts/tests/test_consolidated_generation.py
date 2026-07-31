"""Stage 2.4 uses one NashSU-style whole-source generation call."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _core  # noqa: E402
import _ingest_chunks as chunks  # noqa: E402
import _stage_2_4_generation as generation  # noqa: E402
from _stage_2_context import (  # noqa: E402
    build_consolidated_stage_2_context,
)


def _config(tmp: Path, source_budget: int = 100_000) -> _core.Config:
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
        source_budget=source_budget,
        target_chars=60_000,
        target_tokens=30_000,
        max_tokens=8192,
        conversation_prefix="ab12cd34",
    )


def _digest() -> dict:
    return {
        "book_meta": {"title": "Whole Book"},
        "outline": ["Early", "Late"],
        "key_entities": [],
        "key_concepts": [{"name": "Cross Chunk Method"}],
        "key_claims": [{"claim": "Late evidence changes the conclusion"}],
    }


def _analysis(index: int) -> dict:
    return {
        "chunk_index": index + 1,
        "chunk_total": 3,
        "concepts_found": [{
            "name": f"Concept {index + 1}",
            "importance": "core",
            "definition": f"analysis-evidence-{index + 1}",
            "key_details": [],
        }],
        "entities_found": [],
        "claims": [{
            "claim": f"claim-{index + 1}",
            "evidence": f"section-{index + 1}",
        }],
        "formulas": [],
        "connections_to_existing_wiki": [],
        "schema_typed_candidates": [],
        "updated_global_digest": _digest(),
    }


def _meta(index: int) -> tuple[int, str, str, str]:
    return (
        index,
        f"RAW-EVIDENCE-{index + 1}-" + ("x" * 500),
        "",
        f"Chapter {index + 1}",
    )


class TestConsolidatedContext(unittest.TestCase):
    def test_every_chunk_analysis_and_raw_evidence_is_represented(self):
        context = build_consolidated_stage_2_context(
            _digest(),
            [_analysis(i) for i in range(3)],
            [_meta(i) for i in range(3)],
            20_000,
        )
        self.assertLessEqual(len(context), 20_000)
        self.assertIn("# Consolidated Stage 2 Context", context)
        self.assertIn("Late evidence changes the conclusion", context)
        for index in range(1, 4):
            self.assertIn(f"analysis-evidence-{index}", context)
            self.assertIn(f"RAW-EVIDENCE-{index}", context)
            self.assertIn(f"Chunk {index}/3", context)

    def test_context_is_deterministic_and_budget_bounded(self):
        analyses = [_analysis(i) for i in range(3)]
        metas = [
            (i, f"HEAD-{i}-" + ("z" * 20_000) + f"-TAIL-{i}", "", f"H{i}")
            for i in range(3)
        ]
        first = build_consolidated_stage_2_context(
            _digest(), analyses, metas, 8_000)
        second = build_consolidated_stage_2_context(
            _digest(), analyses, metas, 8_000)
        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 8_000)
        for index in range(3):
            self.assertIn(f"HEAD-{index}", first)
            self.assertIn(f"TAIL-{index}", first)
        self.assertIn("middle omitted", first)

    def test_cardinality_mismatch_is_hard_failure(self):
        with self.assertRaisesRegex(RuntimeError, "cardinality mismatch"):
            build_consolidated_stage_2_context(
                _digest(), [_analysis(0)], [_meta(0), _meta(1)], 8_000)


class TestSingleGenerationCall(unittest.TestCase):
    def test_multi_chunk_pipeline_calls_generate_all_once(self):
        seen: list[dict] = []

        def _fake_all(
            chunk_analyses,
            file_path,
            config,
            template="",
            **kwargs,
        ):
            seen.append({
                "analyses": chunk_analyses,
                "context": kwargs.get("consolidated_context", ""),
            })
            return [("concepts/cross-chunk.md", "body")], [
                "concepts/cross-chunk"
            ], "end_turn"

        def _forbidden_chunk(*_args, **_kwargs):
            raise AssertionError("per-chunk Stage 2.4 generation was called")

        original_all = chunks.stage_2_4_generate_all
        original_chunk = chunks.stage_2_4_generate_chunk
        chunks.stage_2_4_generate_all = _fake_all
        chunks.stage_2_4_generate_chunk = _forbidden_chunk
        try:
            with tempfile.TemporaryDirectory() as directory:
                cfg = _config(Path(directory))
                cfg.raw_root.mkdir(parents=True)
                metas = [_meta(i) for i in range(3)]
                analyses = [_analysis(i) for i in range(3)]
                result = chunks._generate_all_chunks(
                    metas,
                    analyses,
                    {},
                    cfg.raw_root / "book.pdf",
                    cfg,
                    "",
                    chunk_total=3,
                    t_start=time.time(),
                    verbose=False,
                    global_digest=_digest(),
                )
        finally:
            chunks.stage_2_4_generate_all = original_all
            chunks.stage_2_4_generate_chunk = original_chunk

        self.assertEqual(len(seen), 1)
        self.assertEqual(len(seen[0]["analyses"]), 3)
        self.assertIn("analysis-evidence-3", seen[0]["context"])
        self.assertIn("RAW-EVIDENCE-3", seen[0]["context"])
        self.assertEqual(result[0][0][0], "concepts/cross-chunk.md")

    def test_parallel_environment_flag_cannot_restore_chunk_waves(self):
        calls = {"all": 0, "chunk": 0}

        def _fake_all(*_args, **_kwargs):
            calls["all"] += 1
            return [], [], None

        def _fake_chunk(*_args, **_kwargs):
            calls["chunk"] += 1
            return []

        previous = os.environ.get("IMPROVED_WIKI_PARALLEL_GEN")
        original_all = chunks.stage_2_4_generate_all
        original_chunk = chunks.stage_2_4_generate_chunk
        os.environ["IMPROVED_WIKI_PARALLEL_GEN"] = "1"
        chunks.stage_2_4_generate_all = _fake_all
        chunks.stage_2_4_generate_chunk = _fake_chunk
        try:
            with tempfile.TemporaryDirectory() as directory:
                cfg = _config(Path(directory))
                chunks._generate_all_chunks(
                    [_meta(0), _meta(1)],
                    [_analysis(0), _analysis(1)],
                    {},
                    Path("book.pdf"),
                    cfg,
                    "",
                    chunk_total=2,
                    t_start=time.time(),
                    verbose=False,
                    global_digest=_digest(),
                )
        finally:
            chunks.stage_2_4_generate_all = original_all
            chunks.stage_2_4_generate_chunk = original_chunk
            if previous is None:
                os.environ.pop("IMPROVED_WIKI_PARALLEL_GEN", None)
            else:
                os.environ["IMPROVED_WIKI_PARALLEL_GEN"] = previous

        self.assertEqual(calls, {"all": 1, "chunk": 0})

    def test_generation_prompt_receives_shared_context_verbatim(self):
        context = (
            "# Consolidated Stage 2 Context\n"
            "FINAL-DIGEST\nLATE-CHUNK-ANALYSIS\nLATE-RAW-EVIDENCE"
        )
        prompts: list[str] = []
        response = (
            "---FILE:wiki/concepts/concept-1.md---\n"
            "---\ntype: concept\ntitle: Concept 1\n---\nbody\n"
            "---END FILE---\n"
        )

        def _spy(prompt, config, max_tokens=None, label=None):
            prompts.append(prompt)
            return response, "end_turn"

        original = generation.call_anthropic_protocol
        generation.call_anthropic_protocol = _spy
        try:
            with tempfile.TemporaryDirectory() as directory:
                cfg = _config(Path(directory))
                cfg.raw_root.mkdir(parents=True)
                cfg.wiki_dir.mkdir(parents=True)
                generation.stage_2_4_generate_all(
                    [_analysis(0)],
                    cfg.raw_root / "book.pdf",
                    cfg,
                    consolidated_context=context,
                )
        finally:
            generation.call_anthropic_protocol = original

        self.assertEqual(len(prompts), 1)
        self.assertIn(context, prompts[0])
        self.assertIn(
            "Use cross-chunk evidence to keep synthesis",
            prompts[0],
        )


class TestGenerationTokenBudget(unittest.TestCase):
    def test_nashsu_context_ladder(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = _config(Path(directory))
            expectations = {
                64_000: 8_192,
                128_000: 16_384,
                256_000: 24_576,
                512_000: 32_768,
            }
            for context_size, expected in expectations.items():
                with self.subTest(context_size=context_size):
                    cfg.context_size = context_size
                    self.assertEqual(
                        generation._stage_2_4_generation_max_tokens(cfg),
                        expected,
                    )


if __name__ == "__main__":
    unittest.main()
