"""Stage 3.4 failure semantics (fix 2026-07-12).

An LLM failure (retries exhausted) or a YAML parse that yields zero items must
RAISE RuntimeError — not silently degrade to 0 review pages — and append the
failure to runtime_dir/ingest-warnings.log. Review generation runs before page
writes, so a failure leaves Phase 3 mutation-free; the conversation cache makes
a resume cheap.

Run:  python3 scripts/tests/test_review_failure_raises.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _stage_3_4_review as review  # noqa: E402


def _make_config(tmp: Path) -> SimpleNamespace:
    wiki_root = tmp / "proj"
    wiki_dir = wiki_root / "wiki"
    runtime_dir = wiki_root / ".llm-wiki"
    wiki_dir.mkdir(parents=True)
    runtime_dir.mkdir(parents=True)
    return SimpleNamespace(
        wiki_root=wiki_root,
        wiki_dir=wiki_dir,
        runtime_dir=runtime_dir,
        conversation_prefix="testpfx",
        llm_model="test-model",
    )


# 4 blocks >= NashSU threshold, so the review actually fires.
_BLOCKS = [(f"concepts/p{i}.md", f"---\ntype: concept\n---\n# P{i}\nbody {i}\n")
           for i in range(4)]


class TestReviewFailureRaises(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config = _make_config(Path(self._tmp.name))
        self.raw_file = Path(self._tmp.name) / "raw" / "Book.pdf"
        self._orig_retry = review.call_with_retry

    def tearDown(self):
        review.call_with_retry = self._orig_retry
        self._tmp.cleanup()

    def _log_text(self) -> str:
        p = self.config.runtime_dir / "ingest-warnings.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def test_llm_failure_raises_and_logs(self):
        def _boom(fn, **kw):
            raise ValueError("provider down")
        review.call_with_retry = _boom
        with self.assertRaises(RuntimeError) as ctx:
            review.stage_3_4_review_suggestions(_BLOCKS, self.raw_file, self.config)
        self.assertIn("provider down", str(ctx.exception))
        log = self._log_text()
        self.assertIn("Book.pdf", log)
        self.assertIn("provider down", log)

    def test_yaml_garbage_raises_and_logs(self):
        review.call_with_retry = lambda fn, **kw: ("total garbage, not yaml", "end_turn")
        with self.assertRaises(RuntimeError) as ctx:
            review.stage_3_4_review_suggestions(_BLOCKS, self.raw_file, self.config)
        self.assertIn("parse failed", str(ctx.exception))
        self.assertIn("parse failed", self._log_text())

    def test_empty_yaml_array_is_legitimate_and_does_not_raise(self):
        # Since the "at least 5 items" padding requirement was dropped for
        # NashSU parity, an explicit "[]" is a legitimate "nothing found"
        # response and must NOT raise or be treated as a parse failure.
        review.call_with_retry = lambda fn, **kw: ("```yaml\n[]\n```", "end_turn")
        result = review.stage_3_4_review_suggestions(_BLOCKS, self.raw_file, self.config)
        self.assertEqual(result.get("items"), 0)

    def test_valid_yaml_still_writes_review_pages(self):
        yaml_resp = (
            "```yaml\n"
            "- id: 1\n"
            "  type: confirm\n"
            '  title: "check numbers"\n'
            '  description: "verify"\n'
            '  affected_pages: ["concepts/p0.md"]\n'
            "  severity: low\n"
            "  search_queries: []\n"
            "```"
        )
        review.call_with_retry = lambda fn, **kw: (yaml_resp, "end_turn")
        result = review.stage_3_4_review_suggestions(_BLOCKS, self.raw_file, self.config)
        self.assertEqual(result.get("items"), 1)
        self.assertEqual(len(result.get("page_refs", [])), 1)
        self.assertTrue(any((self.config.wiki_dir / "REVIEW").rglob("*.md")))

    def test_prepare_is_pre_write_and_persist_is_post_write(self):
        yaml_resp = (
            "```yaml\n"
            "- id: 1\n"
            "  type: confirm\n"
            '  title: "check numbers"\n'
            '  description: "verify"\n'
            '  affected_pages: ["concepts/p0.md"]\n'
            "  severity: low\n"
            "  search_queries: []\n"
            "```"
        )
        review.call_with_retry = lambda fn, **kw: (yaml_resp, "end_turn")

        prepared = review.stage_3_4_prepare_review_suggestions(
            _BLOCKS, self.raw_file, self.config)
        self.assertEqual(len(prepared.get("items_data", [])), 1)
        self.assertFalse((self.config.wiki_dir / "REVIEW").exists())

        result = review.stage_3_4_persist_review_suggestions(
            prepared, self.raw_file, self.config)
        self.assertEqual(result.get("items"), 1)
        self.assertTrue(any((self.config.wiki_dir / "REVIEW").rglob("*.md")))

    def test_unknown_type_and_traversal_are_rejected_before_write(self):
        yaml_resp = (
            "- id: 1\n"
            "  type: ../../outside\n"
            '  title: "unsafe"\n'
            '  description: "unsafe path"\n'
            '  affected_pages: ["../secrets.md"]\n'
            "  severity: high\n"
            "  search_queries: []\n"
        )
        review.call_with_retry = lambda fn, **kw: (yaml_resp, "end_turn")

        with self.assertRaisesRegex(RuntimeError, "schema validation failed"):
            review.stage_3_4_review_suggestions(
                _BLOCKS, self.raw_file, self.config)
        self.assertFalse((self.config.wiki_dir / "REVIEW").exists())
        self.assertIn("affected_pages", self._log_text())

    def test_research_item_requires_two_or_three_search_queries(self):
        yaml_resp = (
            "- id: 1\n"
            "  type: suggestion\n"
            '  title: "research gap"\n'
            '  description: "needs external evidence"\n'
            '  affected_pages: ["concepts/p0.md"]\n'
            "  severity: medium\n"
            '  search_queries: ["only one query"]\n'
        )
        review.call_with_retry = lambda fn, **kw: (yaml_resp, "end_turn")

        with self.assertRaisesRegex(RuntimeError, "requires 2-3"):
            review.stage_3_4_review_suggestions(
                _BLOCKS, self.raw_file, self.config)
        self.assertFalse((self.config.wiki_dir / "REVIEW").exists())

    def test_non_research_item_rejects_search_queries(self):
        yaml_resp = (
            "- id: 1\n"
            "  type: confirm\n"
            '  title: "check value"\n'
            '  description: "verify value"\n'
            '  affected_pages: ["concepts/p0.md"]\n'
            "  severity: low\n"
            '  search_queries: ["unexpected query"]\n'
        )
        review.call_with_retry = lambda fn, **kw: (yaml_resp, "end_turn")

        with self.assertRaisesRegex(RuntimeError, "empty search_queries"):
            review.stage_3_4_review_suggestions(
                _BLOCKS, self.raw_file, self.config)


if __name__ == "__main__":
    unittest.main()
