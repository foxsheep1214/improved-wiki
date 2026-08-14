"""The source summary is produced by the single Stage 2.4 generation call.

NashSU 0.6.6 has no dedicated source-summary LLM call: `buildGenerationPrompt`
receives `sourceSummaryPath` and the model emits that FILE block alongside the
key/schema-typed pages (ingest.ts:1016). If the block is absent from the write
set afterwards, NashSU writes a deterministic fallback built from the analysis
(ingest.ts:1287) — it never issues a second LLM call.

Stdlib unittest only — no pytest, no network, no LLM calls.
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
import _stage_2_4_generation as gen  # noqa: E402


def _config(tmp: Path) -> _core.Config:
    return _core.Config(
        wiki_root=tmp,
        raw_root=tmp / "raw",
        wiki_dir=tmp / "wiki",
        runtime_dir=tmp / ".llm-wiki",
        cache_path=tmp / ".llm-wiki" / "ingest-cache.json",
        progress_dir=tmp / ".llm-wiki" / "ingest-progress",
        extract_tmp_dir=tmp / ".llm-wiki" / "extract-tmp",
        llm_model="m",
        caption_api_key="",
        caption_base_url="x",
        caption_model="c",
        chunk_overlap=3000,
        source_budget=100_000,
        target_chars=60_000,
        target_tokens=30_000,
        max_tokens=8192,
        context_size=200_000,
    )


def _raw(tmp: Path) -> Path:
    p = tmp / "raw" / "Book" / "Some Book - 2020 - Author.pdf"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"%PDF fake")
    return p


def _analyses() -> list[dict]:
    return [{
        "chunk_index": 1,
        "chunk_total": 1,
        "concepts_found": [{
            "name": "Thermal Resistance",
            "importance": "core",
            "definition": "Temperature rise per unit power.",
            "key_details": ["Series paths add."],
        }],
        "entities_found": [{"name": "JEDEC", "significance": "Standards body."}],
        "claims": [{"claim": "R rises with area.", "evidence": "Fig 2.1",
                    "confidence": "high"}],
        "formulas": [],
        "connections_to_existing_wiki": [],
        "schema_typed_candidates": [],
        "updated_global_digest": {"book_meta": {"title": "Some Book"}},
    }]


class GenerationPromptRequestsTheSourcePage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = _config(self.tmp)
        self.raw = _raw(self.tmp)
        self.prompt = gen._stage_2_4_build_all_prompt(
            _analyses(), self.raw, self.cfg,
            consolidated_context="CTX-PAYLOAD",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_prompt_names_the_exact_source_page_path(self):
        self.assertIn(
            "---FILE:wiki/sources/Book/Some Book - 2020 - Author.md---",
            self.prompt,
        )

    def test_prompt_carries_the_source_summary_body_guidance(self):
        # The source page is free-form and synthesized, not an
        # exhaustive inventory of everything the analysis found.
        for phrase in (
            "scope, approach, and intended audience",
            "exhaustive inventory",
            "no heading-count",
        ):
            self.assertIn(phrase, self.prompt)

    def test_prompt_declares_the_source_page_mandatory_despite_the_sentinel(self):
        # NO_KEY_PAGES means "no key/schema-typed page qualifies". It must NOT
        # be readable as permission to skip the source summary, which is
        # unconditional.
        self.assertIn(gen._NO_KEY_PAGES_SENTINEL, self.prompt)
        lowered = self.prompt.lower()
        self.assertIn("source page is mandatory", lowered)

    def test_prompt_requests_the_bibliographic_frontmatter_fields(self):
        for field in ("authors:", "year:", "url:", "venue:"):
            self.assertIn(field, self.prompt)

    def test_context_payload_appears_exactly_once(self):
        # The whole point of the merge: one call, one copy of the context.
        self.assertEqual(self.prompt.count("CTX-PAYLOAD"), 1)

    def test_yaml_string_digest_prefills_human_source_title(self):
        analyses = _analyses()
        analyses[0]["updated_global_digest"] = (
            "book_meta:\n"
            "  title: \"Some Book\"\n"
            "  authors: [\"A. Author\"]\n"
            "  year: 2020\n"
            "  publisher: \"A Press\"\n"
        )
        prompt = gen._stage_2_4_build_all_prompt(
            analyses, self.raw, self.cfg,
            consolidated_context="CTX-PAYLOAD",
        )
        self.assertIn('title: "Some Book"', prompt)
        self.assertNotIn(
            'title: "Book/Some Book - 2020 - Author"', prompt)
        self.assertIn('authors: ["A. Author"]', prompt)

    def test_digest_bibliography_is_yaml_safe_in_source_template(self):
        analyses = _analyses()
        title = '9.2 "Forward" and "Flyback"'
        analyses[0]["updated_global_digest"] = (
            "book_meta:\n"
            f"  title: '{title}'\n"
            "  authors: ['A. \"Quoted\" Author']\n"
            "  year: 2020\n"
            "  publisher: 'Press \"X\"'\n"
        )
        prompt = gen._stage_2_4_build_all_prompt(
            analyses, self.raw, self.cfg,
            consolidated_context="CTX-PAYLOAD",
        )
        marker = (
            "---FILE:wiki/sources/Book/"
            "Some Book - 2020 - Author.md---"
        )
        after = prompt.split(marker, 1)[1].lstrip()
        self.assertTrue(after.startswith("---\n"))
        frontmatter = after[4:].split("\n---", 1)[0]
        import yaml
        parsed = yaml.safe_load(frontmatter)
        self.assertEqual(title, parsed["title"])
        self.assertEqual(['A. "Quoted" Author'], parsed["authors"])
        self.assertEqual('Press "X"', parsed["venue"])


class SourcePageStemHelper(unittest.TestCase):
    def test_stem_mirrors_the_raw_relative_path(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg, raw = _config(tmp), _raw(tmp)
            self.assertEqual(
                gen.source_page_rel_stem(raw, cfg),
                "Book/Some Book - 2020 - Author",
            )

    def test_stem_falls_back_to_bare_name_outside_raw_root(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _config(tmp)
            outside = tmp / "elsewhere" / "Loose File.pdf"
            outside.parent.mkdir(parents=True, exist_ok=True)
            outside.write_bytes(b"x")
            self.assertEqual(
                gen.source_page_rel_stem(outside, cfg), "Loose File")


class SingleGenerationCallContract(unittest.TestCase):
    def test_no_dedicated_source_page_llm_call_remains(self):
        import _ingest_prepare
        self.assertFalse(
            hasattr(_ingest_prepare, "stage_2_6_source_page"),
            "a separate source-page LLM call must not be wired in",
        )

    def test_deterministic_fallback_is_still_available(self):
        # NashSU keeps the fallback (ingest.ts:1287) even without the stage —
        # it is what guarantees a source page exists when the model omits it.
        from _source_page import build_fallback_source_summary
        out = build_fallback_source_summary(
            "Book/X", "raw/Book/X.pdf", "ANALYSIS TEXT", "2026-08-01")
        self.assertIn("---FILE:wiki/sources/Book/X.md---", out)
        self.assertIn("---END FILE---", out)

class GeneratedSourceBlockIsNormalizedAndGated(unittest.TestCase):
    """The structural gate and frontmatter repair run on Stage 2.4 output.

    They fill blank bibliographic fields from the digest and reject malformed,
    duplicated, or empty source blocks.
    """

    def setUp(self):
        import _ingest_prepare
        self.prep = _ingest_prepare
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = _config(self.tmp)
        self.raw = _raw(self.tmp)
        self.digest = {"book_meta": {
            "title": "Some Book", "authors": ["Author"], "year": 2020,
            "publisher": "A Press",
        }}

    def tearDown(self):
        self._tmp.cleanup()

    def _block(self, body="# Some Book\n\nA grounded summary.", fm=None):
        fm = fm if fm is not None else (
            "---\ntype: source\ntitle: \"Some Book\"\n"
            "tags: [x]\nrelated: []\n"
            "sources: [\"raw/Book/Some Book - 2020 - Author.pdf\"]\n"
            "created: 2026-08-01\nupdated: 2026-08-01\n"
            "authors: []\nyear: \"\"\nurl: \"\"\nvenue: \"\"\n---\n\n"
        )
        return ("sources/Book/Some Book - 2020 - Author.md", fm + body)

    def test_blank_bibliographic_fields_are_filled_from_the_digest(self):
        blocks, missing = self.prep._ensure_source_page(
            self.digest, self.raw, self.cfg, [self._block()])
        self.assertFalse(missing)
        content = blocks[0][1]
        self.assertIn('authors: ["Author"]', content)
        self.assertIn("year: 2020", content)
        self.assertIn('venue: "A Press"', content)

    def test_source_block_without_frontmatter_is_rejected(self):
        bad = ("sources/Book/Some Book - 2020 - Author.md",
               "# Some Book\n\nNo frontmatter at all.\n")
        blocks, missing = self.prep._ensure_source_page(
            self.digest, self.raw, self.cfg, [bad])
        # Falls back deterministically rather than writing a frontmatter-less page.
        self.assertTrue(missing)
        self.assertTrue(blocks[0][1].lstrip().startswith("---"))

    def test_malformed_nonempty_yaml_fields_force_safe_fallback(self):
        malformed_fm = (
            "---\ntype: source\n"
            'title: "9.2 "Forward" and "Flyback""\n'
            "tags: [x]\nrelated: []\n"
            'sources: ["raw/Book/Some "Quoted" Book.pdf"]\n'
            "created: 2026-08-01\nupdated: 2026-08-01\n"
            'authors: ["A. "Quoted" Author"]\n'
            "year: 2020\nurl: \"\"\n"
            'venue: "Press "X""\n---\n\n'
        )
        blocks, missing = self.prep._ensure_source_page(
            self.digest, self.raw, self.cfg,
            [self._block(fm=malformed_fm)],
        )
        self.assertTrue(missing)
        content = blocks[0][1]
        import yaml
        frontmatter = content[4:].split("\n---", 1)[0]
        parsed = yaml.safe_load(frontmatter)
        self.assertIsInstance(parsed, dict)
        self.assertEqual("source", parsed["type"])

    def test_duplicate_source_blocks_are_collapsed_to_one(self):
        blocks, _missing = self.prep._ensure_source_page(
            self.digest, self.raw, self.cfg,
            [self._block(), self._block("# Some Book\n\nA second one.")])
        paths = [p for p, _ in blocks]
        self.assertEqual(
            paths.count("sources/Book/Some Book - 2020 - Author.md"), 1)

    def test_absent_source_block_still_falls_back(self):
        blocks, missing = self.prep._ensure_source_page(
            self.digest, self.raw, self.cfg,
            [("concepts/x.md", "---\ntype: concept\n---\n\n# X\n")])
        self.assertTrue(missing)
        self.assertTrue(any(
            p.endswith("sources/Book/Some Book - 2020 - Author.md")
            for p, _ in blocks))

if __name__ == "__main__":
    unittest.main()


class QueryBridgeGetsNoSourcePage(unittest.TestCase):
    """A deep-research page under wiki/queries/ must NOT be asked for a source
    page — the query page IS the artifact (is_query_bridge_source docstring:
    "it should not get its own wiki/sources/ digest page from Stage 2.4").

    Regression 2026-08-01→08-03: merging Stage 2.6 into 2.4 kept the
    query-bridge skip on the post-write check (_ensure_source_page) but not on
    the prompt, which still demanded a mandatory source page. The model
    complied, the block landed in file_blocks before the skip could apply, and
    four bogus wiki/sources/research-*.md pages were written on RadarWiki —
    each citing a raw/research-*.md file that does not exist, because
    source_page_rel_stem falls back to the bare stem outside raw_root.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = _config(self.tmp)
        self.query_page = self.cfg.wiki_dir / "queries" / "research-x-2026-08-02.md"
        self.query_page.parent.mkdir(parents=True, exist_ok=True)
        self.query_page.write_text("# Research X\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_prompt_omits_the_mandatory_source_page_section(self):
        prompt = gen._stage_2_4_build_all_prompt(
            _analyses(), self.query_page, self.cfg,
            consolidated_context="CTX")
        self.assertNotIn("MANDATORY Source Page", prompt)
        self.assertNotIn("source page is mandatory", prompt.lower())
        self.assertNotIn("---FILE:wiki/sources/", prompt)

    def test_prompt_never_invents_a_nonexistent_raw_path(self):
        prompt = gen._stage_2_4_build_all_prompt(
            _analyses(), self.query_page, self.cfg,
            consolidated_context="CTX")
        self.assertNotIn("raw/research-x-2026-08-02.md", prompt)

    def test_normal_source_still_gets_the_section(self):
        prompt = gen._stage_2_4_build_all_prompt(
            _analyses(), _raw(self.tmp), self.cfg, consolidated_context="CTX")
        self.assertIn("MANDATORY Source Page", prompt)
