"""Regression tests for merge_page_content()'s "fast path 5" (replay guard).

Fast path 5 keeps the EXISTING body and skips the LLM merger. It exists for
one situation only: conversation mode leaves the write loop on every merge
handoff, so the resumed loop replays FILE blocks it already merged.

It used to infer that from frontmatter — "existing.sources ⊇ new.sources" —
which was wrong twice over:

  * 2026-07-09, type:source pages. A source page's `sources:` is a
    self-referential singleton, so the condition held on EVERY re-ingest of
    the same book. Every re-generated source-page body was silently
    discarded (found live: body byte-identical despite a fresh generation
    with 67 claims vs. 8, 28 entities vs. 2). Patched by excluding
    type:source — which left the same hole open one level up.

  * 2026-07-29, multi-source pages. On any page two or more books
    contributed to, the superset holds by construction on every later
    re-ingest, so a corrected source could never retract its earlier
    wording. NashSU keeps multi-source pages on the merger for exactly this
    reason (ingest.ts:1938-1939, "their other sources' contributions must
    survive").

Fix: stop guessing. The write loop keeps a per-page ledger keyed on the exact
incoming block bytes (_ingest_write._load_write_ledger) and passes
``already_merged`` only for a genuine replay.

Stdlib unittest only — no pytest, no network, no LLM calls.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _frontmatter import merge_page_content  # noqa: E402


def _page(fm_type: str, body: str, sources: str = "raw/Book/X.pdf") -> str:
    return (
        "---\n"
        f"type: {fm_type}\n"
        'title: "X"\n'
        "tags: [radar]\n"
        "related: []\n"
        f'sources: ["{sources}"]\n'
        "---\n\n"
        f"{body}"
    )


class SourcePageAlwaysCallsMerger(unittest.TestCase):
    """type:source pages must never hit the idempotent-skip fast path, even
    though old.sources == new.sources is guaranteed by construction."""

    def test_source_page_with_same_source_calls_llm_merger(self):
        existing = _page("source", "## Book Summary\nOld thin summary.\n", sources="raw/Book/X.pdf")
        new = _page("source", "## Book Summary\nFresh rich summary with real claims.\n",
                     sources="raw/Book/X.pdf")

        called = []

        def merger(prev_content, merged_content, source_file):
            called.append(True)
            return _page("source", "## Book Summary\nFresh rich summary with real claims.\n",
                          sources="raw/Book/X.pdf")

        result = merge_page_content(new, existing, merger_fn=merger)
        self.assertTrue(called, "LLM merger must be invoked for a source-page re-ingest")
        self.assertIn("Fresh rich summary", result)
        self.assertNotIn("Old thin summary", result)

    def test_multi_source_page_merges_corrected_body(self):
        """A superset `sources:` is NOT proof the incoming body was merged.

        On a page two or more books contributed to, existing.sources ⊇
        new.sources holds on every later re-ingest by construction. Treating
        that as "already merged" discarded the freshly generated body, so a
        corrected source could never retract its earlier wording — NashSU
        keeps multi-source pages on the merger for this exact reason
        (ingest.ts:1938-1939).
        """
        existing = _page("concept", "## Definition\nStale wording from A and B.\n",
                          sources='"raw/Book/A.pdf", "raw/Book/B.pdf"')
        new = _page("concept", "## Definition\nCorrected wording from B alone.\n",
                     sources="raw/Book/B.pdf")

        called = []

        def merger(prev_content, merged_content, source_file):
            called.append(True)
            return _page("concept",
                          "## Definition\nStale wording from A, corrected by B.\n",
                          sources='"raw/Book/A.pdf", "raw/Book/B.pdf"')

        result = merge_page_content(new, existing, merger_fn=merger)
        self.assertTrue(
            called,
            "a genuinely new body on a multi-source page must reach the merger")
        self.assertIn("corrected by B", result)

    def test_replayed_write_short_circuits(self):
        """The scenario fast path 5 exists for: the write loop was resumed
        after a merge handoff and is replaying a block it already merged.
        The caller proves it with the per-page write ledger."""
        existing = _page("concept", "## Definition\nAlready-merged body from A and B.\n",
                          sources='"raw/Book/A.pdf", "raw/Book/B.pdf"')
        new = _page("concept", "## Definition\nFresh-generation body from B alone.\n",
                     sources="raw/Book/B.pdf")

        called = []

        def merger(prev_content, merged_content, source_file):
            called.append(True)
            return merged_content

        result = merge_page_content(
            new, existing, merger_fn=merger, already_merged=True)
        self.assertFalse(called, "a replayed write must not re-invoke the merger")
        self.assertIn("Already-merged body from A and B", result)

    def test_explicit_sole_source_replacement_skips_merger(self):
        existing = _page(
            "concept",
            "## Definition\nObsolete wording that the corrected source retracts.\n",
        )
        new = _page(
            "concept",
            "## Definition\nCorrected source-grounded wording.\n",
        )
        called = []

        def merger(prev_content, merged_content, source_file):
            called.append(True)
            return merged_content

        result = merge_page_content(
            new,
            existing,
            merger_fn=merger,
            replace_existing_body=True,
        )
        self.assertFalse(called)
        self.assertIn("Corrected source-grounded wording", result)
        self.assertNotIn("Obsolete wording", result)


if __name__ == "__main__":
    unittest.main()
