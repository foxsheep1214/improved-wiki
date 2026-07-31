"""Stage 2.2 must describe and preserve the actual source type."""
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


def _prompt(tmp: Path, kind: str, nested: bool = False) -> str:
    config = _config(tmp)
    config.wiki_dir.mkdir(parents=True)
    parent = config.raw_root / kind
    if nested:
        parent /= "01_topic"
    return analyze._stage_2_2_build_prompt(
        chunk_text="A focused technical source section.",
        chunk_index=0,
        chunk_total=1,
        global_digest={},
        file_path=parent / "source.pdf",
        config=config,
        template=f"# digest-{kind.lower()}.md\n\nType-specific guidance.",
        existing_slugs=[],
    )


class TestStage22SourceKind(unittest.TestCase):
    def test_nested_paper_path_is_not_mistaken_for_topic_or_book(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt = _prompt(Path(directory), "Paper", nested=True)

        self.assertIn("The source is a **paper**", prompt)
        self.assertNotIn("The source is a **01_topic**", prompt)
        self.assertIn("of a source ingest pipeline", prompt)
        self.assertIn("Analyze THIS CHUNK of the source", prompt)
        self.assertNotIn("book ingest pipeline", prompt)

    def test_paper_metadata_uses_venue_doi_and_url(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt = _prompt(Path(directory), "Paper", nested=True)

        digest_contract = prompt.split("updated_global_digest: |", 1)[1]
        self.assertIn("book_meta:  # compatibility key", digest_contract)
        self.assertIn('source_kind: "paper"', digest_contract)
        self.assertIn('venue: "..."', digest_contract)
        self.assertIn('doi: "..."', digest_contract)
        self.assertIn('url: "..."', digest_contract)
        self.assertNotIn('granularity: "textbook" | "manual"', digest_contract)

    def test_book_metadata_retains_manual_granularity_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt = _prompt(Path(directory), "Book")

        digest_contract = prompt.split("updated_global_digest: |", 1)[1]
        self.assertIn('source_kind: "book"', digest_contract)
        self.assertIn('publisher: "..."', digest_contract)
        self.assertIn('granularity: "textbook" | "manual"', digest_contract)


class TestStage24SourceKind(unittest.TestCase):
    ANALYSIS = {
        "concepts_found": [{
            "name": "Key Method",
            "importance": "core",
            "definition": "A source-grounded method.",
            "key_details": [],
        }],
        "entities_found": [],
        "schema_typed_candidates": [],
        "formulas": [],
    }

    def test_generation_prompts_use_source_neutral_language(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            config = _config(tmp)
            config.wiki_dir.mkdir(parents=True)
            file_path = config.raw_root / "Paper" / "01_topic" / "paper.pdf"
            per_chunk = generation._stage_2_4_build_prompt(
                self.ANALYSIS,
                "A focused paper section.",
                0,
                file_path,
                config,
                template="# digest-paper.md",
            )
            all_chunks = generation._stage_2_4_build_all_prompt(
                [self.ANALYSIS],
                file_path,
                config,
                template="# digest-paper.md",
                source_context="A focused paper section.",
            )

        for prompt in (per_chunk, all_chunks):
            self.assertIn("Source: paper", prompt)
            self.assertNotIn("Book: paper", prompt)
            self.assertNotIn("chunks of a book", prompt)
            self.assertNotIn("chunk of a book", prompt)


if __name__ == "__main__":
    unittest.main()
