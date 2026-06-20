"""Tests for _lint_suggest — ported from NashSU lint.test.ts (structural half).

The TS suite mocks the Tauri FS layer; this port passes pages in memory as
(short_name, content) tuples to run_structural_lint. Stdlib unittest only.

Run:  python3 scripts/tests/test_lint_suggest.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _lint_suggest as ls  # noqa: E402


def finding(results, **filters):
    """Find the first result matching all given key=value filters."""
    for r in results:
        if all(r.get(k) == v for k, v in filters.items()):
            return r
    return None


class TestStringSimilarity(unittest.TestCase):
    def test_identical(self):
        self.assertEqual(ls.string_similarity("transformer", "transformer"), 1.0)

    def test_basename_match(self):
        self.assertAlmostEqual(ls.string_similarity("concepts/transformer", "entities/transformer"),
                               ls.SAME_BASENAME_SCORE)

    def test_short_base_returns_zero(self):
        self.assertEqual(ls.string_similarity("cat", "bat"), 0.0)

    def test_levenshtein_close_typo(self):
        # "transfomer" vs "transformer" — 1 char off over 11 → ~0.91 ≥ 0.74
        self.assertGreaterEqual(ls.string_similarity("transfomer", "transformer"), 0.74)


class TestTokenizeForSuggestion(unittest.TestCase):
    def test_cjk_chars_emitted_individually(self):
        toks = ls.tokenize_for_suggestion("注意力 transformer")
        self.assertIn("transformer", toks)
        for ch in "注意力":
            self.assertIn(ch, toks)

    def test_short_latin_tokens_dropped(self):
        toks = ls.tokenize_for_suggestion("a big cat")
        self.assertNotIn("a", toks)
        self.assertIn("big", toks)


class TestRunStructuralLint(unittest.TestCase):
    def test_suggests_closest_page_for_broken_wikilink(self):
        pages = [
            ("transformer.md", "---\ntitle: Transformer\n---\n# Transformer\nAttention model."),
            ("attention.md", "# Attention\nSee [[transfomer]] for the architecture."),
        ]
        results = ls.run_structural_lint(pages)
        broken = finding(results, type="broken-link")
        self.assertIsNotNone(broken)
        self.assertEqual(broken["page"], "attention.md")
        self.assertEqual(broken["broken_target"], "transfomer")
        self.assertEqual(broken["suggested_target"], "transformer.md")

    def test_with_suggestions_false_skips_slow_suggestion_engine(self):
        # validate_ingest.py runs over the whole wiki; the O(n^2) suggestion
        # engine (suggest_related_page / suggest_broken_target) is too slow on
        # large wikis. with_suggestions=False must still DETECT broken-link /
        # orphan / no-outlinks but leave suggested_* = None (no suggestion scan).
        pages = [
            ("transformer.md", "---\ntitle: Transformer\n---\n# Transformer\nAttention model."),
            ("attention.md", "# Attention\nSee [[transfomer]] for the architecture."),
        ]
        results = ls.run_structural_lint(pages, with_suggestions=False)
        broken = finding(results, type="broken-link")
        self.assertIsNotNone(broken, "detection must still run with suggestions off")
        self.assertEqual(broken["broken_target"], "transfomer")
        self.assertIsNone(broken.get("suggested_target"),
                          "suggested_target must be skipped with with_suggestions=False")

    def test_suggests_related_for_orphan_and_no_outlinks(self):
        pages = [
            ("rag.md", "# RAG\nRetrieval augmented generation uses vector search."),
            ("vector-search.md", "# Vector Search\nVector search retrieval finds related chunks."),
        ]
        results = ls.run_structural_lint(pages)
        no_outlinks = finding(results, type="no-outlinks", page="rag.md")
        orphan = finding(results, type="orphan", page="rag.md")
        self.assertEqual(no_outlinks["suggested_target"], "vector-search.md")
        self.assertEqual(orphan["suggested_source"], "vector-search.md")

    def test_no_self_referential_suggestions_when_unrelated(self):
        pages = [
            ("alpha.md", "# Alpha\nAardvark apricot."),
            ("beta.md", "# Beta\nZeppelin zircon."),
        ]
        results = ls.run_structural_lint(pages)
        orphan = finding(results, type="orphan", page="alpha.md")
        self.assertIsNone(orphan["suggested_source"])

    def test_no_same_folder_suggestion_without_shared_terms(self):
        pages = [
            ("concepts/alpha.md", "# Alpha\nAardvark apricot."),
            ("concepts/beta.md", "# Beta\nZeppelin zircon."),
        ]
        results = ls.run_structural_lint(pages)
        orphan = finding(results, type="orphan", page="concepts/alpha.md")
        self.assertIsNone(orphan["suggested_source"])

    def test_no_short_unrelated_typo_suggestion(self):
        pages = [
            ("bat.md", "# Bat\nFlying mammal."),
            ("note.md", "# Note\nSee [[cat]]."),
        ]
        results = ls.run_structural_lint(pages)
        broken = finding(results, type="broken-link", broken_target="cat")
        self.assertIsNotNone(broken)
        self.assertIsNone(broken["suggested_target"])

    def test_skips_index_and_log(self):
        pages = [
            ("index.md", "See [[nothing-here]]."),
            ("log.md", "See [[also-nothing]]."),
            ("real.md", "# Real\nNo links out."),
        ]
        results = ls.run_structural_lint(pages)
        for r in results:
            self.assertNotIn(r["page"], ("index.md", "log.md"))

    def test_resolves_wikilink_case_insensitively(self):
        pages = [
            ("transformer.md", "# Transformer\nBody."),
            ("attention.md", "# Attention\nSee [[Transformer]]."),
        ]
        results = ls.run_structural_lint(pages)
        self.assertIsNone(finding(results, type="broken-link"))
        self.assertIsNone(finding(results, type="orphan", page="transformer.md"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
