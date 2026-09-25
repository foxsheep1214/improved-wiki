"""Per-source evidence ledger: Stage 2.2 claims/quotes/formulas/tables kept
after ingest, and looked up from a wiki page's `sources:`."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace


SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _evidence_ledger as ledger  # noqa: E402
import evidence_lookup  # noqa: E402


_ANALYSES = [
    {
        "_chunk_index": 0,
        "claims": [{"claim": "Reference-element CoMET keeps gain error within 0.2 dB.",
                    "evidence": "§2.7.3; Figure 2.31", "confidence": "high"}],
        "source_quotes": '§5.2: "the penalty will be measurement time rather than accuracy."',
        "formulas": [{"formula": "T = (L T_b + T_p) n", "meaning": "Total calibration time"}],
        "structured_data": [{"label": "Table 2.4: Gain accuracy",
                             "content": "| Board TX | 0.2 dB |"}],
        "concepts_found": [{"name": "CoMET"}],
        "updated_global_digest": "not evidence",
    },
    {"_chunk_index": 1, "claims": [], "source_quotes": ""},
]


def _config(root: Path) -> SimpleNamespace:
    return SimpleNamespace(wiki_root=root, wiki_dir=root / "wiki",
                           runtime_dir=root / ".llm-wiki")


class LedgerTests(unittest.TestCase):
    def test_write_keeps_only_evidence_fields_and_page_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            path = ledger.write_evidence_ledger(
                config, "raw/Book/Phased Array - 2021 - Wu.pdf", "ab" * 32,
                _ANALYSES,
                ["wiki/sources/Book/Phased Array - 2021 - Wu.md",
                 "wiki/concepts/comet-calibration.md", "wiki/index.md",
                 "wiki/log.md", "wiki/REVIEW/confirm/x.md"],
            )
            record = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(path.name, "abababababababab.json")
        self.assertEqual(record["source"], "raw/Book/Phased Array - 2021 - Wu.pdf")
        self.assertEqual(record["source_page"],
                         "wiki/sources/Book/Phased Array - 2021 - Wu.md")
        self.assertEqual(record["pages"], [
            "wiki/concepts/comet-calibration.md",
            "wiki/sources/Book/Phased Array - 2021 - Wu.md"])
        self.assertEqual(len(record["chunks"]), 1)   # empty chunk dropped
        chunk = record["chunks"][0]
        self.assertEqual(chunk["chunk_index"], 0)
        self.assertEqual(set(chunk), {"chunk_index", "claims", "source_quotes",
                                      "formulas", "structured_data"})

    def test_no_analyses_leaves_an_existing_ledger_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            first = ledger.write_evidence_ledger(
                config, "raw/a.pdf", "cd" * 32, _ANALYSES, [])
            before = first.read_text(encoding="utf-8")
            again = ledger.write_evidence_ledger(
                config, "raw/a.pdf", "cd" * 32, [], [])
            self.assertIsNone(again)
            self.assertEqual(first.read_text(encoding="utf-8"), before)


class WritePhaseOrderTests(unittest.TestCase):
    def test_ledger_is_written_before_progress_is_cleared(self):
        import inspect
        import _ingest_write

        source = inspect.getsource(_ingest_write._do_write)
        ledger_at = source.index("write_evidence_ledger(")
        self.assertLess(ledger_at, source.index("clear_progress("))
        self.assertLess(ledger_at, source.index("save_cache("))
        self.assertIn("canonical_source_path(raw_file, config)",
                      source[ledger_at:ledger_at + 200])


class LookupTests(unittest.TestCase):
    def _project(self, root: Path) -> None:
        config = _config(root)
        ledger.write_evidence_ledger(
            config, "raw/Book/Phased Array - 2021 - Wu.pdf", "ef" * 32,
            _ANALYSES, [])
        page = root / "wiki" / "concepts" / "comet-calibration.md"
        page.parent.mkdir(parents=True)
        page.write_text(
            '---\ntype: concept\ntitle: CoMET\nsources: '
            '["raw/Book/Phased Array - 2021 - Wu.pdf", "raw/Book/Other.pdf"]\n'
            '---\n# CoMET\n', encoding="utf-8")

    def test_page_lookup_filters_by_keyword_and_reports_uncovered_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._project(root)
            out = io.StringIO()
            with redirect_stdout(out):
                code = evidence_lookup.main([
                    "--project", str(root),
                    "--page", "concepts/comet-calibration.md",
                    "--grep", "gain", "--json"])
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["missing_sources"], ["raw/Book/Other.pdf"])
        hits = result["matches"]
        self.assertEqual({h["kind"] for h in hits}, {"claim", "structured_data"})
        claim = next(h for h in hits if h["kind"] == "claim")
        self.assertEqual(claim["evidence"], "§2.7.3; Figure 2.31")
        self.assertEqual(claim["source"], "raw/Book/Phased Array - 2021 - Wu.pdf")

    def test_source_lookup_without_grep_lists_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._project(root)
            out = io.StringIO()
            with redirect_stdout(out):
                evidence_lookup.main(["--project", str(root),
                                      "--source", "Phased Array", "--json"])
        kinds = [h["kind"] for h in json.loads(out.getvalue())["matches"]]
        self.assertEqual(sorted(kinds),
                         ["claim", "formula", "source_quotes", "structured_data"])


if __name__ == "__main__":
    unittest.main()
