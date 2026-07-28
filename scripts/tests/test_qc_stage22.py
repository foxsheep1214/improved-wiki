"""Offline Stage 2.2 QC without content-count quotas."""
from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qc_stage22


def _response(
    *,
    concepts: str = "concepts_found: []\n",
    entities: str = "entities_found: []\n",
    claims: str = "claims: []\n",
    source_quotes: str = "",
) -> str:
    return (
        "chunk_index: 1\n"
        "chunk_total: 1\n"
        f"{entities}"
        f"{concepts}"
        f"{source_quotes}"
        f"{claims}"
        "updated_global_digest: |\n"
        "  book_meta: {}\n"
        "  outline: []\n"
        "  key_entities: []\n"
        "  key_concepts: []\n"
        "  key_claims: []\n"
    )


def _write(tmp: Path, body: str) -> Path:
    path = tmp / "Stage-2-2-Chunk-1-abcd1234.txt"
    path.write_text(body, encoding="utf-8")
    return path


class TestNashSUKeyItemPolicy(unittest.TestCase):
    def test_sparse_response_with_zero_candidates_passes(self):
        with tempfile.TemporaryDirectory() as d:
            ok, msg = qc_stage22.check(_write(Path(d), _response()))
        self.assertTrue(ok, msg)
        self.assertIn("0 key concepts", msg)

    def test_source_quotes_are_optional(self):
        claims = (
            "claims:\n"
            '  - claim: "A core result"\n'
            '    evidence: "§2.1"\n'
        )
        with tempfile.TemporaryDirectory() as d:
            ok, msg = qc_stage22.check(
                _write(Path(d), _response(claims=claims, source_quotes=""))
            )
        self.assertTrue(ok, msg)

    def test_missing_required_structure_fails(self):
        with tempfile.TemporaryDirectory() as d:
            ok, msg = qc_stage22.check(
                _write(Path(d), "concepts_found: []\n")
            )
        self.assertFalse(ok)
        self.assertIn("missing top-level", msg)

    def test_placeholder_candidate_fails(self):
        concepts = (
            "concepts_found:\n"
            '  - name: "Technical Content"\n'
            '    importance: "core"\n'
        )
        with tempfile.TemporaryDirectory() as d:
            ok, msg = qc_stage22.check(
                _write(Path(d), _response(concepts=concepts))
            )
        self.assertFalse(ok)
        self.assertIn("placeholder", msg)

    def test_claim_without_evidence_fails(self):
        claims = (
            "claims:\n"
            '  - claim: "An ungrounded assertion"\n'
        )
        with tempfile.TemporaryDirectory() as d:
            ok, msg = qc_stage22.check(
                _write(Path(d), _response(claims=claims))
            )
        self.assertFalse(ok)
        self.assertIn("evidence", msg)

    def test_empty_evidence_value_fails(self):
        claims = (
            "claims:\n"
            '  - claim: "An ungrounded assertion"\n'
            '    evidence: ""\n'
        )
        with tempfile.TemporaryDirectory() as d:
            ok, msg = qc_stage22.check(
                _write(Path(d), _response(claims=claims))
            )
        self.assertFalse(ok)
        self.assertIn("evidence", msg)


class TestChunkNumTolerance(unittest.TestCase):
    def test_numeric_name(self):
        self.assertEqual(
            qc_stage22._chunk_num(Path("Stage-2-2-Chunk-7.txt")), 7
        )

    def test_non_numeric_name_returns_none(self):
        self.assertIsNone(
            qc_stage22._chunk_num(Path("Stage-2-2-Chunk-copy.txt"))
        )


class TestSingleFileCli(unittest.TestCase):
    def test_file_scope_checks_only_requested_response(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            good = _write(tmp, _response())
            (tmp / "Stage-2-2-Chunk-2-stale.txt").write_text(
                "concepts_found: []\n", encoding="utf-8"
            )
            out = StringIO()
            with patch.object(
                sys, "argv", ["qc_stage22.py", "--file", str(good)]
            ):
                with redirect_stdout(out):
                    rc = qc_stage22.main()
            self.assertEqual(rc, 0, out.getvalue())
            self.assertIn("✓ OK", out.getvalue())
            self.assertNotIn("stale", out.getvalue())

    def test_file_scope_rejects_missing_path(self):
        err = StringIO()
        with patch.object(
            sys,
            "argv",
            ["qc_stage22.py", "--file", "/definitely/missing/response.txt"],
        ):
            with redirect_stderr(err):
                rc = qc_stage22.main()
        self.assertEqual(rc, 2)
        self.assertIn("not found", err.getvalue())


if __name__ == "__main__":
    unittest.main()
