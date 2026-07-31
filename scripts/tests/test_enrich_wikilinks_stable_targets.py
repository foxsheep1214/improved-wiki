"""Regression: enrich_wikilinks_batch must build an identical prompt whether or
not this ingest's own pages already appear in existing_slugs.

Bug (2026-07-01): on a conversation-mode resume, list_existing_slugs rescans the
wiki and now includes the just-written pages, so `existing_slugs[:200]` shifted,
changing the enrichment prompt hash and spuriously issuing a SECOND enrichment
handoff for the same ingest. Fix: filter the batch's own slugs out of the
"existing" snapshot (they are re-added as batch targets). This test locks in
prompt stability across the two cases.

Stdlib unittest only — call_anthropic_protocol is monkeypatched (no network/LLM).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _enrich_wikilinks as ewl  # noqa: E402


def _make_pages():
    body = "This page explains the alpha concept in relation to the beta concept. " * 3
    return [
        ("wiki/concepts/alpha.md", f"---\ntype: concept\ntitle: Alpha\n---\n\n# Alpha\n\n{body}"),
        ("wiki/concepts/beta.md", f"---\ntype: concept\ntitle: Beta\n---\n\n# Beta\n\n{body}"),
    ]


def _make_mixed_pages():
    """One zero-outlink candidate plus one already-linked sibling."""
    body = "This page explains the alpha concept in relation to the beta concept. " * 3
    return [
        ("wiki/concepts/alpha.md", f"---\ntype: concept\ntitle: Alpha\n---\n\n# Alpha\n\n{body}"),
        (
            "wiki/concepts/beta.md",
            "---\ntype: concept\ntitle: Beta\n---\n\n# Beta\n\n"
            f"{body} See [[concepts/radar-range-equation]].",
        ),
    ]


class TestEnrichStableTargets(unittest.TestCase):
    def setUp(self):
        self._orig = ewl.call_anthropic_protocol
        self.captured = []

        def _fake(prompt, config, **kwargs):
            self.captured.append(prompt)
            return "{}", None  # valid empty JSON → no changes, no ConversationPending

        ewl.call_anthropic_protocol = _fake

    def tearDown(self):
        ewl.call_anthropic_protocol = self._orig

    def test_prompt_identical_regardless_of_batch_in_existing(self):
        pages = _make_pages()
        existing_pre = ["radar-range-equation", "cfar", "swerling"]
        # Run A: pre-ingest snapshot (batch pages NOT yet on disk).
        ewl.enrich_wikilinks_batch(pages, list(existing_pre), config=None)
        # Run B (resume): the two batch slugs are now part of existing_slugs.
        existing_with_batch = existing_pre + ["alpha", "beta"]
        ewl.enrich_wikilinks_batch(pages, existing_with_batch, config=None)

        self.assertEqual(len(self.captured), 2)
        self.assertEqual(
            self.captured[0], self.captured[1],
            "enrichment prompt must be identical across resume (batch slugs "
            "filtered from existing snapshot)",
        )

    def test_batch_slugs_still_targets(self):
        pages = _make_pages()
        ewl.enrich_wikilinks_batch(pages, ["radar-range-equation"], config=None)
        prompt = self.captured[0]
        # sibling slugs remain valid link targets
        self.assertIn("[[alpha]]", prompt)
        self.assertIn("[[beta]]", prompt)
        self.assertIn("[[radar-range-equation]]", prompt)

    def test_prompt_stable_with_linked_and_zero_outlink_batch_pages(self):
        """Regression from the live Hennessy ingest (2026-07-20).

        The zero-outlink gate excluded an already-linked sibling from
        ``batch_slugs``. After a conversation-mode resume that sibling leaked
        into ``existing_slugs[:200]``, producing a new prompt hash and a second
        1024-page enrichment handoff.
        """
        pages = _make_mixed_pages()
        existing_pre = ["radar-range-equation", "cfar", "swerling"]
        ewl.enrich_wikilinks_batch(pages, list(existing_pre), config=None)
        ewl.enrich_wikilinks_batch(
            pages, existing_pre + ["alpha", "beta"], config=None)

        self.assertEqual(len(self.captured), 2)
        self.assertEqual(self.captured[0], self.captured[1])
        # The linked sibling remains available as a target even though it is
        # not itself included in the Pages To Enrich section.
        self.assertIn("[[beta]]", self.captured[0])
        self.assertNotIn("## PAGE: wiki/concepts/beta.md", self.captured[0])


if __name__ == "__main__":
    unittest.main()
