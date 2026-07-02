"""Tests for Stage 2.7 cross-source query-resolution sub-step (_stage_2_8).

Covers the embedding-prefilter swap (title-Jaccard → cosine), the A3 revive
(top-k candidates reach the judge even below RESOLVE_COSINE_THRESHOLD;
threshold only marks resolve conclusions; cross_refs written back into kept
query frontmatter), the empty-wiki short-circuit (no embed, no raise), the
no-fallback raise on embed failure, and the unchanged LLM-judge
default-to-kept + closed-query drop. Embeddings/embed calls are injected or
spied (no network).

Run:  python3 scripts/tests/test_stage_2_8_query_resolve.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _dedup_embedding as emb  # noqa: E402
import _stage_2_8_query_resolve as q  # noqa: E402


def _write_page(wiki_root, sub, stem, title, body="body"):
    d = wiki_root / sub
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}.md").write_text(f"---\ntitle: {title}\n---\n{body}", encoding="utf-8")


def _query_block(slug, title, body="body"):
    return (f"queries/{slug}.md", f"---\ntitle: {title}\n---\n{body}")


class TestFindRelatedViaEmbedding(unittest.TestCase):
    def test_top_k_returned_even_below_threshold(self):
        # A3: no threshold gate — both pages come back, ranked by cosine,
        # each as a (page_id, title, similarity) triple.
        existing = [
            {"id": "concepts/fourier", "stem": "fourier", "title": "Fourier Transform",
             "tags": [], "body": "x"},
            {"id": "concepts/newton", "stem": "newton", "title": "Newton's Laws",
             "tags": [], "body": "y"},
        ]
        query = {"slug": "q1", "title": "什么是傅里叶变换", "body": "..."}
        vectors = {
            "concepts/fourier": [1.0, 0.0],
            "concepts/newton": [0.0, 1.0],
            "__query__q1": [0.98, 0.02],
        }
        related = q._stage_2_8_find_related_wiki_pages(query, existing, vectors)
        self.assertEqual([pid for pid, _t, _s in related],
                         ["concepts/fourier", "concepts/newton"])
        self.assertGreater(related[0][2], q.RESOLVE_COSINE_THRESHOLD)
        self.assertLess(related[1][2], q.RESOLVE_COSINE_THRESHOLD)

    def test_top_k_caps_candidates(self):
        existing = [
            {"id": f"concepts/c{i}", "stem": f"c{i}", "title": f"C{i}",
             "tags": [], "body": "x"}
            for i in range(12)
        ]
        vectors = {f"concepts/c{i}": [1.0, i / 100.0] for i in range(12)}
        vectors["__query__q1"] = [1.0, 0.0]
        related = q._stage_2_8_find_related_wiki_pages(
            {"slug": "q1", "title": "T", "body": "b"}, existing, vectors, top_k=8)
        self.assertEqual(len(related), 8)

    def test_missing_query_vector_returns_empty(self):
        existing = [{"id": "concepts/fourier", "stem": "fourier",
                     "title": "Fourier Transform", "tags": [], "body": "x"}]
        query = {"slug": "q1", "title": "T", "body": "b"}
        self.assertEqual(
            q._stage_2_8_find_related_wiki_pages(query, existing, {"concepts/fourier": [1.0, 0.0]}),
            [])


class TestResolveQueriesFlow(unittest.TestCase):
    def test_empty_wiki_returns_kept_without_embedding(self):
        called = {"embed": False}
        orig = q.embed_pages
        q.embed_pages = lambda pages: (called.__setitem__("embed", True) or {})
        try:
            with tempfile.TemporaryDirectory() as tmp:
                wiki = Path(tmp)  # no concepts/ or entities/ dirs
                res = q.stage_2_8_resolve_queries(
                    [_query_block("q1", "Anything")], wiki, object())
        finally:
            q.embed_pages = orig
        self.assertEqual(res["q1"]["status"], "kept")
        self.assertFalse(called["embed"], "must not embed when there is nothing to resolve against")

    def test_no_queries_returns_empty_without_embedding(self):
        called = {"embed": False}
        orig = q.embed_pages
        q.embed_pages = lambda pages: (called.__setitem__("embed", True) or {})
        try:
            with tempfile.TemporaryDirectory() as tmp:
                wiki = Path(tmp)
                _write_page(wiki, "concepts", "fourier", "Fourier Transform")
                res = q.stage_2_8_resolve_queries(
                    [("concepts/x.md", "---\ntitle: X\n---\nbody")], wiki, object())
        finally:
            q.embed_pages = orig
        self.assertEqual(res, {})
        self.assertFalse(called["embed"])

    def test_no_fallback_raises_on_embed_failure(self):
        orig = q.embed_pages
        # Non-empty wiki + a query, but embedding returns all-None → must raise.
        q.embed_pages = lambda pages: {p["id"]: None for p in pages}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                wiki = Path(tmp)
                _write_page(wiki, "concepts", "fourier", "Fourier Transform")
                with self.assertRaises(emb.DuplicatePrefilterError):
                    q.stage_2_8_resolve_queries(
                        [_query_block("q1", "什么是傅里叶变换")], wiki, object())
        finally:
            q.embed_pages = orig

    def test_below_threshold_candidates_still_reach_judge(self):
        # A3 revive: production cosines 0.75-0.79 never cleared the old 0.82
        # gate, so the judge historically never fired. Below-threshold top-k
        # candidates must now be judged (mocked embeddings, spied LLM).
        judge_calls = []

        def _spy(prompt, config, max_tokens=None, label=None):
            judge_calls.append(prompt)
            return "STATUS: kept | REASON: only partially answered", "end_turn"

        embeddings = {
            "concepts/fourier": [0.5, 0.8660254],  # cosine 0.5 vs query — below 0.70
            "__query__q1": [1.0, 0.0],
        }
        orig = q.call_anthropic_protocol
        q.call_anthropic_protocol = _spy
        try:
            with tempfile.TemporaryDirectory() as tmp:
                wiki = Path(tmp)
                _write_page(wiki, "concepts", "fourier", "Fourier Transform")
                res = q.stage_2_8_resolve_queries(
                    [_query_block("q1", "什么是傅里叶变换")], wiki, object(),
                    embeddings=embeddings)
        finally:
            q.call_anthropic_protocol = orig
        self.assertEqual(len(judge_calls), 1, "judge must fire even below threshold")
        self.assertIn("[[concepts/fourier]]", judge_calls[0])
        self.assertEqual(res["q1"]["status"], "kept")
        # Below threshold → not recorded as a resolve conclusion.
        self.assertEqual(res["q1"]["resolution_pages"], [])

    def test_resolution_pages_filtered_by_threshold(self):
        embeddings = {
            "concepts/high": [1.0, 0.0],           # cosine 1.0 — above 0.70
            "concepts/low": [0.5, 0.8660254],       # cosine 0.5 — below 0.70
            "__query__q1": [1.0, 0.0],
        }
        orig = q.call_anthropic_protocol
        q.call_anthropic_protocol = lambda *a, **k: ("STATUS: kept | REASON: r", "end_turn")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                wiki = Path(tmp)
                _write_page(wiki, "concepts", "high", "High Match")
                _write_page(wiki, "concepts", "low", "Low Match")
                res = q.stage_2_8_resolve_queries(
                    [_query_block("q1", "High Match?")], wiki, object(),
                    embeddings=embeddings)
        finally:
            q.call_anthropic_protocol = orig
        self.assertEqual(res["q1"]["resolution_pages"], ["concepts/high"])


class TestJudgeUnchangedBehavior(unittest.TestCase):
    def test_judge_defaults_to_kept_on_llm_failure(self):
        def _boom(*a, **k):
            raise RuntimeError("llm down")
        orig = q.call_anthropic_protocol
        q.call_anthropic_protocol = _boom
        try:
            status, reason = q._stage_2_8_judge_query_resolution(
                {"slug": "q1", "title": "T", "body": "b"},
                [("concepts/fourier", "Fourier Transform", 0.75)], object())
        finally:
            q.call_anthropic_protocol = orig
        self.assertEqual(status, "kept")

    def test_no_related_short_circuits_to_kept(self):
        status, _ = q._stage_2_8_judge_query_resolution(
            {"slug": "q1", "title": "T", "body": "b"}, [], object())
        self.assertEqual(status, "kept")

    def test_closed_query_block_dropped(self):
        file_blocks = [
            ("queries/q1.md", "---\ntitle: Q1\n---\nopen"),
            ("queries/q2.md", "---\ntitle: Q2\n---\nopen"),
            ("concepts/c.md", "---\ntitle: C\n---\nbody"),
        ]
        resolutions = {"q1": {"status": "closed"}, "q2": {"status": "kept"}}
        result = q._stage_2_8_update_file_blocks_after_resolution(file_blocks, resolutions)
        paths = [p for p, _ in result]
        self.assertEqual(paths, ["queries/q2.md", "concepts/c.md"])


class TestCrossRefsWriteBack(unittest.TestCase):
    def test_kept_query_gets_cross_refs_frontmatter(self):
        file_blocks = [
            ("queries/q1.md", '---\ntitle: "Q1?"\nrelated: []\n---\nbody'),
            ("concepts/c.md", "---\ntitle: C\n---\nbody"),
        ]
        resolutions = {"q1": {"status": "kept",
                              "resolution_pages": ["concepts/high", "entities/e1"],
                              "reason": "r"}}
        result = q._stage_2_8_apply_cross_refs(file_blocks, resolutions)
        q1 = dict(result)["queries/q1.md"]
        self.assertIn('cross_refs: ["concepts/high", "entities/e1"]', q1)
        # Non-query block untouched.
        self.assertEqual(dict(result)["concepts/c.md"], "---\ntitle: C\n---\nbody")
        # Input not mutated (new list, new tuples).
        self.assertNotIn("cross_refs", file_blocks[0][1])

    def test_no_resolution_pages_leaves_query_untouched(self):
        block = ("queries/q1.md", '---\ntitle: "Q1?"\n---\nbody')
        result = q._stage_2_8_apply_cross_refs(
            [block], {"q1": {"status": "kept", "resolution_pages": [], "reason": "r"}})
        self.assertEqual(result, [block])

    def test_unresolved_slug_left_untouched(self):
        block = ("queries/q9.md", '---\ntitle: "Q9?"\n---\nbody')
        result = q._stage_2_8_apply_cross_refs([block], {})
        self.assertEqual(result, [block])


if __name__ == "__main__":
    unittest.main()
