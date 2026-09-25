"""Stage 2.3 semantic nominations: existing pages close in meaning to a
candidate that the lexical title pass did not match, shown to Stage 2.4 as
POSSIBLY ALREADY EXISTS for the model to judge."""
from __future__ import annotations

import math
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _core  # noqa: E402
import _stage_2_3_incremental as s23  # noqa: E402
import _stage_2_4_generation as s24  # noqa: E402


def _unit(angle_deg: float) -> list[float]:
    """Unit vector in the x-y plane; cosine to [1, 0, 0] is cos(angle)."""
    a = math.radians(angle_deg)
    return [math.cos(a), math.sin(a), 0.0]


_UNRELATED = [0.0, 0.0, 1.0]   # orthogonal to every indexed page


def _config(root: Path) -> SimpleNamespace:
    return SimpleNamespace(wiki_root=root, wiki_dir=root / "wiki",
                           runtime_dir=root / ".llm-wiki")


def _index(root: Path, rows: list[tuple[str, int, str, float]]) -> None:
    import lancedb

    lance = root / ".llm-wiki" / "lancedb"
    lance.mkdir(parents=True)
    lancedb.connect(str(lance)).create_table("wiki_chunks", [
        {"chunk_id": f"{pid}#{idx}", "page_id": pid, "chunk_index": idx,
         "title": title, "path": f"{pid}.md", "chunk_text": title,
         "heading_path": "", "vector": _unit(angle)}
        for pid, idx, title, angle in rows
    ])


_ANALYSES = [{
    "concepts_found": [
        {"name": "降压变换器", "definition": "把高压直流降到低压直流", "importance": "core"},
        {"name": "Snubber Design", "definition": "RC damping of ringing", "importance": "core"},
    ],
    "entities_found": [{"name": "Texas Instruments", "significance": "vendor"}],
}]


class SemanticNominationTests(unittest.TestCase):
    def test_nominates_same_route_pages_above_the_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _index(root, [
                ("concepts/buck-converter", 0, "Buck Converter", 20.0),     # cos .94
                ("concepts/buck-converter", 1, "Buck Converter", 0.0),      # not chunk 0
                ("concepts/boost-converter", 0, "Boost Converter", 55.0),   # cos .57
                ("entities/buck-converter-ic", 0, "Buck IC", 5.0),          # other route
            ])
            vectors = {"降压变换器": _unit(0.0), "Snubber Design": _UNRELATED,
                       "Texas Instruments": _UNRELATED}
            with (mock.patch("build_embeddings.embedding_config_from_env",
                             return_value=None),
                  mock.patch("build_embeddings.embed_with_config",
                             side_effect=lambda texts, _c: [
                                 vectors[t.split(":")[0]] for t in texts])):
                matches = s23.stage_2_3_semantic_candidates(
                    _config(root), _ANALYSES, associations={})

        self.assertEqual(list(matches), ["降压变换器"])
        self.assertEqual(matches["降压变换器"], [
            {"slug": "concepts/buck-converter", "title": "Buck Converter",
             "cosine": 0.94}])

    def test_lexically_associated_names_are_not_queried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _index(root, [("concepts/buck-converter", 0, "Buck Converter", 0.0)])
            embed = mock.Mock(side_effect=lambda texts, _c: [_unit(0.0)] * len(texts))
            with (mock.patch("build_embeddings.embedding_config_from_env",
                             return_value=None),
                  mock.patch("build_embeddings.embed_with_config", embed)):
                s23.stage_2_3_semantic_candidates(
                    _config(root), _ANALYSES,
                    associations={"降压变换器": ["concepts/buck-converter"]})
        texts = embed.call_args.args[0]
        self.assertFalse(any(t.startswith("降压变换器") for t in texts))

    def test_no_vector_index_means_no_nominations(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                s23.stage_2_3_semantic_candidates(
                    _config(Path(tmp)), _ANALYSES, associations={}), {})


class GenerationPromptTests(unittest.TestCase):
    def test_prompt_marks_nominations_and_makes_them_linkable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _core.Config(
                wiki_root=root, raw_root=root / "raw", wiki_dir=root / "wiki",
                runtime_dir=root / "rt", cache_path=root / "rt" / "c.json",
                progress_dir=root / "rt" / "p", extract_tmp_dir=root / "rt" / "e",
                llm_model="m", caption_api_key="", caption_base_url="x",
                caption_model="c", chunk_overlap=3000, source_budget=100000,
                target_chars=60000, target_tokens=30000, max_tokens=8192,
                conversation_prefix="ab12cd34",
            )
            config.wiki_dir.mkdir()
            config.raw_root.mkdir()
            prompt = s24._stage_2_4_build_all_prompt(
                _ANALYSES, root / "raw" / "book.pdf", config,
                semantic_matches={"降压变换器": [{
                    "slug": "concepts/buck-converter",
                    "title": "Buck Converter", "cosine": 0.94}]},
            )
        self.assertIn("POSSIBLY ALREADY EXISTS", prompt)
        self.assertIn('[[concepts/buck-converter]] "Buck Converter" (cosine 0.94)',
                      prompt)
        linkable = prompt.split("# Linkable pages", 1)[1]
        self.assertIn("concepts/buck-converter", linkable)
        self.assertIn("same subject", prompt)


if __name__ == "__main__":
    unittest.main()
