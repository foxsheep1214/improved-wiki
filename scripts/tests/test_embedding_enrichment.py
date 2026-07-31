"""Embedding enrichment + NashSU 0.6.6 Markdown chunker regressions.

Stdlib unittest only — no pytest, no network, no embedding backend.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_embeddings as be  # noqa: E402


class TestEnrichForEmbedding(unittest.TestCase):
    def test_joins_title_breadcrumb_and_body(self):
        self.assertEqual(
            "Matched Filter\n\nTheory > Derivation\n\nCorrelates the echo.",
            be.enrich_for_embedding(
                "Matched Filter", "Theory > Derivation", "Correlates the echo."),
        )

    def test_omits_empty_parts(self):
        self.assertEqual(
            "Matched Filter\n\nbody",
            be.enrich_for_embedding("Matched Filter", "", "body"))
        self.assertEqual("body", be.enrich_for_embedding("", "   ", "body"))


class TestHeadingPathAt(unittest.TestCase):
    BODY = (
        "# Range Resolution\n\nintro\n\n"
        "## Theory\n\nt-body\n\n"
        "### Derivation\n\nd-body\n\n"
        "## Practice\n\np-body\n"
    )

    def test_breadcrumb_tracks_the_open_stack(self):
        at = lambda needle: be.heading_path_at(  # noqa: E731
            self.BODY, self.BODY.index(needle))
        self.assertEqual("# Range Resolution", at("intro"))
        self.assertEqual("# Range Resolution > ## Theory", at("t-body"))
        self.assertEqual(
            "# Range Resolution > ## Theory > ### Derivation", at("d-body"))
        # A sibling H2 pops the deeper level rather than accumulating it.
        self.assertEqual("# Range Resolution > ## Practice", at("p-body"))

    def test_no_heading_yields_empty(self):
        self.assertEqual("", be.heading_path_at("just prose\n", 3))


class TestMarkdownChunker(unittest.TestCase):
    def test_short_page_is_one_chunk(self):
        text = "# Title\n\nshort body\n"
        chunks = be.chunk_markdown(text)
        self.assertEqual(1, len(chunks))
        self.assertEqual(text, chunks[0].text)

    def test_blank_page_yields_nothing(self):
        self.assertEqual([], be.chunk_markdown("   \n\n  \n"))

    def test_table_is_never_split(self):
        table_rows = "\n".join(
            f"| PARAM{i} | {i} mV | typ |" for i in range(120))
        text = (
            "# Datasheet\n\n" + "filler paragraph.\n\n" * 40
            + "| Name | Value | Note |\n|---|---|---|\n" + table_rows
            + "\n\ntrailing prose\n"
        )
        chunks = be.chunk_markdown(text)
        # NashSU overlap may repeat the table's tail in the following chunk;
        # the complete atomic table itself must still occur exactly once.
        table_chunks = [
            chunk for chunk in chunks
            if "| Name | Value | Note |" in chunk.text
        ]
        self.assertEqual(1, len(table_chunks))
        self.assertIn("| Name | Value | Note |", table_chunks[0].text)
        self.assertIn("| PARAM119 |", table_chunks[0].text)
        self.assertTrue(table_chunks[0].oversized)

    def test_fenced_code_is_never_split(self):
        code = "\n".join(f"    line_{i} = {i}" for i in range(200))
        text = (
            "# Guide\n\n" + "prose paragraph.\n\n" * 40
            + "```python\n" + code + "\n```\n\nafter\n"
        )
        chunks = be.chunk_markdown(text)
        code_chunks = [chunk for chunk in chunks if "line_0" in chunk.text]
        self.assertEqual(1, len(code_chunks))
        self.assertIn("line_199", code_chunks[0].text)
        self.assertGreaterEqual(code_chunks[0].text.count("```"), 2)
        self.assertTrue(code_chunks[0].oversized)

    def test_default_target_and_hard_limit_match_nashsu(self):
        chunks = be.chunk_markdown("x" * 3000, overlap_chars=0)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(all(len(chunk.text) <= 1500 for chunk in chunks))


class TestBuildChunks(unittest.TestCase):
    def setUp(self):
        self._max = getattr(be, "MAX_CHARS", None)
        be.MAX_CHARS = 1500

    def tearDown(self):
        if self._max is None:
            del be.MAX_CHARS
        else:
            be.MAX_CHARS = self._max

    PAGE = {
        "page_id": "concepts/range-resolution",
        "path": "concepts/range-resolution.md",
        "title": "Range Resolution",
        "heading": "Range Resolution",
        "body": "# Range Resolution\n\n## Theory\n\nBandwidth sets it.\n",
    }

    def test_index_stores_raw_text_but_hashes_the_enriched_text(self):
        chunks = be.build_chunks([self.PAGE])
        chunk = next(c for c in chunks if "Bandwidth sets it." in c["chunk_text"])
        # The index keeps the raw chunk (NashSU stores chunk.text, not the
        # enriched string); only the embedded/hashed text carries identity.
        self.assertFalse(chunk["chunk_text"].startswith("Range Resolution\n\n"))
        self.assertTrue(chunk["embed_text"].startswith("Range Resolution\n\n"))
        self.assertIn("Bandwidth sets it.", chunk["chunk_text"])

        import hashlib
        self.assertEqual(
            hashlib.sha256(chunk["embed_text"].encode()).hexdigest()[:16],
            chunk["text_sha16"],
            "cache key must follow the text actually embedded",
        )

    def test_later_chunks_carry_their_own_section_breadcrumb(self):
        body = ("# Range Resolution\n\n## Theory\n\n"
                + "theory prose.\n\n" * 90
                + "## Practice\n\n" + "practice prose.\n\n" * 90)
        chunks = be.build_chunks([dict(self.PAGE, body=body)])
        self.assertGreater(len(chunks), 1)
        paths = {c["heading_path"] for c in chunks}
        self.assertIn("# Range Resolution > ## Theory", paths)
        self.assertIn("# Range Resolution > ## Practice", paths)
        # Every chunk's own breadcrumb leads its embedded text.
        for c in chunks:
            self.assertIn(c["heading_path"], c["embed_text"])

    def test_same_body_under_a_different_title_gets_a_new_cache_key(self):
        other = dict(self.PAGE, title="Range Cell", page_id="concepts/range-cell")
        a, b = be.build_chunks([self.PAGE])[0], be.build_chunks([other])[0]
        self.assertEqual(a["chunk_text"], b["chunk_text"])
        self.assertNotEqual(a["text_sha16"], b["text_sha16"])


if __name__ == "__main__":
    unittest.main()
