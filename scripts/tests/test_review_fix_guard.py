"""review_fix_guard — confirm reviews close only after a guarded page repair.

M8: a review-fix batch silently cleared ``related:`` on 4 pages that were NOT
in the review item's ``affected_pages``. No script applies review fixes to
wiki pages (the writer is the conversation agent), so the guard is the
code-side checkpoint that the conversational path runs before/after an edit
batch: every touched page must be declared; the review page itself is always
allowed (marking it resolved is part of the fix).

The original A8/M8 page-scope checks remain; snapshot/finalize adds observable
change and verification gates. Stdlib unittest only.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import review_fix_guard as g  # noqa: E402


_REVIEW_MD = """---
type: review
review_id: review-ab12cd34
review_type: confirm
severity: medium
affected_pages: [queries/低空目标检测.md, comparisons/mti-vs-pulse-doppler.md]
search_queries: []
resolved: false
created: 2026-07-02
source_ingest: "book"
---

# [confirm] 低空目标检测数据待核
"""


_REVIEW_BLOCK_LIST_MD = """---
type: review
review_id: review-block-list
affected_pages:
  - entities/james-watt.md
  - comparisons/mti-vs-pulse-doppler.md
resolved: false
---

# [suggestion] block-style declaration
"""


class NormalizePageRef(unittest.TestCase):
    def test_variants_collapse_to_same_key(self):
        expect = "concepts/foo-bar"
        for ref in ("concepts/foo-bar", "concepts/foo-bar.md",
                    "wiki/concepts/foo-bar.md", "[[concepts/foo-bar]]",
                    "/Users/x/proj/wiki/concepts/Foo-Bar.md"):
            self.assertEqual(g.normalize_page_ref(ref), expect, ref)


class AllowedPages(unittest.TestCase):
    def test_parses_affected_pages(self):
        self.assertEqual(
            g.allowed_pages_from_review(_REVIEW_MD),
            {"queries/低空目标检测", "comparisons/mti-vs-pulse-doppler"},
        )

    def test_parses_block_style_affected_pages(self):
        self.assertEqual(
            g.allowed_pages_from_review(_REVIEW_BLOCK_LIST_MD),
            {"entities/james-watt", "comparisons/mti-vs-pulse-doppler"},
        )

    def test_missing_field_allows_nothing(self):
        self.assertEqual(
            g.allowed_pages_from_review("---\ntype: review\n---\nbody\n"),
            set(),
        )


class CheckTargets(unittest.TestCase):
    def _review_file(self, tmp: Path) -> Path:
        p = tmp / "wiki" / "REVIEW" / "confirm" / "2026-07-02-book-item.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_REVIEW_MD, encoding="utf-8")
        return p

    def test_declared_targets_pass(self):
        with tempfile.TemporaryDirectory() as d:
            rp = self._review_file(Path(d))
            violations = g.check_review_fix_targets(
                rp, ["wiki/queries/低空目标检测.md",
                     "comparisons/mti-vs-pulse-doppler"])
            self.assertEqual(violations, [])

    def test_undeclared_target_is_violation(self):
        """The M8 shape: a related:-clearing edit on a page outside the
        declared affected_pages must be flagged."""
        with tempfile.TemporaryDirectory() as d:
            rp = self._review_file(Path(d))
            violations = g.check_review_fix_targets(
                rp, ["wiki/queries/低空目标检测.md",
                     "wiki/comparisons/ekf-vs-ukf-vs-pf.md"])
            self.assertEqual(violations, ["wiki/comparisons/ekf-vs-ukf-vs-pf.md"])

    def test_review_page_itself_always_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            rp = self._review_file(Path(d))
            self.assertEqual(g.check_review_fix_targets(rp, [str(rp)]), [])

    def test_unreadable_review_raises(self):
        # A guard that cannot see the declaration must not pass anything.
        with self.assertRaises(OSError):
            g.check_review_fix_targets(Path("/nonexistent/review.md"), ["x"])


class RepairLifecycle(unittest.TestCase):
    def _fixture(self, tmp: Path):
        review = (tmp / "wiki" / "REVIEW" / "confirm" /
                  "2026-07-02-book-item.md")
        review.parent.mkdir(parents=True, exist_ok=True)
        review.write_text(_REVIEW_MD, encoding="utf-8")
        page = tmp / "wiki" / "queries" / "低空目标检测.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("# 低空目标检测\n\n旧内容\n", encoding="utf-8")
        return review, page

    def test_snapshot_rejects_undeclared_target(self):
        with tempfile.TemporaryDirectory() as d:
            review, _page = self._fixture(Path(d))
            with self.assertRaisesRegex(ValueError, "not declared"):
                g.create_fix_snapshot(review, ["concepts/out-of-scope.md"])

    def test_snapshot_rejects_absolute_target_outside_wiki(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            review, _page = self._fixture(tmp)
            outside = tmp / "outside" / "wiki" / "queries" / "低空目标检测.md"
            outside.parent.mkdir(parents=True, exist_ok=True)
            outside.write_text("not the project page\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "outside wiki"):
                g.create_fix_snapshot(review, [str(outside)])

    def test_unchanged_page_cannot_finalize(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            review, page = self._fixture(tmp)
            state = tmp / "state.json"
            g.write_fix_snapshot(
                state, g.create_fix_snapshot(review, [str(page)]))
            with self.assertRaisesRegex(ValueError, "no affected page changed"):
                g.finalize_fix(review, state, "structural lint passed")
            self.assertIn("resolved: false",
                          review.read_text(encoding="utf-8"))
            self.assertTrue(state.exists())

    def test_changed_and_verified_page_closes_review(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            review, page = self._fixture(tmp)
            state = tmp / "state.json"
            g.write_fix_snapshot(
                state, g.create_fix_snapshot(review, [str(page)]))
            page.write_text("# 低空目标检测\n\n已修正内容\n", encoding="utf-8")

            reason = g.finalize_fix(
                review, state,
                "source value checked; structural lint passed")

            text = review.read_text(encoding="utf-8")
            self.assertIn("resolved: true", text)
            self.assertIn("Fixed: wiki/queries/低空目标检测.md", text)
            self.assertIn("Verified: source value checked", text)
            self.assertIn("wiki/queries/低空目标检测.md", reason)
            self.assertFalse(state.exists())

    def test_verification_is_required(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            review, page = self._fixture(tmp)
            state = tmp / "state.json"
            g.write_fix_snapshot(
                state, g.create_fix_snapshot(review, [str(page)]))
            page.write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "verification"):
                g.finalize_fix(review, state, "")

    def test_review_change_invalidates_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            review, page = self._fixture(tmp)
            snapshot = g.create_fix_snapshot(review, [str(page)])
            review.write_text(_REVIEW_MD + "\nchanged\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "review changed"):
                g.validate_fix_changes(review, snapshot)


if __name__ == "__main__":
    unittest.main()
