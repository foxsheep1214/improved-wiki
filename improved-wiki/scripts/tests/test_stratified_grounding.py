"""Tests for the A6 big-book grounding de-bias (audit H2, 2026-07-02).

Covers the stratified per-chapter source sampler used for Stage 2.7/2.9
grounding (_ingest_prepare), the per-chunk uniform claims quota (Stage 2.7),
the chapter-scaled comparison cap and the max_tokens truncation retry
(Stage 2.9). No network — the LLM call is spied.

Run:  python3 scripts/tests/test_stratified_grounding.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _ingest_prepare as prep  # noqa: E402
import _stage_2_7_query_generation as q27  # noqa: E402
import _stage_2_9_comparison as c29  # noqa: E402


def _book(n_chapters: int, body_len: int = 2000) -> str:
    """Synthetic multi-chapter text with a unique marker at each chapter head."""
    parts = ["前言部分 FRONTMATTER-MARK\n" + "f" * 200]
    for i in range(1, n_chapters + 1):
        parts.append(f"# 第{i}章 主题{i}\nCH{i}-MARK\n" + ("x" * body_len))
    return "\n".join(parts)


class TestSplitSourceChapters(unittest.TestCase):
    def test_splits_at_chapter_anchors_keeping_front_matter(self):
        text = _book(3)
        chapters = prep._split_source_chapters(text)
        self.assertEqual(len(chapters), 4)  # front matter + 3 chapters
        self.assertIn("FRONTMATTER-MARK", chapters[0])
        for i in (1, 2, 3):
            self.assertIn(f"CH{i}-MARK", chapters[i])

    def test_english_chapter_anchor(self):
        text = "intro\n# Chapter 1 Basics\naaa\n## Chapter 2 Advanced\nbbb"
        self.assertEqual(len(prep._split_source_chapters(text)), 3)

    def test_no_anchor_returns_whole_text(self):
        self.assertEqual(prep._split_source_chapters("plain text"), ["plain text"])

    def test_empty_text(self):
        self.assertEqual(prep._split_source_chapters(""), [])


class TestStratifiedSourceSample(unittest.TestCase):
    def test_all_chapters_represented_within_budget(self):
        text = _book(10, body_len=5000)
        budget = 4000  # far below len(text) — old prefix would cover ~ch1 only
        sample = prep._stratified_source_sample(text, budget)
        self.assertLessEqual(len(sample), budget)
        for i in range(1, 11):
            self.assertIn(f"CH{i}-MARK", sample,
                          f"chapter {i} head missing from stratified sample")

    def test_within_budget_passes_through_whole(self):
        text = _book(3, body_len=100)
        self.assertEqual(prep._stratified_source_sample(text, len(text) + 1), text)

    def test_no_anchor_falls_back_to_prefix(self):
        text = "z" * 1000
        self.assertEqual(prep._stratified_source_sample(text, 100), "z" * 100)

    def test_tiny_budget_falls_back_to_prefix(self):
        # per-chapter share <= 0 → prefix fallback, never an empty sample.
        text = _book(50)
        sample = prep._stratified_source_sample(text, 30)
        self.assertEqual(sample, text[:30])


class TestClaimsPerChunkQuota(unittest.TestCase):
    def test_round_robin_covers_all_chunks(self):
        chunk_analyses = [
            {"claims": [f"c1-{i}" for i in range(40)]},  # old [:30] took ONLY these
            {"claims": [f"c2-{i}" for i in range(40)]},
            {"claims": ["c3-0"]},
        ]
        sampled = q27._stage_2_7_sample_claims(chunk_analyses, limit=30)
        self.assertEqual(len(sampled), 30)
        self.assertIn("c1-0", sampled)
        self.assertIn("c2-0", sampled)
        self.assertIn("c3-0", sampled)
        # Round-robin: first three picks are each chunk's head claim.
        self.assertEqual(sampled[:3], ["c1-0", "c2-0", "c3-0"])

    def test_fewer_claims_than_limit_returns_all(self):
        chunk_analyses = [{"claims": ["a", "b"]}, {"claims": ["c"]}]
        self.assertEqual(sorted(q27._stage_2_7_sample_claims(chunk_analyses, limit=30)),
                         ["a", "b", "c"])

    def test_tolerates_malformed_chunks(self):
        chunk_analyses = [{"claims": "not-a-list"}, "not-a-dict", {}, {"claims": ["ok"]}]
        self.assertEqual(q27._stage_2_7_sample_claims(chunk_analyses), ["ok"])


class TestComparisonCap(unittest.TestCase):
    def test_cap_scales_with_chapter_count(self):
        self.assertEqual(c29._stage_2_9_comparison_cap(0), 3)
        self.assertEqual(c29._stage_2_9_comparison_cap(7), 3)
        self.assertEqual(c29._stage_2_9_comparison_cap(8), 4)
        self.assertEqual(c29._stage_2_9_comparison_cap(26), 6)
        self.assertEqual(c29._stage_2_9_comparison_cap(40), 8)
        self.assertEqual(c29._stage_2_9_comparison_cap(200), 8)  # hard ceiling

    def test_cap_lands_in_prompt(self):
        # wiki_dir: required since B4 (existing-comparisons injection);
        # nonexistent dir → helper yields no section.
        config = SimpleNamespace(raw_root=Path("/tmp/raw"),
                                 wiki_dir=Path("/tmp/nonexistent-wiki-b4"))
        prompt = c29._stage_2_9_build_prompt_in_source(
            ["a", "b"], Path("/tmp/raw/book.pdf"), config, comp_cap=6)
        self.assertIn("Generate at most 6 comparison pages.", prompt)


class TestTruncationRetry(unittest.TestCase):
    _RESP = ('---FILE:wiki/comparisons/a-vs-b.md---\n'
             '---\ntitle: "A vs B"\n---\nbody\n---END FILE---')

    def _run(self, stops):
        """Run stage 2.9 with a spied LLM returning self._RESP with the given
        stop reasons in order; returns (blocks, number of LLM calls)."""
        calls = []

        def _spy(prompt, config, max_tokens=None, label=None):
            calls.append(prompt)
            return self._RESP, stops[min(len(calls) - 1, len(stops) - 1)]

        config = SimpleNamespace(raw_root=Path("/tmp/raw"),
                                 wiki_dir=Path("/tmp/nonexistent-wiki-b4"),
                                 compute_max_tokens=lambda n: n)
        orig = c29.call_anthropic_protocol
        c29.call_anthropic_protocol = _spy
        try:
            blocks, _ = c29.stage_2_9_comparison_generation(
                {}, [], [("concepts/a.md", "x"), ("concepts/b.md", "y")],
                Path("/tmp/raw/book.pdf"), config)
        finally:
            c29.call_anthropic_protocol = orig
        return blocks, len(calls)

    def test_normal_stop_makes_single_call(self):
        blocks, n_calls = self._run(["end_turn"])
        self.assertEqual(n_calls, 1)
        self.assertEqual(len(blocks), 1)

    def test_max_tokens_stop_retries_once(self):
        blocks, n_calls = self._run(["max_tokens", "end_turn"])
        self.assertEqual(n_calls, 2)
        self.assertEqual(len(blocks), 1)

    def test_still_truncated_keeps_parsed_blocks_no_third_call(self):
        blocks, n_calls = self._run(["max_tokens", "max_tokens"])
        self.assertEqual(n_calls, 2, "retry exactly once")
        self.assertEqual(len(blocks), 1)


if __name__ == "__main__":
    unittest.main()
