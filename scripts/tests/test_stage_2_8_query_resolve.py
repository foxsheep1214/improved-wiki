"""Tests for Stage 2.7 cross-source query-resolution sub-step (_stage_2_8).

Covers the embedding-prefilter swap (title-Jaccard → cosine), the empty-wiki
short-circuit (no embed, no raise), the no-fallback raise on embed failure, and
the unchanged LLM-judge default-to-kept + closed-query drop. Embeddings/embed
calls are injected or spied (no network).

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
    def test_similar_page_is_related(self):
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
        self.assertIn(("fourier", "Fourier Transform"), related)
        self.assertNotIn(("newton", "Newton's Laws"), related)

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


class TestJudgeUnchangedBehavior(unittest.TestCase):
    def test_judge_defaults_to_kept_on_llm_failure(self):
        def _boom(*a, **k):
            raise RuntimeError("llm down")
        orig = q.call_anthropic_protocol
        q.call_anthropic_protocol = _boom
        try:
            status, reason = q._stage_2_8_judge_query_resolution(
                {"slug": "q1", "title": "T", "body": "b"},
                [("fourier", "Fourier Transform")], object())
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


if __name__ == "__main__":
    unittest.main()
