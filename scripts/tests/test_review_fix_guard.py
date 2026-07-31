"""review_fix_guard — review-fix batches may only touch declared pages
(audit 2026-07-02, A8/M8).

M8: a review-fix batch silently cleared ``related:`` on 4 pages that were NOT
in the review item's ``affected_pages``. No script applies review fixes to
wiki pages (the writer is the conversation agent), so the guard is the
code-side checkpoint that the conversational path runs before/after an edit
batch: every touched page must be declared; the review page itself is always
allowed (marking it resolved is part of the fix).

Stdlib unittest only.
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


if __name__ == "__main__":
    unittest.main()
