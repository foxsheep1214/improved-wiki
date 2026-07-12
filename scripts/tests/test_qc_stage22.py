"""qc_stage22.check() — offline QC for Stage 2.2 chunk-analysis responses.

Covers the two checks migrated from the removed C1 hard gate (2026-07-08,
d28ae85): source_quotes present + non-empty, and every claim carrying a
non-empty evidence anchor. Advisory scanner — flags for re-dispatch, never
aborts the pipeline.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qc_stage22


PADDING = "# padding line to satisfy the MIN_BYTES size check\n" * 80

CONCEPTS = "\n".join(
    f'  - name: "real-concept-{i}"\n    definition: "a genuine definition"'
    for i in range(6)
)

SOURCE_QUOTES = (
    "source_quotes: |\n"
    '  # §2.3.4: "The Barker code of length 13 provides optimal peak sidelobe level."\n'
    '  # 式(3.6): "Modulating waveform = exp(j*pi*tau*B*t^2)"\n'
)

CLAIMS_WITH_EVIDENCE = (
    "claims:\n"
    '  - claim: "Barker-13 peak sidelobe is -22.3 dB"\n'
    '    evidence: "§2.3.4, Table 2-1"\n'
    '  - claim: "LFM time-bandwidth product sets compression gain"\n'
    '    evidence: "式(3.6), p.88"\n'
)


def _write(tmp: Path, body: str) -> Path:
    f = tmp / "Stage-2-2-Chunk-1-abcd1234.txt"
    f.write_text(body, encoding="utf-8")
    return f


def _good_response(source_quotes=SOURCE_QUOTES, claims=CLAIMS_WITH_EVIDENCE) -> str:
    return (
        "chunk_index: 1\n"
        f"concepts_found:\n{CONCEPTS}\n"
        f"{source_quotes}"
        f"{claims}"
        f"{PADDING}"
    )


class TestExistingChecks(unittest.TestCase):
    def test_good_response_passes(self):
        with tempfile.TemporaryDirectory() as d:
            ok, msg = qc_stage22.check(_write(Path(d), _good_response()))
            self.assertTrue(ok, msg)

    def test_thin_response_fails_on_size(self):
        with tempfile.TemporaryDirectory() as d:
            ok, msg = qc_stage22.check(_write(Path(d), "concepts_found: []\n"))
            self.assertFalse(ok)
            self.assertIn("size", msg)


class TestSourceQuotesCheck(unittest.TestCase):
    def test_missing_source_quotes_fails(self):
        with tempfile.TemporaryDirectory() as d:
            ok, msg = qc_stage22.check(_write(Path(d), _good_response(source_quotes="")))
            self.assertFalse(ok)
            self.assertIn("source_quotes", msg)

    def test_empty_source_quotes_block_fails(self):
        # Field present but the block scalar has no content lines.
        empty = "source_quotes: |\nclaims_follow_immediately: true\n"
        with tempfile.TemporaryDirectory() as d:
            ok, msg = qc_stage22.check(_write(Path(d), _good_response(source_quotes=empty)))
            self.assertFalse(ok)
            self.assertIn("source_quotes", msg)


class TestEvidenceCheck(unittest.TestCase):
    def test_claim_without_evidence_fails(self):
        claims = (
            "claims:\n"
            '  - claim: "Barker-13 peak sidelobe is -22.3 dB"\n'
            '    evidence: "§2.3.4, Table 2-1"\n'
            '  - claim: "an ungrounded assertion"\n'
        )
        with tempfile.TemporaryDirectory() as d:
            ok, msg = qc_stage22.check(_write(Path(d), _good_response(claims=claims)))
            self.assertFalse(ok)
            self.assertIn("evidence", msg)

    def test_empty_evidence_value_fails(self):
        claims = (
            "claims:\n"
            '  - claim: "Barker-13 peak sidelobe is -22.3 dB"\n'
            '    evidence: ""\n'
        )
        with tempfile.TemporaryDirectory() as d:
            ok, msg = qc_stage22.check(_write(Path(d), _good_response(claims=claims)))
            self.assertFalse(ok)
            self.assertIn("evidence", msg)

    def test_no_claims_at_all_is_not_flagged_by_evidence_check(self):
        # Short chunks may legitimately carry few claims; claim-count policing
        # is not this check's job — it only enforces evidence coverage.
        with tempfile.TemporaryDirectory() as d:
            ok, msg = qc_stage22.check(_write(Path(d), _good_response(claims="claims: []\n")))
            self.assertTrue(ok, msg)



class TestChunkNumTolerance(unittest.TestCase):
    """2026-07-12: a glob-matched file without a numeric chunk index must not
    crash the sort key (re.search(...).group on None)."""

    def test_numeric_name(self):
        self.assertEqual(qc_stage22._chunk_num(Path("Stage-2-2-Chunk-7.txt")), 7)

    def test_non_numeric_name_returns_none(self):
        self.assertIsNone(qc_stage22._chunk_num(Path("Stage-2-2-Chunk-copy.txt")))


if __name__ == "__main__":
    unittest.main()
