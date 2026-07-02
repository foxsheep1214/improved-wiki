"""Tests for Stage 2.4 in-source dedup sub-step (_stage_2_5_dedup).

Covers the embedding-prefilter swap (Jaccard → cosine), the no-fallback raise
when embeddings are unavailable, and the unchanged LLM-confirm gate + wikilink
rewrite. Embeddings are injected (no network), mirroring test_dedup_embedding.

Run:  python3 scripts/tests/test_stage_2_5_dedup.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _dedup_embedding as emb  # noqa: E402
import _stage_2_5_dedup as d  # noqa: E402


def _concept(slug, title, defn="some definition"):
    return {"slug": slug, "title": title, "definition_snippet": defn,
            "block_index": 0, "full_content": ""}


class TestFindDuplicateConceptsEmbedding(unittest.TestCase):
    def test_near_identical_vectors_form_one_group(self):
        # Arrange: a cross-language synonym pair the old word-Jaccard would miss.
        concepts = [
            _concept("pao", "PAO", "phosphate accumulating organisms"),
            _concept("julinjun", "聚磷菌", "聚磷菌"),
            _concept("other", "Other", "unrelated thing"),
        ]
        vectors = {"pao": [1.0, 0.0], "julinjun": [0.99, 0.01], "other": [0.0, 1.0]}

        # Act
        groups = d._stage_2_5_find_duplicate_concepts(concepts, embeddings=vectors)

        # Assert: pao+julinjun cluster together by index; other excluded.
        self.assertEqual([sorted(g) for g in groups], [[0, 1]])

    def test_orthogonal_vectors_yield_no_groups(self):
        concepts = [_concept("a", "A"), _concept("b", "B"), _concept("c", "C")]
        vectors = {"a": [1.0, 0.0, 0.0], "b": [0.0, 1.0, 0.0], "c": [0.0, 0.0, 1.0]}
        self.assertEqual(d._stage_2_5_find_duplicate_concepts(concepts, embeddings=vectors), [])

    def test_single_concept_returns_no_groups(self):
        self.assertEqual(d._stage_2_5_find_duplicate_concepts([_concept("a", "A")]), [])

    def test_no_fallback_raises_when_embeddings_unavailable(self):
        # Most pages fail to embed (None) → DuplicatePrefilterError, NOT a silent
        # empty result and NOT a Jaccard fallback.
        concepts = [_concept("a", "A"), _concept("b", "B"), _concept("c", "C")]
        broken = {"a": [1.0, 0.0], "b": None, "c": None}
        with self.assertRaises(emb.DuplicatePrefilterError):
            d._stage_2_5_find_duplicate_concepts(concepts, embeddings=broken)


class TestLlmConfirmGate(unittest.TestCase):
    def test_llm_no_blocks_merge(self):
        concepts = [_concept("pao", "PAO"), _concept("julinjun", "聚磷菌")]
        orig = d.call_anthropic_protocol
        d.call_anthropic_protocol = lambda *a, **k: ("GROUP 1: MERGE no", None)
        try:
            rules = d._stage_2_5_generate_merge_rules(concepts, [[0, 1]], config=object())
        finally:
            d.call_anthropic_protocol = orig
        self.assertEqual(rules, [])

    def test_llm_yes_produces_merge_rule(self):
        concepts = [_concept("pao", "PAO", "longer definition wins primary"),
                    _concept("julinjun", "聚磷菌", "x")]
        orig = d.call_anthropic_protocol
        d.call_anthropic_protocol = lambda *a, **k: ("GROUP 1: MERGE yes | PRIMARY: pao", None)
        try:
            rules = d._stage_2_5_generate_merge_rules(concepts, [[0, 1]], config=object())
        finally:
            d.call_anthropic_protocol = orig
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["primary_slug"], "pao")
        self.assertEqual(rules[0]["duplicate_slugs"], ["julinjun"])

    def test_all_groups_confirmed_in_one_llm_call(self):
        """Perf (Finding C): N candidate groups → ONE batched confirm call, not
        N. Each group still gets its own verdict, conservative default holds."""
        concepts = [
            _concept("pao", "PAO", "phosphate accumulating organisms"),
            _concept("julinjun", "聚磷菌", "聚磷菌"),
            _concept("ft", "Fourier transform", "integral transform"),
            _concept("flbyhh", "傅里叶变换", "傅里叶变换"),
        ]
        calls = {"n": 0}

        def _mock(*a, **k):
            calls["n"] += 1
            # group 1 (pao/julinjun) merges; group 2 (ft/flbyhh) does not
            return ("GROUP 1: MERGE yes | PRIMARY: pao\nGROUP 2: MERGE no", None)

        orig = d.call_anthropic_protocol
        d.call_anthropic_protocol = _mock
        try:
            rules = d._stage_2_5_generate_merge_rules(
                concepts, [[0, 1], [2, 3]], config=object())
        finally:
            d.call_anthropic_protocol = orig
        self.assertEqual(calls["n"], 1)            # ← single batched call
        self.assertEqual(len(rules), 1)            # only group 1 confirmed
        self.assertEqual(rules[0]["primary_slug"], "pao")
        self.assertEqual(rules[0]["duplicate_slugs"], ["julinjun"])

    def test_llm_failure_keeps_all_candidates(self):
        concepts = [_concept("pao", "PAO"), _concept("julinjun", "聚磷菌")]

        def _boom(*a, **k):
            raise RuntimeError("handoff died")

        orig = d.call_anthropic_protocol
        d.call_anthropic_protocol = _boom
        try:
            rules = d._stage_2_5_generate_merge_rules(concepts, [[0, 1]], config=object())
        finally:
            d.call_anthropic_protocol = orig
        self.assertEqual(rules, [])


class TestApplyMergeRewritesWikilinks(unittest.TestCase):
    def test_drops_duplicate_block_and_redirects_links(self):
        # Sibling links are redirected to the primary; the primary's own link
        # to its merged-away duplicate is de-linked (not a self-link) — the
        # self-link case is covered in test_dedup_link_rewrite.py.
        file_blocks = [
            ("concepts/pao.md", "---\ntitle: PAO\n---\nprimary body"),
            ("concepts/julinjun.md", "---\ntitle: 聚磷菌\n---\nduplicate body"),
            ("concepts/sibling.md", "---\ntitle: Sibling\n---\nSee [[julinjun]] for detail."),
        ]
        rules = [{
            "primary_slug": "pao", "primary_title": "PAO",
            "duplicate_slugs": ["julinjun"], "merge_strategy": "union",
            "merge_reason": "test",
        }]
        result = d._stage_2_5_apply_merge_rules(file_blocks, rules)
        paths = [p for p, _ in result]
        self.assertEqual(paths, ["concepts/pao.md", "concepts/sibling.md"])
        sibling = dict(result)["concepts/sibling.md"]
        self.assertIn("[[pao]]", sibling)
        self.assertNotIn("[[julinjun]]", sibling)


if __name__ == "__main__":
    unittest.main()
