"""Stage 2.3 association detection: don't collapse distinct concepts that merely
share connective/qualifier words.

Regression (book-2 re-ingest, Kuphaldt Vol I): "Series and parallel capacitors"
was auto-flagged ALREADY COVERED by existing "series-and-parallel-batteries"
(and "Series and parallel inductors" likewise). Title-word Jaccard counted the
connective "and" and the qualifiers "series"/"parallel", so two concepts that
differ only in the head noun (capacitors vs batteries) scored ≥0.5 and the
distinct concept was suppressed (never generated) — worse than a missed link.

Fix: drop stopwords from the title-word set and require Jaccard > 0.5 (strictly
more shared than not), so a single differing head noun no longer collapses.

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
            self.assertIn("thermal-resistance", assoc["Thermal resistance"])

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
            self.assertIn("Thevenins-Theorem", assoc["Thevenin's Theorem"])


if __name__ == "__main__":
    unittest.main()
