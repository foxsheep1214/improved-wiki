"""Tests for wiki-lint-fix.py's score-gated broken-link auto-rewrite
(2026-07-10, user-approved lint hardening).

Policy: a broken-link finding with a suggested_target is only auto-rewritten
when its suggested_score >= BROKEN_LINK_AUTO_REWRITE_MIN_SCORE (0.9) — i.e.
exact (1.0) and same-basename (0.96) tier suggestions. Contains-tier (0.82)
and fuzzy-Levenshtein suggestions instead become REVIEW/suggestion items
carrying the proposed target, for a human to approve. A finding with a
suggestion but NO score (stale cache from an older lint) is treated
conservatively as below-threshold.

Rationale (real incident class): automated linking once rewrote the literal
substring 脉冲压缩 across 10+ pages to the narrower 脉冲压缩与MTI组合 page —
string-similar is not meaning-similar, and a headless batch multiplies one
bad suggestion. NashSU never faces this: its Fix is human-clicked per item.

The module filename has hyphens (wiki-lint-fix.py) so it is loaded via
importlib. Stdlib unittest only.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "wiki_lint_fix", _SCRIPTS_DIR / "wiki-lint-fix.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bl(page, broken, suggested, score):
    f = {"type": "broken-link", "severity": "warning", "page": page,
         "detail": f"Broken link: [[{broken}]] — target page not found.",
         "broken_target": broken, "suggested_target": suggested}
    if score is not None:
        f["suggested_score"] = score
    return f


class TestPlanFixesScoreGate(unittest.TestCase):
    def test_high_score_becomes_rewrite(self):
        wlf = _load_module()
        actions = wlf.plan_fixes([_bl("a.md", "transfomer-x", "transformer.md", 0.96)])
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["kind"], "rewrite")

    def test_mid_score_becomes_review_rewrite(self):
        wlf = _load_module()
        actions = wlf.plan_fixes([_bl("a.md", "some phrase with transformer inside",
                                      "transformer.md", 0.82)])
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["kind"], "review-rewrite")
        self.assertEqual(actions[0]["suggested"], "transformer.md")
        self.assertEqual(actions[0]["score"], 0.82)

    def test_missing_score_treated_conservatively(self):
        """Stale cache without suggested_score → never auto-rewrite."""
        wlf = _load_module()
        actions = wlf.plan_fixes([_bl("a.md", "transfomer-x", "transformer.md", None)])
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["kind"], "review-rewrite")

    def test_exact_threshold_boundary_auto_rewrites(self):
        wlf = _load_module()
        actions = wlf.plan_fixes([_bl(
            "a.md", "x", "y.md", wlf.BROKEN_LINK_AUTO_REWRITE_MIN_SCORE)])
        self.assertEqual(actions[0]["kind"], "rewrite")

    def test_no_suggestion_still_becomes_stub_action(self):
        wlf = _load_module()
        actions = wlf.plan_fixes([_bl("a.md", "missing-thing", None, None)])
        self.assertEqual(actions[0]["kind"], "stub")


class TestMainEndToEndScoreGate(unittest.TestCase):
    def _make_wiki(self, root: Path) -> Path:
        wiki = root / "wiki"
        (wiki / "concepts").mkdir(parents=True)
        (wiki / "concepts" / "transformer.md").write_text(
            "---\ntype: concept\ntitle: Transformer\n---\n\n# T\nbody [[concepts/attention]].",
            encoding="utf-8")
        (wiki / "concepts" / "attention.md").write_text(
            "---\ntype: concept\ntitle: Attention\n---\n\n# A\n"
            "high [[concepts/transformerX]] and mid [[transformer overview note]].",
            encoding="utf-8")
        return wiki

    def test_mid_band_not_rewritten_and_review_created(self):
        import tempfile
        wlf = _load_module()
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            wiki = self._make_wiki(root)
            cache = root / "lint-cache.json"
            cache.write_text(json.dumps([
                _bl("concepts/attention.md", "concepts/transformerX",
                    "concepts/transformer.md", 0.93),
                _bl("concepts/attention.md", "transformer overview note",
                    "concepts/transformer.md", 0.82),
            ]), encoding="utf-8")

            old_argv = sys.argv
            sys.argv = ["wiki-lint-fix.py", "--apply", "--no-stub",
                        "--from-cache", str(cache),
                        "--project-root", str(root),
                        "--wiki-root", str(wiki)]
            try:
                rc = wlf.main()
            finally:
                sys.argv = old_argv
            self.assertEqual(rc, 0)

            content = (wiki / "concepts" / "attention.md").read_text(encoding="utf-8")
            # high-band (0.93) rewritten; mid-band (0.82) untouched
            self.assertIn("[[concepts/transformer.md]]", content.replace(
                "[[concepts/transformer]]", "[[concepts/transformer.md]]"))
            self.assertNotIn("[[concepts/transformerX]]", content)
            self.assertIn("[[transformer overview note]]", content)
            # mid-band routed to a review item that names the suggested target
            review_files = list((wiki / "REVIEW" / "suggestion").glob("*.md"))
            self.assertEqual(len(review_files), 1)
            body = review_files[0].read_text(encoding="utf-8")
            self.assertIn("transformer overview note", body)
            self.assertIn("concepts/transformer.md", body)
            self.assertIn("0.82", body)


class TestDeleteOrphansEmitReview(unittest.TestCase):
    def test_preview_with_emit_review_writes_items_but_deletes_nothing(self):
        import tempfile
        wlf = _load_module()
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            wiki = root / "wiki"
            (wiki / "concepts").mkdir(parents=True)
            orphan = wiki / "concepts" / "lonely.md"
            orphan.write_text(
                "---\ntype: concept\ntitle: Lonely\n---\n\n# L\n[[concepts/other]]",
                encoding="utf-8")
            (wiki / "concepts" / "other.md").write_text(
                "---\ntype: concept\ntitle: Other\n---\n\n# O\nbody",
                encoding="utf-8")
            cache = root / "lint-cache.json"
            cache.write_text(json.dumps([
                {"type": "orphan", "severity": "info",
                 "page": "concepts/lonely.md",
                 "detail": "No other pages link to this page."},
            ]), encoding="utf-8")

            old_argv = sys.argv
            sys.argv = ["wiki-lint-fix.py", "--delete-orphans", "--emit-review",
                        "--from-cache", str(cache),
                        "--project-root", str(root),
                        "--wiki-root", str(wiki)]
            try:
                rc = wlf.main()
            finally:
                sys.argv = old_argv
            self.assertEqual(rc, 0)
            # preview: the orphan file survives...
            self.assertTrue(orphan.exists())
            # ...but a review item was actually written (that is the point of
            # --emit-review: the preview's actionable output).
            review_files = list((wiki / "REVIEW" / "suggestion").glob("*orphan-delete*"))
            self.assertEqual(len(review_files), 1)
            body = review_files[0].read_text(encoding="utf-8")
            self.assertIn("concepts/lonely.md", body)
            self.assertIn("--delete-orphans --apply", body)

    def test_emit_review_is_idempotent(self):
        import tempfile
        wlf = _load_module()
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            wiki = root / "wiki"
            (wiki / "concepts").mkdir(parents=True)
            (wiki / "concepts" / "lonely.md").write_text(
                "---\ntype: concept\ntitle: Lonely\n---\n\n# L\n[[concepts/other]]",
                encoding="utf-8")
            (wiki / "concepts" / "other.md").write_text(
                "---\ntype: concept\ntitle: Other\n---\n\n# O\nbody",
                encoding="utf-8")
            cache = root / "lint-cache.json"
            cache.write_text(json.dumps([
                {"type": "orphan", "severity": "info",
                 "page": "concepts/lonely.md",
                 "detail": "No other pages link to this page."},
            ]), encoding="utf-8")

            argv = ["wiki-lint-fix.py", "--delete-orphans", "--emit-review",
                    "--from-cache", str(cache),
                    "--project-root", str(root),
                    "--wiki-root", str(wiki)]
            old_argv = sys.argv
            try:
                sys.argv = argv
                wlf.main()
                sys.argv = argv
                wlf.main()
            finally:
                sys.argv = old_argv
            review_files = list((wiki / "REVIEW" / "suggestion").glob("*orphan-delete*"))
            self.assertEqual(len(review_files), 1)


if __name__ == "__main__":
    unittest.main()
