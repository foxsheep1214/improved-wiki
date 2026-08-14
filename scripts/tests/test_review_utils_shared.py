"""One shared answer to "is this review still pending?".

Three call sites had grown three different implementations, and they disagreed
on real inputs (measured 2026-08-05):

    input              sweep   batch_resolve   _stage_validators
    resolved: yes      True    False           True        <-- disagree
    resolved: false    False   False           False
    (beyond char 500)  False   False           True        <-- disagree

    sweep_reviews._scan_reviews : full frontmatter parse, accepts true/yes/1
    batch_resolve_reviews       : regex on one key, accepts only "true"
    _stage_validators           : `"resolved: false" in content[:500]` — a raw
                                  substring over a truncated prefix, so a long
                                  frontmatter silently reads as resolved

A review that says `resolved: yes` was pending to one tool and done to another;
one whose frontmatter runs past 500 characters vanished from the validator's
pending count. Collapsed onto `_review_utils.is_review_resolved`.

Stdlib unittest only — no pytest, no network, no LLM calls.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _review_utils import is_review_resolved  # noqa: E402


def _page(resolved: str, *, pad: int = 0) -> str:
    padding = "".join(f"note_{i}: filler value\n" for i in range(pad))
    return (f"---\nreview_type: suggestion\ntitle: \"T\"\n{padding}"
            f"resolved: {resolved}\nresolved_at: null\n---\n\n# T\n")


class TruthyFormsAllCount(unittest.TestCase):
    def test_accepts_the_forms_sweep_already_accepted(self):
        for value in ("true", "True", "TRUE", "yes", "1"):
            self.assertTrue(is_review_resolved(_page(value)), value)

    def test_quoted_values_count(self):
        for value in ('"true"', "'true'"):
            self.assertTrue(is_review_resolved(_page(value)), value)


class PendingForms(unittest.TestCase):
    def test_false_and_friends_are_pending(self):
        for value in ("false", "False", "no", "0", "null"):
            self.assertFalse(is_review_resolved(_page(value)), value)

    def test_missing_key_is_pending(self):
        self.assertFalse(is_review_resolved(
            "---\nreview_type: suggestion\n---\n\n# T\n"))

    def test_empty_content_is_pending(self):
        self.assertFalse(is_review_resolved(""))


class NotFooledByPosition(unittest.TestCase):
    def test_long_frontmatter_still_reads_as_pending(self):
        """The validator's content[:500] prefix scan lost these entirely."""
        page = _page("false", pad=120)
        self.assertGreater(page.index("resolved:"), 500)
        self.assertFalse(is_review_resolved(page))

    def test_long_frontmatter_still_reads_as_resolved(self):
        self.assertTrue(is_review_resolved(_page("true", pad=120)))

    def test_body_mention_does_not_flip_the_verdict(self):
        page = ("---\nreview_type: suggestion\nresolved: false\n---\n\n"
                "# T\n\nEarlier we wrote resolved: true in prose.\n")
        self.assertFalse(is_review_resolved(page))


class CallersShareTheHelper(unittest.TestCase):
    def test_no_caller_reimplements_the_test(self):
        import inspect
        import batch_resolve_reviews, sweep_reviews, _stage_validators
        # The banned shapes are the three original hand-rolled variants.
        self.assertNotIn('"resolved: false" in',
                         inspect.getsource(_stage_validators))
        for mod in (batch_resolve_reviews, sweep_reviews, _stage_validators):
            self.assertIn("is_review_resolved", inspect.getsource(mod),
                          f"{mod.__name__} must use the shared helper")


if __name__ == "__main__":
    unittest.main()
