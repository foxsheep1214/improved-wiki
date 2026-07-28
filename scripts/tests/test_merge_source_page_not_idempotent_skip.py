"""Regression test for the 2026-07-09 source-page merge bug.

merge_page_content()'s "fast path 5" (idempotent re-merge guard) treats
existing.sources being a superset of new.sources as proof "this collision was
already merged in a prior run" and short-circuits to keep the EXISTING body
untouched. That heuristic is correct for concept/entity pages, which
legitimately accumulate `sources:` contributions from multiple different
books over time. It is wrong for type:source pages: a source page's
`sources:` field is a self-referential singleton (always exactly the one raw
file it describes), so old.sources == new.sources on EVERY re-ingest of the
same book, by construction — the fast path fired unconditionally and silently
discarded every re-generated source-page body, keeping only the frontmatter
array unions. Found via a live re-ingest where the body stayed byte-identical
to the pre-re-ingest version despite a fresh, substantially different
LLM-generated body (67 claims vs. 8, 28 entities vs. 2, etc.).

Fix: exclude type:source pages from fast path 5, so they always fall through
to the real LLM merge (or the earlier, still-valid fast paths for genuine
no-op cases: byte-identical, images-only diff, wikilink-only diff).

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

    def test_concept_page_with_superset_sources_still_short_circuits(self):
        """Unchanged behavior for the scenario fast path 5 was built for:
        a concept page whose sources already include the new content's
        source (multi-book accumulation / crash-resume) must still skip the
        LLM merger and keep the existing (already-merged) body."""
        existing = _page("concept", "## Definition\nAlready-merged body from A and B.\n",
                          sources='"raw/Book/A.pdf", "raw/Book/B.pdf"')
        new = _page("concept", "## Definition\nFresh-generation body from B alone.\n",
                     sources="raw/Book/B.pdf")

        called = []

        def merger(prev_content, merged_content, source_file):
            called.append(True)
            return merged_content

        result = merge_page_content(new, existing, merger_fn=merger)
        self.assertFalse(called, "concept-page idempotent-skip fast path must still fire")
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
