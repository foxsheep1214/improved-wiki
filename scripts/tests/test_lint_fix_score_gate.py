"""Tests for wiki-lint-fix.py's score-gated broken-link auto-rewrite
(2026-07-10, user-approved lint hardening).

Policy: a broken-link finding with a suggested_target is only auto-rewritten
when its suggested_tier is exact or same-basename. Contains-tier and
fuzzy-Levenshtein suggestions — however high their score — instead become
REVIEW/suggestion items carrying the proposed target, for a human to approve.
A finding without a tier (stale cache from an older lint) is rewritten only
when its score is exact.

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
import tempfile
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


def _bl(page, broken, suggested, score, tier=None):
    f = {"type": "broken-link", "severity": "warning", "page": page,
         "detail": f"Broken link: [[{broken}]] — target page not found.",
         "broken_target": broken, "suggested_target": suggested}
    if score is not None:
        f["suggested_score"] = score
    if tier is not None:
        f["suggested_tier"] = tier
    return f


class TestPlanFixesScoreGate(unittest.TestCase):
    def test_high_score_becomes_rewrite(self):
        wlf = _load_module()
        actions = wlf.plan_fixes([_bl("a.md", "concepts/transformer", "methodology/transformer.md",
                                      0.96, "basename")])
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["kind"], "rewrite")

    def test_mid_score_becomes_review_rewrite(self):
        wlf = _load_module()
        actions = wlf.plan_fixes([_bl("a.md", "some phrase with transformer inside",
                                      "transformer.md", 0.82, "fuzzy")])
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

    def test_high_scoring_fuzzy_match_goes_to_review(self):
        # One edit apart, opposite meaning: the score alone cannot tell.
        wlf = _load_module()
        actions = wlf.plan_fixes([_bl(
            "a.md", "concepts/ac-coupling", "concepts/dc-coupling.md", 0.95, "fuzzy")])
        self.assertEqual(actions[0]["kind"], "review-rewrite")

    def test_tierless_cache_rewrites_only_an_exact_score(self):
        wlf = _load_module()
        actions = wlf.plan_fixes([_bl("a.md", "x", "y.md", 1.0),
                                  _bl("a.md", "p", "q.md", 0.96)])
        self.assertEqual([a["kind"] for a in actions], ["rewrite", "review-rewrite"])

    def test_no_suggestion_still_becomes_stub_action(self):
        wlf = _load_module()
        actions = wlf.plan_fixes([_bl("a.md", "missing-thing", None, None)])
        self.assertEqual(actions[0]["kind"], "stub")

    def test_redirect_frontmatter_origin_is_preserved_and_applied(self):
        wlf = _load_module()
        finding = _bl(
            "entities/legacy.md", "entities/canoncal",
            "entities/canonical.md", 0.96, "basename")
        finding["link_origin"] = "redirect-frontmatter"
        actions = wlf.plan_fixes([finding])
        self.assertEqual(actions[0]["link_origin"], "redirect-frontmatter")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wiki = root / "wiki"
            (wiki / "entities").mkdir(parents=True)
            page = wiki / "entities" / "legacy.md"
            page.write_text(
                "---\ntype: redirect\nmetadata:\n"
                "  redirect: entities/canoncal\n"
                "redirect: entities/canoncal\n---\n\n"
                "See [[entities/canoncal]].\n",
                encoding="utf-8",
            )
            summary = wlf.apply_fixes(root, wiki, actions, dry_run=False)
            updated = page.read_text(encoding="utf-8")
            self.assertEqual(summary["rewrite"], 1)
            self.assertIn('redirect: "entities/canonical"', updated)
            self.assertIn("  redirect: entities/canoncal", updated)
            self.assertIn("[[entities/canonical]]", updated)


class TestMainEndToEndScoreGate(unittest.TestCase):
    def _make_wiki(self, root: Path) -> Path:
        wiki = root / "wiki"
        (wiki / "concepts").mkdir(parents=True)
        (wiki / "concepts" / "transformer.md").write_text(
            "---\ntype: concept\ntitle: Transformer\n---\n\n# T\nbody [[concepts/attention]].",
            encoding="utf-8")
        (wiki / "concepts" / "attention.md").write_text(
            "---\ntype: concept\ntitle: Attention\n---\n\n# A\n"
            "high [[methodology/transformer]] and mid [[transformer overview note]].",
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
                _bl("concepts/attention.md", "methodology/transformer",
                    "concepts/transformer.md", 0.96, "basename"),
                _bl("concepts/attention.md", "transformer overview note",
                    "concepts/transformer.md", 0.82, "fuzzy"),
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
            # same-basename rewritten; the contains-tier match untouched
            self.assertIn("[[concepts/transformer]]", content)
            self.assertNotIn("[[methodology/transformer]]", content)
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
            review_files = list((wiki / "REVIEW" / "orphan").glob("orphan-*.md"))
            self.assertEqual(len(review_files), 1)
            body = review_files[0].read_text(encoding="utf-8")
            self.assertIn("concepts/lonely.md", body)
            self.assertIn("--delete-orphans --apply", body)

    def test_orphan_delete_review_item_is_human_gated(self):
        """The orphan-delete review item must carry human_gate: true so
        sweep_reviews' LLM judge never auto-resolves it — the judge only sees
        page ids + titles and cannot know inbound-link state."""
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
            old_argv = sys.argv
            sys.argv = ["wiki-lint-fix.py", "--delete-orphans", "--emit-review",
                        "--from-cache", str(cache),
                        "--project-root", str(root), "--wiki-root", str(wiki)]
            try:
                wlf.main()
            finally:
                sys.argv = old_argv
            review = list((wiki / "REVIEW" / "orphan").glob("orphan-*.md"))[0]
            self.assertIn("human_gate: true", review.read_text(encoding="utf-8"))

    def test_stale_cache_orphan_reverified_against_current_disk(self):
        """2026-07-11 (#4): an orphan listed in the cache that has since
        gained an inbound link (e.g. a --fix-links append in the same lint
        run) must be dropped from the preview/review/delete set."""
        import tempfile
        wlf = _load_module()
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            wiki = root / "wiki"
            (wiki / "concepts").mkdir(parents=True)
            # 'rescued' has an inbound link NOW (from linker.md), but the
            # stale cache still lists it as an orphan.
            (wiki / "concepts" / "rescued.md").write_text(
                "---\ntype: concept\ntitle: Rescued\n---\n\n# R\n[[concepts/linker]]",
                encoding="utf-8")
            (wiki / "concepts" / "linker.md").write_text(
                "---\ntype: concept\ntitle: Linker\n---\n\n# L\nSee [[concepts/rescued]].",
                encoding="utf-8")
            cache = root / "lint-cache.json"
            cache.write_text(json.dumps([
                {"type": "orphan", "severity": "info",
                 "page": "concepts/rescued.md",
                 "detail": "No other pages link to this page."},
            ]), encoding="utf-8")
            old_argv = sys.argv
            sys.argv = ["wiki-lint-fix.py", "--delete-orphans", "--emit-review",
                        "--from-cache", str(cache),
                        "--project-root", str(root), "--wiki-root", str(wiki)]
            try:
                rc = wlf.main()
            finally:
                sys.argv = old_argv
            self.assertEqual(rc, 0)
            # no review item, no deletion — the cached orphan was re-verified
            # against current disk and dropped.
            self.assertTrue((wiki / "concepts" / "rescued.md").exists())
            review_dir = wiki / "REVIEW" / "orphan"
            items = list(review_dir.glob("orphan-*.md")) if review_dir.exists() else []
            self.assertEqual(items, [])

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
            review_files = list((wiki / "REVIEW" / "orphan").glob("orphan-*.md"))
            self.assertEqual(len(review_files), 1)


class TestBrokenRelatedRepair(unittest.TestCase):
    def test_confident_suggestion_repoints_and_weak_one_drops(self):
        wlf = _load_module()
        findings = [
            {"type": "broken-related", "page": "concepts/a.md",
             "broken_target": "concepts/scan-loss", "suggested_target":
             "methodology/scan-loss.md", "suggested_score": 0.96,
             "suggested_tier": "basename"},
            {"type": "broken-related", "page": "concepts/a.md",
             "broken_target": "concepts/memory-ranks", "suggested_target":
             "concepts/memory-banks.md", "suggested_score": 0.917,
             "suggested_tier": "fuzzy"},
        ]
        actions = wlf.plan_fixes(findings)
        self.assertEqual([a["replacement"] for a in actions],
                         ["methodology/scan-loss.md", None])
        with tempfile.TemporaryDirectory() as t:
            wiki = Path(t) / "wiki"
            (wiki / "concepts").mkdir(parents=True)
            page = wiki / "concepts" / "a.md"
            page.write_text(
                '---\ntype: concept\nrelated: ["concepts/b", "concepts/scan-loss", '
                '"concepts/memory-ranks"]\n---\n\nBody [[concepts/b]].\n',
                encoding="utf-8")
            summary = wlf.apply_fixes(Path(t), wiki, actions, dry_run=False)
            self.assertEqual(summary["related"], 2)
            self.assertIn('related: ["concepts/b", "methodology/scan-loss"]',
                          page.read_text(encoding="utf-8"))


class TestRedirectMissingTargetFix(unittest.TestCase):
    def test_suggested_target_is_written_and_unsuggested_left(self):
        wlf = _load_module()
        actions = wlf.plan_fixes([
            {"type": "redirect-missing-target", "page": "concepts/old.md",
             "suggested_target": "concepts/new.md"},
            {"type": "redirect-missing-target", "page": "concepts/vague.md",
             "suggested_target": None},
        ])
        self.assertEqual(actions, [{"kind": "redirect", "page": "concepts/old.md",
                                    "target": "concepts/new.md"}])
        with tempfile.TemporaryDirectory() as t:
            wiki = Path(t) / "wiki"
            (wiki / "concepts").mkdir(parents=True)
            stub = wiki / "concepts" / "old.md"
            stub.write_text("---\ntype: redirect\n---\n\nSee [[concepts/new]].\n",
                            encoding="utf-8")
            summary = wlf.apply_fixes(Path(t), wiki, actions, dry_run=False)
            self.assertEqual(summary["redirect"], 1)
            self.assertIn('redirect: "concepts/new"', stub.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
