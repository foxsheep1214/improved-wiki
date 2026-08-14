"""NashSU 0.6.6 synthesis/thesis ingest-policy regressions."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _core  # noqa: E402
import _ingest_chunks as chunks  # noqa: E402
import _schema  # noqa: E402
import _stage_2_4_generation as generation  # noqa: E402
import _stage_2_analyze as analyze  # noqa: E402


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


def _write_research_schema(tmp: Path) -> None:
    template = SCRIPTS_DIR.parent / "templates" / "schema.md"
    (tmp / "schema.md").write_text(
        template.read_text(encoding="utf-8"),
        encoding="utf-8",
    )


class TestNashSUSynthesisThesisAnalysis(unittest.TestCase):
    def test_analysis_gets_frozen_index_and_living_page_policy(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _config(tmp)
            cfg.wiki_dir.mkdir(parents=True)
            _write_research_schema(tmp)
            prompt = analyze._stage_2_2_build_prompt(
                chunk_text="The source advances a falsifiable aperture hypothesis.",
                chunk_index=0,
                chunk_total=1,
                global_digest={},
                file_path=cfg.raw_root / "book.pdf",
                config=cfg,
                existing_slugs=["bandwidth-predicts-resolution"],
                wiki_index_context=(
                    "# Wiki Index\n\n## Thesis\n"
                    "- [[thesis/bandwidth-predicts-resolution]]"
                ),
            )

        self.assertIn("Current Wiki Index (FROZEN FOR THIS SOURCE)", prompt)
        self.assertIn("thesis/bandwidth-predicts-resolution", prompt)
        self.assertIn("may seed a `speculative` thesis", prompt)
        self.assertIn("current source may seed a new synthesis", prompt)
        self.assertIn("unless the project schema explicitly requires", prompt)
        self.assertNotIn(
            "A synthesis candidate still requires actual multi-source evidence",
            prompt,
        )

    def test_index_titles_are_not_promoted_to_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _config(tmp)
            cfg.wiki_dir.mkdir(parents=True)
            _write_research_schema(tmp)
            block = analyze._stage_2_2_schema_types_block(
                cfg,
                wiki_index_context=(
                    "## Synthesis\n- [[synthesis/unified-aperture-view]]"
                ),
            )
        self.assertIn("navigation context, not factual evidence", block)
        self.assertIn("Never infer cross-source facts from index titles alone", block)


class TestNashSUIndexSnapshot(unittest.TestCase):
    def test_large_index_keeps_late_synthesis_and_thesis_sections(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _config(tmp)
            cfg.wiki_dir.mkdir(parents=True)
            filler = "\n".join(
                f"- [[concepts/concept-{i}]] — description"
                for i in range(3000)
            )
            (cfg.wiki_dir / "index.md").write_text(
                "# Wiki Index\n\n## Concepts（概念）\n"
                f"{filler}\n\n"
                "## Synthesis（综合）\n"
                "- [[synthesis/unified-aperture-view]] — existing synthesis\n\n"
                "## Thesis（论题）\n"
                "- [[thesis/bandwidth-predicts-resolution]] — living thesis\n",
                encoding="utf-8",
            )
            context = _schema.load_wiki_index_context(cfg)

        self.assertLessEqual(len(context), 40_000)
        self.assertIn("Priority Existing Synthesis/Thesis Index Sections", context)
        self.assertIn("synthesis/unified-aperture-view", context)
        self.assertIn("thesis/bandwidth-predicts-resolution", context)
        self.assertIn("Current Wiki Index (prefix)", context)

    def test_chunk_driver_forwards_same_frozen_index_to_analysis(self):
        seen: list[str] = []

        def fake_analyze(*args, **kwargs):
            seen.append(kwargs["wiki_index_context"])
            return {
                "updated_global_digest": {
                    "book_meta": {},
                    "outline": [],
                    "key_entities": [],
                    "key_concepts": [],
                    "key_claims": [],
                },
            }

        with tempfile.TemporaryDirectory() as d:
            cfg = _config(Path(d))
            with patch.object(chunks, "_stage_2_2_analyze_chunk", fake_analyze):
                chunks._analyze_all_chunks(
                    [(0, "chunk", "", "")],
                    {},
                    "",
                    cfg.raw_root / "book.pdf",
                    cfg,
                    "",
                    1,
                    0.0,
                    False,
                    existing_slugs=[],
                    wiki_index_context="FROZEN INDEX",
                )

        self.assertEqual(seen, ["FROZEN INDEX"])


class TestNashSUSynthesisThesisGeneration(unittest.TestCase):
    def test_generation_keeps_recommended_living_pages(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _config(tmp)
            cfg.wiki_dir.mkdir(parents=True)
            cfg.raw_root.mkdir(parents=True)
            _write_research_schema(tmp)
            prompt = generation._stage_2_4_build_all_prompt(
                [{
                    "concepts_found": [],
                    "entities_found": [],
                    "schema_typed_candidates": [
                        {
                            "type": "synthesis",
                            "name": "Unified Aperture View",
                            "folder": "synthesis",
                            "rationale": "Connects the source's material findings.",
                        },
                        {
                            "type": "thesis",
                            "name": "Bandwidth Predicts Resolution",
                            "folder": "thesis",
                            "rationale": "The source advances a falsifiable hypothesis.",
                        },
                    ],
                    "formulas": [],
                }],
                cfg.raw_root / "book.pdf",
                cfg,
                source_context="source text",
            )

        self.assertIn("synthesis/unified-aperture-view", prompt)
        self.assertIn("thesis/bandwidth-predicts-resolution", prompt)
        self.assertIn(
            "A recommended synthesis/thesis candidate has already passed",
            prompt,
        )
        self.assertIn("A first source may seed the page", prompt)
        self.assertIn("status: speculative", prompt)
        self.assertIn("do not fabricate other sources from index titles", prompt)

    def test_analysis_policy_version_invalidates_old_candidate_cache(self):
        self.assertEqual(
            chunks.ANALYSIS_POLICY_VERSION,
            "nashsu-0.6.6-schema-typed-v3-synthesis-thesis",
        )


if __name__ == "__main__":
    unittest.main()
