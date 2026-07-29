"""Embedding input must carry page identity and never split a table.

Two NashSU parity gaps this pins down:

  * embedding.ts:299-308 enriches the embedded text with the page title and
    the chunk's heading breadcrumb before hashing/embedding, and stores the
    RAW chunk text in the index. improved-wiki embedded the bare chunk, so a
    chunk that never repeats its page title carried no page identity in its
    vector (measured: 33% of HardwareWiki chunks, 46% of RadarWiki).
  * text-chunker.ts:301-336 marks fenced code and markdown tables
    indivisible. The old character-window split cut datasheet parameter
    tables between rows, leaving the trailing half without its header.

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
        self.assertEqual("Range Resolution", at("intro"))
        self.assertEqual("Range Resolution > Theory", at("t-body"))
        self.assertEqual(
            "Range Resolution > Theory > Derivation", at("d-body"))
        # A sibling H2 pops the deeper level rather than accumulating it.
        self.assertEqual("Range Resolution > Practice", at("p-body"))

    def test_no_heading_yields_empty(self):
        self.assertEqual("", be.heading_path_at("just prose\n", 3))


class TestChunkSpans(unittest.TestCase):
    def test_short_page_is_one_span(self):
        text = "# Title\n\nshort body\n"
        self.assertEqual([(0, len(text))], be.chunk_spans(text, max_chars=1500))

    def test_blank_page_yields_nothing(self):
        self.assertEqual([], be.chunk_spans("   \n\n  \n", max_chars=1500))

    def assert_no_span_cuts_a_protected_block(self, text):
        """No span may START or END strictly inside a protected block.

        Asserted against the detected ranges rather than hand-computed
        offsets: those are the same ranges the chunker honours, so the test
        cannot drift from the contract by an off-by-one.
        """
        from _stage_2_analyze import _stage_2_1_find_protected_ranges
        protected = _stage_2_1_find_protected_ranges(text)
        self.assertTrue(protected, "fixture should contain a protected block")

        spans = be.chunk_spans(text, max_chars=1500, overlap=200)
        self.assertGreater(len(spans), 1, "fixture should need several chunks")
        for start, end in spans:
            for lo, hi in protected:
                for pos, which in ((start, "start"), (end, "end")):
                    self.assertFalse(
                        lo < pos < hi,
                        f"span ({start}, {end}) {which} lands inside "
                        f"protected block [{lo}, {hi})")

    def test_table_is_never_split(self):
        table_rows = "\n".join(
            f"| PARAM{i} | {i} mV | typ |" for i in range(120))
        self.assert_no_span_cuts_a_protected_block(
            "# Datasheet\n\n" + "filler paragraph.\n\n" * 40
            + "| Name | Value | Note |\n|---|---|---|\n" + table_rows
            + "\n\ntrailing prose\n")

    def test_fenced_code_is_never_split(self):
        code = "\n".join(f"    line_{i} = {i}" for i in range(200))
        self.assert_no_span_cuts_a_protected_block(
            "# Guide\n\n" + "prose paragraph.\n\n" * 40
            + "```python\n" + code + "\n```\n\nafter\n")

    def test_spans_cover_the_document(self):
        text = "# T\n\n" + "".join(f"para {i}.\n\n" for i in range(400))
        spans = be.chunk_spans(text, max_chars=1500, overlap=200)
        self.assertEqual(0, spans[0][0])
        self.assertEqual(len(text), spans[-1][1])
        for (_s1, e1), (s2, _e2) in zip(spans, spans[1:]):
            self.assertLessEqual(s2, e1, "gap between consecutive spans")


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
        (chunk,) = be.build_chunks([self.PAGE])
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
        self.assertIn("Range Resolution > Theory", paths)
        self.assertIn("Range Resolution > Practice", paths)
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
