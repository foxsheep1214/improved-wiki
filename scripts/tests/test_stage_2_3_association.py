"""Stage 2.3 association detection: don't collapse distinct concepts that merely
share connective/qualifier words.

Regression (book-2 re-ingest, Kuphaldt Vol I): "Series and parallel capacitors"
was auto-flagged ALREADY COVERED by existing "series-and-parallel-batteries"
(and "Series and parallel inductors" likewise). Title-word Jaccard counted the
connective "and" and the qualifiers "series"/"parallel", so two concepts that
differ only in the head noun (capacitors vs batteries) scored ≥0.5 and the
distinct concept was suppressed (never generated) — worse than a missed link.

Fixes: drop stopwords from the title-word set and require conservative ASCII
Jaccard for an exact update target, so a differing head noun or defining
qualifier no longer collapses a distinct page.

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

import _stage_2_3_incremental as s23  # noqa: E402


def _write_concept(wiki: Path, slug: str, title: str) -> None:
    (wiki / "concepts").mkdir(parents=True, exist_ok=True)
    (wiki / "concepts" / f"{slug}.md").write_text(
        f"---\ntype: concept\ntitle: \"{title}\"\n---\n\nbody\n", encoding="utf-8"
    )


class Stage23AssociationDetection(unittest.TestCase):
    def test_distinct_head_noun_not_collapsed(self):
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d)
            _write_concept(wiki, "series-and-parallel-batteries", "Series and Parallel Batteries")
            chunks = [{"concepts_found": [
                {"name": "Series and parallel capacitors"},
                {"name": "Series and parallel inductors"},
            ], "entities_found": []}]
            assoc = s23.stage_2_3_detect_incremental_associations(wiki, chunks)
            self.assertNotIn("Series and parallel capacitors", assoc)
            self.assertNotIn("Series and parallel inductors", assoc)

    def test_true_duplicate_still_matched(self):
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d)
            _write_concept(wiki, "thermal-resistance", "Thermal Resistance")
            chunks = [{"concepts_found": [{"name": "Thermal resistance"}],
                       "entities_found": []}]
            assoc = s23.stage_2_3_detect_incremental_associations(wiki, chunks)
            self.assertIn("Thermal resistance", assoc)
            self.assertIn(
                "concepts/thermal-resistance",
                assoc["Thermal resistance"],
            )

    def test_exact_slug_rename_still_matched(self):
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d)
            _write_concept(wiki, "ohms-law", "Ohm's Law")
            chunks = [{"concepts_found": [{"name": "ohms-law"}], "entities_found": []}]
            assoc = s23.stage_2_3_detect_incremental_associations(wiki, chunks)
            self.assertIn("ohms-law", assoc)

    def test_accent_and_apostrophe_variant_matched(self):
        # Regression (Op Amps re-ingest 2026-06-30): an existing page titled with
        # an accent + possessive apostrophe must still dedup against the plain
        # variant. Before accent/punct folding, "Thévenin's Theorem" vs
        # "Thevenin's Theorem" tokenized to disjoint head nouns (Jaccard 0.33)
        # and a duplicate page slipped through Stage 2.3.
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d)
            _write_concept(wiki, "Thevenins-Theorem", "Thévenin's Theorem")
            chunks = [{"concepts_found": [{"name": "Thevenin's Theorem"}],
                       "entities_found": []}]
            assoc = s23.stage_2_3_detect_incremental_associations(wiki, chunks)
            self.assertIn("Thevenin's Theorem", assoc)
            self.assertIn(
                "concepts/Thevenins-Theorem",
                assoc["Thevenin's Theorem"],
            )

    def test_live_transmission_line_title_collisions_not_collapsed(self):
        """Shared scaffolding words do not prove subject identity.

        Live Volume-2 failures routed three new subjects onto broader,
        narrower, or sibling existing pages.  Those same-route associations
        become destructive update targets, so all three fuzzy matches must be
        rejected.  A real exact Lange Coupler page must still win by slug.
        """
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d)
            _write_concept(
                wiki,
                "patch-transmission-line-model",
                "Patch Transmission Line Model",
            )
            _write_concept(
                wiki,
                "transmission-line-loss-parameters",
                "Transmission Line Loss Parameters",
            )
            _write_concept(wiki, "unfolded-lange-coupler", "Unfolded Lange Coupler")
            _write_concept(wiki, "lange-coupler", "Lange Coupler")
            chunks = [{"concepts_found": [
                {"name": "Transmission Line RLGC Model"},
                {"name": "Transmission Line Wave Parameters"},
                {"name": "Lange Coupler"},
            ], "entities_found": []}]

            assoc = s23.stage_2_3_detect_incremental_associations(wiki, chunks)

            self.assertNotIn("Transmission Line RLGC Model", assoc)
            self.assertNotIn("Transmission Line Wave Parameters", assoc)
            self.assertEqual(
                assoc.get("Lange Coupler"),
                ["concepts/lange-coupler"],
            )


if __name__ == "__main__":
    unittest.main()
