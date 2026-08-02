"""Source-page contracts for the block emitted by Stage 2.4.

Three guarantees run before write:
  * the structural gate (`_validate_source_file_block`);
  * frontmatter repair (`_normalize_source_frontmatter`);
  * bibliographic pre-fill, which moved to `_source_bibliographic_fields`.
Prompt wording is covered in test_source_page_in_generation.py; FILE-block
truncation repair is Stage 2.4's, covered in test_file_block_repair.py.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _core  # noqa: E402
import _source_page as source_page  # noqa: E402


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
        source_page._validate_source_file_block(
            _page("## Why this matters\n\nA concise synthesis."), "book"
        )

    def test_no_h2_heading_is_valid(self):
        source_page._validate_source_file_block(
            _page("A short but substantive source summary."), "book"
        )

    def test_wrong_path_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            source_page._validate_source_file_block(
                _page("body", "wiki/sources/other.md"), "book"
            )

    def test_multiple_blocks_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            source_page._validate_source_file_block(
                _page("body") + _page("other", "wiki/sources/other.md"),
                "book",
            )

    def test_missing_end_marker_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "END FILE"):
            source_page._validate_source_file_block(
                _page("body").replace("---END FILE---", ""), "book"
            )

    def test_empty_body_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "non-empty body"):
            source_page._validate_source_file_block(_page(""), "book")


class TestPromptPolicy(unittest.TestCase):
    def test_empty_related_is_preserved(self):
        normalized = source_page._normalize_source_frontmatter(
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
        normalized = source_page._normalize_source_frontmatter(
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
        """A type-specific ``*_meta`` block wins over the compatibility
        ``book_meta``, and a bare DOI becomes a URL. The shared
        ``_source_bibliographic_fields`` helper pre-fills those fields in the
        Stage 2.4 prompt."""
        from _stage_2_4_generation import _source_bibliographic_fields
        bib = _source_bibliographic_fields({
            "book_meta": {"title": "Compat", "authors": ["Ignored"]},
            "paper_meta": {
                "title": "Real Paper", "authors": ["A. Author"],
                "year": 2021, "doi": "10.1000/xyz", "venue": "IEEE TAP",
            },
        }, "Paper/real-paper")
        self.assertEqual(bib["title"], "Real Paper")
        self.assertEqual(bib["authors"], '["A. Author"]')
        self.assertEqual(bib["year"], "2021")
        self.assertEqual(bib["url"], '"https://doi.org/10.1000/xyz"')
        self.assertEqual(bib["venue"], '"IEEE TAP"')

    def test_publisher_folds_into_venue_when_no_venue(self):
        from _stage_2_4_generation import _source_bibliographic_fields
        bib = _source_bibliographic_fields(
            {"book_meta": {"title": "B", "publisher": "A Press"}}, "Book/b")
        self.assertEqual(bib["venue"], '"A Press"')

if __name__ == "__main__":
    unittest.main()
