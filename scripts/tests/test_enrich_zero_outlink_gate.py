"""Redundancy fix C (2026-07-09): gate wikilink enrichment to zero-outlink pages.

Stage 2.4 generation already mandates inline [[wikilinks]] (with a linkable
list) in every page it writes, so the post-write batched enrichment round-trip
was a no-op in practice — the documented safe answer to its handoff was often
`{}` (observed live on the Jakiraman re-ingest, LLM-task-8928cbfc). Keep the
NashSU-parity bailout for pages that genuinely have ZERO outgoing [[links]]
(merge leftovers, legacy pages), skip everything else; when no page qualifies,
the whole LLM round-trip is skipped.

Stdlib unittest only — call_anthropic_protocol is monkeypatched.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _enrich_wikilinks as ew  # noqa: E402


def _page(body: str) -> str:
    return f"---\ntype: concept\ntitle: \"X\"\n---\n\n{body}"


LONG = "This is a substantial page body about radar signal processing. " * 5


class ZeroOutlinkGate(unittest.TestCase):
    def test_all_pages_already_linked_skips_llm_entirely(self):
        pages = [
            ("concepts/a.md", _page(LONG + " See [[concepts/matched-filter]].")),
            ("concepts/b.md", _page(LONG + " Related to [[entities/hansen]].")),
        ]

        def boom(*a, **k):
            raise AssertionError("LLM must not be called when no page qualifies")

        orig = ew.call_anthropic_protocol
        ew.call_anthropic_protocol = boom
        try:
            result = ew.enrich_wikilinks_batch(pages, ["concepts/matched-filter"], object())
        finally:
            ew.call_anthropic_protocol = orig
        self.assertEqual(result, {})

    def test_zero_outlink_page_still_enriched(self):
        linked = ("concepts/a.md", _page(LONG + " See [[concepts/matched-filter]]."))
        bare = ("concepts/b.md", _page(LONG + " The matched filter maximizes SNR."))
        captured = {}

        def fake(prompt, config, **kw):
            captured["prompt"] = prompt
            return ('{"concepts/b.md": [{"term": "matched filter", '
                    '"target": "concepts/matched-filter"}]}', None)

        orig = ew.call_anthropic_protocol
        ew.call_anthropic_protocol = fake
        try:
            result = ew.enrich_wikilinks_batch(
                [linked, bare], ["concepts/matched-filter"], object())
        finally:
            ew.call_anthropic_protocol = orig

        # Only the zero-outlink page goes into the prompt and gets enriched.
        self.assertIn("concepts/b.md", captured["prompt"])
        self.assertNotIn("concepts/a.md", captured["prompt"])
        self.assertIn("concepts/b.md", result)
        self.assertIn(
            "[[concepts/matched-filter|matched filter]]",
            result["concepts/b.md"],
        )
        self.assertNotIn("concepts/a.md", result)

    def test_enrichment_preserves_surface_text_when_target_differs(self):
        page = (
            "findings/margin.md",
            _page(LONG + " Maximum warpage of about 255 micrometers."),
        )

        def fake(prompt, config, **kw):
            return (
                '{"findings/margin.md": [{"term": "warpage", '
                '"target": "hybrid-substrate-reliability-modeling"}]}',
                None,
            )

        orig = ew.call_anthropic_protocol
        ew.call_anthropic_protocol = fake
        try:
            result = ew.enrich_wikilinks_batch(
                [page],
                ["hybrid-substrate-reliability-modeling"],
                object(),
            )
        finally:
            ew.call_anthropic_protocol = orig

        self.assertIn(
            "Maximum [[hybrid-substrate-reliability-modeling|warpage]] "
            "of about 255 micrometers.",
            result["findings/margin.md"],
        )

    def test_enrichment_escapes_alias_separator_in_markdown_table(self):
        page = (
            "comparisons/motors.md",
            _page(
                LONG
                + "\n\n| Dimension | Value |\n"
                "|---|---|\n"
                "| Kind | motor control |\n"
            ),
        )

        def fake(prompt, config, **kw):
            return (
                '{"comparisons/motors.md": [{"term": "motor control", '
                '"target": "concepts/motor-control"}]}',
                None,
            )

        orig = ew.call_anthropic_protocol
        ew.call_anthropic_protocol = fake
        try:
            result = ew.enrich_wikilinks_batch(
                [page],
                ["concepts/motor-control"],
                object(),
            )
        finally:
            ew.call_anthropic_protocol = orig

        self.assertIn(
            "[[concepts/motor-control\\|motor control]]",
            result["comparisons/motors.md"],
        )

    def test_legacy_repair_handles_two_terms_with_same_target(self):
        content = _page(
            "A [[wlcsp-fan-in-redistribution]] architecture mounts balls on "
            "the [[wlcsp-fan-in-redistribution]]."
        )
        suggestions = [
            {"term": "fan-in WLCSP architecture",
             "target": "wlcsp-fan-in-redistribution"},
            {"term": "Cu RDL", "target": "wlcsp-fan-in-redistribution"},
        ]
        repaired, count = ew.repair_legacy_bare_enrichment_links(
            content, suggestions)
        self.assertEqual(count, 2)
        self.assertIn(
            "[[wlcsp-fan-in-redistribution|fan-in WLCSP architecture]]",
            repaired,
        )
        self.assertIn("[[wlcsp-fan-in-redistribution|Cu RDL]]", repaired)


if __name__ == "__main__":
    unittest.main()
