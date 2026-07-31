"""test_lint_fix_stub.py — broken-link stub fix must also rewrite the source link.

Regression for the stub-rewrite bug: a broken-link finding with no
suggested_target used to create a stub page but never repoint the source page's
[[broken]] link at it, leaving the link dangling and the fix non-idempotent
(the next lint re-reported the same broken-link forever). NashSU handleFix does
ensureBrokenLinkStub THEN rewriteWikilinkTarget; this verifies the port now
matches and that a second lint pass is clean.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# wiki-lint-fix.py has a hyphen → import by path.
_spec = importlib.util.spec_from_file_location(
    "wiki_lint_fix", _SCRIPTS_DIR / "wiki-lint-fix.py")
wlf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wlf)

from _lint_suggest import run_structural_lint  # noqa: E402


def _page(body: str) -> str:
    return f"---\ntype: concept\ntitle: Src\n---\n\n{body}\n"


class TestStubFixRewritesSource(unittest.TestCase):
    def _build(self, tmp: Path) -> Path:
        wiki = tmp / "wiki"
        (wiki / "concepts").mkdir(parents=True)
        # A broken link with no similar page → no suggestion → stub branch.
        (wiki / "concepts" / "src.md").write_text(
            _page("See [[Foo Bar Baz]] for details."), encoding="utf-8")
        return wiki

    def test_stub_creates_page_and_repoints_link_idempotently(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            wiki = self._build(tmp)

            pages = wlf._collect_pages(wiki)
            findings = run_structural_lint(pages, with_suggestions=True)
            broken = [f for f in findings if f["type"] == "broken-link"]
            self.assertEqual(len(broken), 1)
            self.assertIsNone(broken[0].get("suggested_target"),
                              "test premise: this broken link must have no suggestion")

            actions = wlf.plan_fixes(findings)
            stub_actions = [a for a in actions if a["kind"] == "stub"]
            self.assertEqual(len(stub_actions), 1)
            self.assertEqual(stub_actions[0].get("page"), "concepts/src.md",
                             "stub action must carry the source page")

            wlf.apply_fixes(tmp, wiki, actions, dry_run=False)

            # 1. Stub page was created.
            stub = wiki / "queries" / "foo-bar-baz.md"
            self.assertTrue(stub.exists(), "stub page should be created")

            # 2. Source link was repointed to the stub.
            src = (wiki / "concepts" / "src.md").read_text(encoding="utf-8")
            self.assertIn("[[queries/foo-bar-baz]]", src)
            self.assertNotIn("[[Foo Bar Baz]]", src)

            # 3. Idempotent: a re-lint finds no broken-link for it.
            pages2 = wlf._collect_pages(wiki)
            findings2 = run_structural_lint(pages2, with_suggestions=True)
            broken2 = [f for f in findings2 if f["type"] == "broken-link"]
            self.assertEqual(broken2, [], "re-lint should report no broken links")

    def test_rewrite_before_append_no_duplicate_link(self):
        # Regression: an append action that adds [[concepts/transformer]] used to
        # run BEFORE the rewrite that fixed a broken variant of the same link →
        # two identical links. Link-fixing actions must run first.
        # 2026-07-10: the broken link here is a quote-leak variant
        # ([[concepts/transformer"]], suggestion score 1.0) rather than the old
        # letter-transposition typo (score ~0.82) — fuzzy-tier suggestions now
        # route to REVIEW instead of auto-rewriting (see
        # test_lint_fix_score_gate), so the ordering scenario needs a
        # suggestion that clears the 0.9 auto-rewrite gate.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            wiki = tmp / "wiki"
            (wiki / "concepts").mkdir(parents=True)
            (wiki / "concepts" / "transformer.md").write_text(
                _page("# Transformer\nA transformer."), encoding="utf-8")
            # Quote-leak broken link → exact match after strip, score 1.0.
            (wiki / "concepts" / "usage.md").write_text(
                _page('# Usage\nSee [[concepts/transformer"]].'), encoding="utf-8")

            pages = wlf._collect_pages(wiki)
            findings = run_structural_lint(pages, with_suggestions=True)
            actions = wlf.plan_fixes(findings)
            wlf.apply_fixes(tmp, wiki, actions, dry_run=False)

            usage = (wiki / "concepts" / "usage.md").read_text(encoding="utf-8")
            self.assertEqual(
                usage.count("[[concepts/transformer]]"), 1,
                f"expected exactly one canonical link, got:\n{usage}")
            self.assertNotIn('[[concepts/transformer"]]', usage)

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            wiki = self._build(tmp)
            pages = wlf._collect_pages(wiki)
            findings = run_structural_lint(pages, with_suggestions=True)
            actions = wlf.plan_fixes(findings)

            before = (wiki / "concepts" / "src.md").read_text(encoding="utf-8")
            wlf.apply_fixes(tmp, wiki, actions, dry_run=True)
            after = (wiki / "concepts" / "src.md").read_text(encoding="utf-8")
            self.assertEqual(before, after, "dry-run must not modify the source page")
            self.assertFalse((wiki / "queries" / "foo-bar-baz.md").exists(),
                             "dry-run must not create the stub")


if __name__ == "__main__":
    unittest.main()
