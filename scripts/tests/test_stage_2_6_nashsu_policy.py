"""Stage 2.6 follows NashSU's free-form source-summary contract."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _core  # noqa: E402
import _stage_2_6_source_page as s26  # noqa: E402


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


def _page(body: str, path: str = "wiki/sources/book.md") -> str:
    return (
        f"---FILE:{path}---\n"
        "---\n"
        "type: source\n"
        "title: Book\n"
        "tags: []\n"
        "related: []\n"
        "sources: [\"raw/book.pdf\"]\n"
        "---\n\n"
        f"{body}\n"
        "---END FILE---\n"
    )


class TestStructuralGate(unittest.TestCase):
    def test_arbitrary_useful_structure_is_valid(self):
        s26._stage_2_6_validate_source_file_block(
            _page("## Why this matters\n\nA concise synthesis."), "book"
        )

    def test_no_h2_heading_is_valid(self):
        s26._stage_2_6_validate_source_file_block(
            _page("A short but substantive source summary."), "book"
        )

    def test_wrong_path_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            s26._stage_2_6_validate_source_file_block(
                _page("body", "wiki/sources/other.md"), "book"
            )

    def test_multiple_blocks_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            s26._stage_2_6_validate_source_file_block(
                _page("body") + _page("other", "wiki/sources/other.md"),
                "book",
            )

    def test_missing_end_marker_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "END FILE"):
            s26._stage_2_6_validate_source_file_block(
                _page("body").replace("---END FILE---", ""), "book"
            )

    def test_empty_body_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "non-empty body"):
            s26._stage_2_6_validate_source_file_block(_page(""), "book")


class TestPromptPolicy(unittest.TestCase):
    def test_prompt_has_no_inventory_or_count_contract(self):
        digest = {
            "book_meta": {"title": "Book"},
            "outline": ["Chapter 1"],
            "key_concepts": [{"name": "Core Method"}],
            "key_entities": [{"name": "Example System"}],
            "key_claims": [{"claim": "A core result", "evidence": "§1"}],
        }
        prompts: list[str] = []

        def _spy(prompt, config, max_tokens=None, label=None):
            prompts.append(prompt)
            return _page("## Synthesis\n\nOnly the core material."), "end_turn"

        original = s26.call_anthropic_protocol
        s26.call_anthropic_protocol = _spy
        try:
            with tempfile.TemporaryDirectory() as d:
                tmp = Path(d)
                cfg = _config(tmp)
                cfg.raw_root.mkdir(parents=True)
                cfg.wiki_dir.mkdir(parents=True)
                response, _ = s26.stage_2_6_source_page(
                    digest,
                    cfg.raw_root / "book.pdf",
                    cfg,
                    linkable_slugs=[
                        "concepts/core-method",
                        "entities/example-system",
                    ],
                    generated_concepts=["concepts/core-method"],
                    generated_entities=["entities/example-system"],
                    chunk_claims=[
                        {"claim": "A core result", "evidence": "§1"},
                        {"claim": "A duplicate result", "evidence": "§1"},
                    ],
                )
        finally:
            s26.call_anthropic_protocol = original

        self.assertIn("Only the core material", response)
        self.assertEqual(len(prompts), 1)
        prompt = prompts[0]
        self.assertIn("no heading-count, concept-count, or claim-count target", prompt)
        self.assertIn("Do not list every", prompt)
        self.assertIn("do not reproduce the list wholesale", prompt)
        self.assertNotIn("list EVERY", prompt)
        self.assertNotIn("Include **EVERY", prompt)
        self.assertNotIn("aim for 5-15", prompt.lower())

    def test_prompt_uses_consolidated_whole_source_context(self):
        digest = {
            "book_meta": {"title": "Book"},
            "outline": ["Early", "Late"],
            "key_concepts": [],
            "key_entities": [],
            "key_claims": [],
        }
        context = (
            "# Consolidated Stage 2 Context\n"
            "## Final Global Digest\nFINAL-DIGEST\n"
            "## Per-Chunk Analyses\nLATE-CHUNK-ANALYSIS\n"
            "## Bounded Raw Source Evidence\nLATE-RAW-EVIDENCE"
        )
        prompts: list[str] = []

        def _spy(prompt, config, max_tokens=None, label=None):
            prompts.append(prompt)
            return _page("Grounded whole-source summary."), "end_turn"

        original = s26.call_anthropic_protocol
        s26.call_anthropic_protocol = _spy
        try:
            with tempfile.TemporaryDirectory() as d:
                tmp = Path(d)
                cfg = _config(tmp)
                cfg.raw_root.mkdir(parents=True)
                cfg.wiki_dir.mkdir(parents=True)
                s26.stage_2_6_source_page(
                    digest,
                    cfg.raw_root / "book.pdf",
                    cfg,
                    consolidated_context=context,
                    chunk_claims=[{
                        "claim": "duplicate claim section must be suppressed",
                        "evidence": "late",
                    }],
                )
        finally:
            s26.call_anthropic_protocol = original

        self.assertEqual(len(prompts), 1)
        self.assertIn(context, prompts[0])
        self.assertIn("ground the summary in the WHOLE source", prompts[0])
        self.assertNotIn(
            "# Claim candidates from per-chunk analysis",
            prompts[0],
        )

    def test_empty_related_is_preserved(self):
        normalized = s26._normalize_source_frontmatter(
            _page("body"),
            authors_yaml="[]",
            year_yaml='""',
            url_yaml='""',
            venue_yaml='""',
        )
        self.assertIn("related: []", normalized)

    def test_known_metadata_replaces_blank_generated_values(self):
        response = _page("body").replace(
            "sources: [\"raw/book.pdf\"]\n",
            "sources: [\"raw/book.pdf\"]\n"
            "authors: []\n"
            "year: \"\"\n"
            "url: \"\"\n"
            "venue: \"\"\n",
        )
        normalized = s26._normalize_source_frontmatter(
            response,
            authors_yaml='["A. Author"]',
            year_yaml="2024",
            url_yaml='"https://doi.org/10.1/example"',
            venue_yaml='"IET Radar"',
        )
        self.assertIn('authors: ["A. Author"]', normalized)
        self.assertIn("year: 2024", normalized)
        self.assertIn('url: "https://doi.org/10.1/example"', normalized)
        self.assertIn('venue: "IET Radar"', normalized)

    def test_specific_paper_meta_overrides_compatibility_meta(self):
        digest = {
            "book_meta": {
                "title": "Paper",
                "authors": [],
                "year": "2024",
                "publisher": "Generic Publisher",
            },
            "paper_meta": {
                "title": "Paper",
                "authors": ["A. Author"],
                "year": "2023",
                "venue": "IET Radar",
                "doi": "10.1/example",
            },
            "outline": [],
            "key_concepts": [],
            "key_entities": [],
            "key_claims": [],
        }

        def _spy(prompt, config, max_tokens=None, label=None):
            return _page(
                "Grounded summary.",
                "wiki/sources/Paper/paper.md",
            ), "end_turn"

        original = s26.call_anthropic_protocol
        s26.call_anthropic_protocol = _spy
        try:
            with tempfile.TemporaryDirectory() as directory:
                tmp = Path(directory)
                cfg = _config(tmp)
                (cfg.raw_root / "Paper").mkdir(parents=True)
                cfg.wiki_dir.mkdir(parents=True)
                response, _ = s26.stage_2_6_source_page(
                    digest,
                    cfg.raw_root / "Paper" / "paper.pdf",
                    cfg,
                    template="# digest-paper.md",
                )
        finally:
            s26.call_anthropic_protocol = original

        self.assertIn('authors: ["A. Author"]', response)
        self.assertIn("year: 2023", response)
        self.assertIn('url: "https://doi.org/10.1/example"', response)
        self.assertIn('venue: "IET Radar"', response)


class TestSourceFallbackAndTruncationRepair(unittest.TestCase):
    DIGEST = {
        "book_meta": {"title": "Book"},
        "outline": [],
        "key_concepts": [],
        "key_entities": [],
        "key_claims": [],
    }

    def _run(self, responses, chunk_analyses=None):
        calls: list[str] = []

        def _spy(prompt, config, max_tokens=None, label=None):
            index = len(calls)
            calls.append(prompt)
            return responses[min(index, len(responses) - 1)]

        original = s26.call_anthropic_protocol
        s26.call_anthropic_protocol = _spy
        try:
            with tempfile.TemporaryDirectory() as d:
                tmp = Path(d)
                cfg = _config(tmp)
                cfg.raw_root.mkdir(parents=True)
                cfg.wiki_dir.mkdir(parents=True)
                result = s26.stage_2_6_source_page(
                    self.DIGEST,
                    cfg.raw_root / "book.pdf",
                    cfg,
                    chunk_analyses=chunk_analyses,
                )
        finally:
            s26.call_anthropic_protocol = original
        return result, calls

    def test_missing_source_block_gets_deterministic_fallback(self):
        tail = "TAIL-OF-COMPLETE-ANALYSIS"
        (response, stop_reason), calls = self._run(
            [("The model omitted its FILE block.", "end_turn")],
            chunk_analyses=[{"chunk_index": 1, "evidence": tail}],
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(stop_reason, "fallback-source-summary")
        self.assertIn("Source: raw/book.pdf", response)
        self.assertIn(tail, response)
        s26._stage_2_6_validate_source_file_block(response, "book")

    def test_truncated_source_block_is_targeted_then_recovered(self):
        truncated = (
            "---FILE:wiki/sources/book.md---\n"
            "---\ntype: source\ntitle: Book\n---\npartial"
        )
        (response, stop_reason), calls = self._run([
            (truncated, "end_turn"),
            (_page("## Repaired\n\nComplete source page."), "end_turn"),
        ])
        self.assertEqual(len(calls), 2)
        self.assertEqual(stop_reason, "end_turn")
        self.assertIn("Complete source page.", response)
        self.assertIn("- wiki/sources/book.md", calls[1])
        self.assertIn("repairing truncated wiki FILE blocks", calls[1])

    def test_unrecovered_source_block_falls_back_with_full_analysis(self):
        tail = "ANALYSIS-TAIL-MUST-SURVIVE"
        truncated = "---FILE:wiki/sources/book.md---\npartial"
        (response, stop_reason), calls = self._run(
            [
                (truncated, "end_turn"),
                ("repair also malformed", "end_turn"),
            ],
            chunk_analyses=[{
                "chunk_index": 1,
                "large": "x" * 20_000 + tail,
            }],
        )
        self.assertEqual(len(calls), 2)
        # Distinct from the malformed-block fallback above: an unrecovered
        # truncation is a lost LLM turn, so the caller must skip the cache and
        # let the next ingest regenerate the page (NashSU ingest.ts:1326-1341).
        # A merely malformed block is a formatting miss and caches normally.
        self.assertEqual(
            stop_reason, "fallback-source-summary-unrecovered-truncation")
        self.assertIn(tail, response)
        self.assertNotIn("... (truncated)", response)


if __name__ == "__main__":
    unittest.main()
