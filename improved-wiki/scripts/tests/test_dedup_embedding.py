"""Tests for _dedup_embedding — ported from NashSU dedup_embedding.ts.

Covers the pure functions: cosine_similarity, page_to_embedding_text,
candidate_pairs (with injected embeddings, no network), cluster_by_pairs,
and the DuplicatePrefilterError fallback. Stdlib unittest only.

Run:  python3 scripts/tests/test_dedup_embedding.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _dedup_embedding as e  # noqa: E402


class TestCosineSimilarity(unittest.TestCase):
    def test_identical_vectors(self):
        self.assertAlmostEqual(e.cosine_similarity([1, 0, 0], [1, 0, 0]), 1.0)

    def test_orthogonal(self):
        self.assertAlmostEqual(e.cosine_similarity([1, 0], [0, 1]), 0.0)

    def test_opposite(self):
        self.assertAlmostEqual(e.cosine_similarity([1, 1], [-1, -1]), -1.0)

    def test_none_or_mismatched_length(self):
        self.assertEqual(e.cosine_similarity(None, [1, 2]), 0.0)
        self.assertEqual(e.cosine_similarity([1, 2], [1, 2, 3]), 0.0)
        self.assertEqual(e.cosine_similarity([], []), 0.0)


class TestPageToEmbeddingText(unittest.TestCase):
    def test_assembles_slug_title_tags_body(self):
        text = e.page_to_embedding_text(
            {"id": "wiki/entities/foo.md", "title": "Foo", "tags": ["a", "b"],
             "body": "body text"})
        self.assertIn("foo", text)
        self.assertIn("Foo", text)
        self.assertIn("a b", text)
        self.assertIn("body text", text)

    def test_truncates_body_to_budget(self):
        body = "x" * 5000
        text = e.page_to_embedding_text({"id": "p.md", "title": "T", "body": body}, budget=100)
        self.assertLessEqual(len(text), 200)


class TestCandidatePairs(unittest.TestCase):
    def _pages(self):
        return [
            {"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"},
        ]

    def test_pairs_above_threshold(self):
        emb = {
            "a": [1.0, 0.0], "b": [0.99, 0.01],
            "c": [0.0, 1.0], "d": [0.01, 0.99],
        }
        pairs = e.candidate_pairs(self._pages(), threshold=0.82, embeddings=emb)
        pair_ids = {frozenset(p) for p in pairs}
        self.assertIn(frozenset(("a", "b")), pair_ids)
        self.assertIn(frozenset(("c", "d")), pair_ids)
        self.assertNotIn(frozenset(("a", "c")), pair_ids)

    def test_symmetric_dedup(self):
        emb = {"a": [1.0, 0.0], "b": [1.0, 0.0], "c": [0.0, 1.0]}
        pairs = e.candidate_pairs(self._pages()[:3], threshold=0.82, embeddings=emb)
        self.assertEqual(len([p for p in pairs if set(p) == {"a", "b"}]), 1)

    def test_respects_top_k(self):
        emb = {"a": [1.0, 0.0], "b": [0.9, 0.1], "c": [0.9, 0.1], "d": [0.9, 0.1]}
        pairs = e.candidate_pairs(self._pages(), top_k=1, threshold=0.5, embeddings=emb)
        a_pairs = [p for p in pairs if "a" in p]
        self.assertLessEqual(len(a_pairs), 1)

    def test_raises_below_min_success_ratio(self):
        emb = {"a": [1.0, 0.0], "b": [0.9, 0.1], "c": None, "d": None}
        with self.assertRaises(e.DuplicatePrefilterError):
            e.candidate_pairs(self._pages(), embeddings=emb, min_success_ratio=0.8)

    def test_empty_pages(self):
        self.assertEqual(e.candidate_pairs([]), [])

    def test_matches_cosine_similarity_at_moderate_scale(self):
        """2026-07-10: candidate_pairs' inner loop was rewritten to normalize
        each vector once and use a fast dot product instead of calling
        cosine_similarity() (which recomputes both vectors' norms) on every
        pairwise comparison — confirmed live as the dominant cost of an
        O(N^2) sweep (~40 CPU-minutes on a ~7500-page wiki). This is a
        correctness regression guard at a scale (200 pages) big enough that
        a normalization or dot-product mistake would show up as wrong
        membership, not just a rounding blip: every result must still equal
        what the original per-pair cosine_similarity() call would produce.
        """
        import random
        random.seed(42)
        n = 200
        pages = [{"id": f"p{i}"} for i in range(n)]
        emb = {f"p{i}": [random.random() for _ in range(16)] for i in range(n)}
        # Force a handful of near-duplicate pairs above threshold so the
        # test isn't just checking an all-empty result.
        emb["p1"] = list(emb["p0"])
        emb["p1"][0] += 1e-6
        emb["p50"] = list(emb["p49"])
        emb["p50"][0] += 1e-6

        threshold = 0.9
        pairs = e.candidate_pairs(pages, threshold=threshold, top_k=n, embeddings=emb)
        got = {frozenset(p) for p in pairs}

        expected = set()
        for i in range(n):
            for j in range(i + 1, n):
                sim = e.cosine_similarity(emb[f"p{i}"], emb[f"p{j}"])
                if sim >= threshold:
                    expected.add(frozenset((f"p{i}", f"p{j}")))

        self.assertEqual(got, expected)
        self.assertIn(frozenset(("p0", "p1")), got)
        self.assertIn(frozenset(("p49", "p50")), got)


class TestClusterByPairs(unittest.TestCase):
    def test_clusters_pairs_into_groups(self):
        ids = ["a", "b", "c", "d", "e"]
        pairs = [("a", "b"), ("b", "c"), ("d", "e")]
        groups = e.cluster_by_pairs(ids, pairs)
        grouped = {frozenset(g) for g in groups}
        self.assertIn(frozenset({"a", "b", "c"}), grouped)
        self.assertIn(frozenset({"d", "e"}), grouped)

    def test_no_pairs_no_groups(self):
        self.assertEqual(e.cluster_by_pairs(["a", "b"], []), [])

    def test_handles_unknown_pair_ids_gracefully(self):
        groups = e.cluster_by_pairs(["a", "b"], [("a", "b"), ("a", "z")])
        self.assertEqual([frozenset(g) for g in groups], [frozenset({"a", "b"})])


if __name__ == "__main__":
    unittest.main()
