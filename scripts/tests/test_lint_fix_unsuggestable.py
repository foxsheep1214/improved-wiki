"""test_lint_fix_unsuggestable.py — orphan/no-outlinks with no suggestion route to review.

Regression for audit M4 (2026-07-07): plan_fixes silently dropped orphan (no
suggested_source) and no-outlinks (no suggested_target) findings — no stub or
append action is possible without a suggestion, so they vanished. NashSU
handleFix routes unsuggestable findings to the Review store; the port now
matches via _emit_review_for_unsuggestable (called in --no-stub mode, which
wiki-lint.sh passes during its default fix-links stage unless disabled).

Stdlib unittest only.
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

_spec = importlib.util.spec_from_file_location(
    "wiki_lint_fix", _SCRIPTS_DIR / "wiki-lint-fix.py")
wlf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wlf)


class EmitReviewForUnsuggestable(unittest.TestCase):
    def _findings(self):
        return [
            {"type": "orphan", "page": "concepts/orphan1.md"},
            {"type": "no-outlinks", "page": "concepts/noout1.md"},
            {"type": "orphan", "page": "concepts/has-source.md",
             "suggested_source": "sources/book.md"},
            {"type": "no-outlinks", "page": "concepts/has-target.md",
             "suggested_target": "concepts/x.md"},
            {"type": "orphan", "page": "concepts/orphan1.md"},  # dup
            {"type": "broken-link", "page": "concepts/other.md"},  # wrong type
        ]

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d) / "wiki"; wiki.mkdir()
            wlf._emit_review_for_unsuggestable(wiki, self._findings(), dry_run=True)
            self.assertFalse((wiki / "REVIEW").exists())

    def test_apply_emits_only_unsuggestable_unique(self):
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d) / "wiki"; wiki.mkdir()
            wlf._emit_review_for_unsuggestable(wiki, self._findings(), dry_run=False)
            files = sorted((wiki / "REVIEW" / "suggestion").glob("*.md"))
            # orphan1 + noout1 only; has-source/has-target skipped (had suggestion),
            # dup orphan1 skipped, broken-link skipped (wrong type).
            self.assertEqual(len(files), 2)
            names = " ".join(f.name for f in files)
            # New scheme: <type>-<topic>-<date>.md (topic from the review title
            # "Unsuggestable <kind>: <page>"). See _review_utils.review_filename.
            self.assertIn("Unsuggestable-orphan-concepts-orphan1", names)
            # topic truncates at 40 chars (_review_utils._REVIEW_TOPIC_MAXLEN)
            self.assertIn("Unsuggestable-no-outlinks-concepts-noout", names)

    def test_emitted_item_shape(self):
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d) / "wiki"; wiki.mkdir()
            wlf._emit_review_for_unsuggestable(
                wiki, [{"type": "orphan", "page": "concepts/foo.md"}], dry_run=False)
            txt = (wiki / "REVIEW" / "suggestion").glob("*.md").__next__().read_text()
            self.assertIn("type: review", txt)
            self.assertIn("review_type: suggestion", txt)
            self.assertIn("resolved: false", txt)
            self.assertIn("concepts/foo.md", txt)

    def test_no_unsuggestable_emits_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d) / "wiki"; wiki.mkdir()
            # All have suggestions → nothing emitted.
            findings = [
                {"type": "orphan", "page": "a.md", "suggested_source": "s.md"},
                {"type": "no-outlinks", "page": "b.md", "suggested_target": "c.md"},
            ]
            wlf._emit_review_for_unsuggestable(wiki, findings, dry_run=False)
            self.assertFalse((wiki / "REVIEW").exists())

    def test_filename_no_double_md(self):
        # page ends in .md; the slug must strip it so the filename isn't foo.md.md
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d) / "wiki"; wiki.mkdir()
            wlf._emit_review_for_unsuggestable(
                wiki, [{"type": "orphan", "page": "concepts/foo.md"}], dry_run=False)
            f = list((wiki / "REVIEW" / "suggestion").glob("*.md"))[0]
            self.assertFalse(f.name.endswith(".md.md"))


if __name__ == "__main__":
    unittest.main()
