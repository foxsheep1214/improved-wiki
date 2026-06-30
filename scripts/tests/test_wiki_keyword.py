"""Tests for _wiki_keyword — ported from NashSU search.rs keyword scoring.

Covers tokenize_query (CJK bigram + stopwords), score_file (filename exact,
title phrase, body phrase, token weights), keyword_search end-to-end, and
rrf_merge. Uses tempfile. Stdlib unittest only.

Run:  python3 scripts/tests/test_wiki_keyword.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _wiki_keyword as k  # noqa: E402


class TestTokenizeQuery(unittest.TestCase):
    def test_english_lowercased_drops_stopwords(self):
        toks = k.tokenize_query("How does the ADL8113 work")
        self.assertIn("adl8113", toks)
        self.assertNotIn("how", toks)
        self.assertNotIn("the", toks)

    def test_cjk_bigram_expansion(self):
        toks = k.tokenize_query("功率变换器")
        self.assertIn("功率", toks)
        self.assertIn("变换", toks)
        self.assertIn("换器", toks)
        self.assertIn("功率变换器", toks)

    def test_short_cjk_token_kept_verbatim(self):
        toks = k.tokenize_query("除磷")
        self.assertIn("除磷", toks)

    def test_punctuation_splits(self):
        toks = k.tokenize_query("ADL8113,INA1H94")
        self.assertIn("adl8113", toks)
        self.assertIn("ina1h94", toks)


class TestScoreFile(unittest.TestCase):
    def test_filename_exact_match_scores_highest(self):
        content = "---\ntitle: Foo\n---\n\n# Foo\nbody\n"
        r = k.score_file("entities/adl8113.md", "adl8113.md", content,
                         k.tokenize_query("adl8113"), "adl8113", "adl8113")
        self.assertIsNotNone(r)
        # filename exact (200) + title phrase (50, file_name is in title_text)
        # + title token (5) — matches NashSU score_file semantics.
        self.assertGreaterEqual(r["score"], k.FILENAME_EXACT_BONUS)

    def test_title_phrase_bonus(self):
        content = "---\ntitle: ADL8113 Datasheet\n---\n\n# ADL8113\nbody\n"
        r = k.score_file("entities/foo.md", "foo.md", content,
                         k.tokenize_query("adl8113"), "adl8113", "adl8113")
        self.assertIsNotNone(r)
        self.assertTrue(r["title_match"])
        self.assertGreaterEqual(r["score"], k.PHRASE_IN_TITLE_BONUS)

    def test_body_phrase_occurrences(self):
        content = "---\ntitle: X\n---\n\n" + ("adl8113 " * 12)
        r = k.score_file("entities/foo.md", "foo.md", content,
                         k.tokenize_query("adl8113"), "adl8113", "adl8113")
        self.assertIsNotNone(r)
        # 10 (capped) phrase occ × 20 + content token weight (1)
        self.assertGreaterEqual(r["score"], k.MAX_PHRASE_OCC_COUNTED * k.PHRASE_IN_CONTENT_PER_OCC)

    def test_no_match_returns_none(self):
        content = "---\ntitle: Unrelated\n---\n\nnothing relevant here\n"
        r = k.score_file("entities/foo.md", "foo.md", content,
                         k.tokenize_query("adl8113"), "adl8113", "adl8113")
        self.assertIsNone(r)


class TestKeywordSearch(unittest.TestCase):
    def _wiki(self, td: str):
        Path(td, "wiki", "entities").mkdir(parents=True)
        Path(td, "wiki", "concepts").mkdir()
        Path(td, "wiki/entities/adl8113.md").write_text(
            "---\ntitle: ADL8113\n---\n\n# ADL8113\nA high-speed amplifier.\n",
            encoding="utf-8")
        Path(td, "wiki/entities/ina1h94.md").write_text(
            "---\ntitle: INA1H94\n---\n\n# INA1H94\nCurrent sense amp.\n",
            encoding="utf-8")
        Path(td, "wiki/concepts/power-converter.md").write_text(
            "---\ntitle: 功率变换器\n---\n\n# 功率变换器\nDC-DC topology.\n",
            encoding="utf-8")

    def test_part_number_ranks_top(self):
        with tempfile.TemporaryDirectory() as td:
            self._wiki(td)
            results = k.keyword_search(Path(td, "wiki"), "ADL8113", max_results=5)
            self.assertTrue(results)
            self.assertEqual(results[0]["path"], "entities/adl8113.md")

    def test_cjk_query_hits(self):
        with tempfile.TemporaryDirectory() as td:
            self._wiki(td)
            results = k.keyword_search(Path(td, "wiki"), "功率变换器", max_results=5)
            paths = [r["path"] for r in results]
            self.assertIn("concepts/power-converter.md", paths)

    def test_no_results_for_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            self._wiki(td)
            self.assertEqual(k.keyword_search(Path(td, "wiki"), "zzzznotfound", 5), [])


class TestRrfMerge(unittest.TestCase):
    def test_both_rank_high_surfaces_top(self):
        kw = [{"path": "a.md", "score": 100, "title": "A", "snippet": "", "title_match": True, "vector_score": None},
              {"path": "b.md", "score": 50, "title": "B", "snippet": "", "title_match": False, "vector_score": None}]
        vec = [{"path": "b.md", "score": 0.9, "title": "B", "snippet": "", "title_match": False, "vector_score": 0.9},
               {"path": "c.md", "score": 0.8, "title": "C", "snippet": "", "title_match": False, "vector_score": 0.8}]
        fused = k.rrf_merge(kw, vec, top=5)
        paths = [r["path"] for r in fused]
        self.assertEqual(paths[0], "b.md")
        self.assertIn("c.md", paths)
        self.assertIn("a.md", paths)

    def test_empty_vector_returns_keyword_order(self):
        kw = [{"path": "a.md", "score": 100, "title": "A", "snippet": "", "title_match": True, "vector_score": None}]
        fused = k.rrf_merge(kw, [], top=5)
        self.assertEqual(len(fused), 1)
        self.assertEqual(fused[0]["path"], "a.md")


class TestExtractTitle(unittest.TestCase):
    def test_frontmatter_title_wins(self):
        self.assertEqual(k.extract_title("---\ntitle: My Title\n---\n\n# Heading\n", "x.md"), "My Title")

    def test_heading_fallback(self):
        self.assertEqual(k.extract_title("---\ntype: entity\n---\n\n# Heading Title\n", "x.md"), "Heading Title")

    def test_filename_fallback(self):
        self.assertEqual(k.extract_title("no frontmatter", "my-page.md"), "my page")


if __name__ == "__main__":
    unittest.main()
