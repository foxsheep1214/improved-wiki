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

    def test_empty_related_is_preserved(self):
        normalized = s26._normalize_source_frontmatter(
            _page("body"),
            authors_yaml="[]",
            year_yaml='""',
            url_yaml='""',
            venue_yaml='""',
        )
        self.assertIn("related: []", normalized)


if __name__ == "__main__":
    unittest.main()
