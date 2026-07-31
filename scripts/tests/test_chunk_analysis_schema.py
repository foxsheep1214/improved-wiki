"""Strict Stage 2.2 chunk-analysis schema regressions."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _stage_2_analyze import (  # noqa: E402
    ChunkAnalysisValidationError,
    normalize_and_validate_chunk_analysis,
)


def _valid() -> dict:
    return {
        "chunk_index": 1,
        "chunk_total": 2,
        "entities_found": [{
            "name": "A. E. Fitzgerald",
            "significance": "Author of the source.",
        }],
        "concepts_found": [{
            "name": "Magnetic Circuit Analysis",
            "importance": "Core",
            "definition": "Analysis using magnetic reluctance and flux.",
            "key_details": ["Relates magnetomotive force, flux, and reluctance."],
        }],
        "source_quotes": '§1.2: "Flux follows the magnetic circuit."',
        "claims": [{
            "claim": "Reluctance opposes magnetic flux.",
            "evidence": "§1.2",
            "confidence": "High",
        }],
        "formulas": [{
            "formula": r"\mathcal{F}=\Phi\mathcal{R}",
            "meaning": "Magnetomotive force equals flux times reluctance.",
        }],
        "connections_to_existing_wiki": [],
        "schema_typed_candidates": [],
        "updated_global_digest": {
            "book_meta": {"title": "Electric Machinery"},
            "outline": ["Chapter 1"],
            "key_entities": [],
            "key_concepts": [{"name": "Magnetic Circuit Analysis"}],
            "key_claims": [],
        },
    }


class TestChunkAnalysisSchema(unittest.TestCase):
    def test_valid_analysis_is_normalized(self):
        got = normalize_and_validate_chunk_analysis(
            _valid(), expected_index=1, expected_total=2)
        self.assertEqual(got["concepts_found"][0]["importance"], "core")
        self.assertEqual(got["claims"][0]["confidence"], "high")

    def test_fitzgerald_stringified_name_item_is_rejected(self):
        bad = _valid()
        bad["concepts_found"] = ['name: "Magnetic Circuit Analysis"']
        with self.assertRaisesRegex(
                ChunkAnalysisValidationError, "items must be mappings"):
            normalize_and_validate_chunk_analysis(bad)

    def test_missing_concept_contract_is_rejected(self):
        bad = _valid()
        del bad["concepts_found"][0]["importance"]
        with self.assertRaisesRegex(
                ChunkAnalysisValidationError, "importance"):
            normalize_and_validate_chunk_analysis(bad)

    def test_claim_without_evidence_is_rejected(self):
        bad = _valid()
        bad["claims"][0]["evidence"] = ""
        with self.assertRaisesRegex(
                ChunkAnalysisValidationError, "evidence"):
            normalize_and_validate_chunk_analysis(bad)

    def test_wrong_chunk_identity_is_rejected(self):
        with self.assertRaisesRegex(
                ChunkAnalysisValidationError, "chunk_total=2, expected 8"):
            normalize_and_validate_chunk_analysis(
                _valid(), expected_index=1, expected_total=8)


if __name__ == "__main__":
    unittest.main()
