"""Tests for the _lint_suggest wiring in validate_ingest.

Exercises validate_ingest.collect_structural_lint_findings over a tmp wiki,
verifying broken-link suggestions, orphan suggestions, and anchor exclusion.
Does NOT call validate_ingest.main() (that needs a real cache); the function
under test takes wiki_dir explicitly and uses no module globals.

Run:  python3 scripts/tests/test_validate_lint_suggest.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import validate_ingest as vi  # noqa: E402


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class TestCollectStructuralLintFindings(unittest.TestCase):
    def test_broken_link_with_close_typo_gets_suggestion(self):
        with tempfile.TemporaryDirectory() as t:
            wiki = Path(t) / "wiki"
            _write(wiki / "concepts/transformer.md",
                   "---\ntitle: Transformer\n---\n# Transformer\nAttention model.")
            _write(wiki / "concepts/attention.md",
                   "# Attention\nSee [[transfomer]] for the architecture.")
            findings = vi.collect_structural_lint_findings(wiki)
            broken = next(f for f in findings if f["type"] == "broken-link")
            self.assertEqual(broken["broken_target"], "transfomer")
            self.assertEqual(broken["suggested_target"], "concepts/transformer.md")

    def test_orphan_gets_suggested_source(self):
        with tempfile.TemporaryDirectory() as t:
            wiki = Path(t) / "wiki"
            _write(wiki / "concepts/rag.md",
                   "# RAG\nRetrieval augmented generation uses vector search.")
            _write(wiki / "concepts/vector-search.md",
                   "# Vector Search\nVector search retrieval finds related chunks.")
            findings = vi.collect_structural_lint_findings(wiki)
            orphan = next(f for f in findings if f["type"] == "orphan"
                          and f["page"] == "concepts/rag.md")
            self.assertEqual(orphan["suggested_source"], "concepts/vector-search.md")

    def test_excludes_anchors(self):
        with tempfile.TemporaryDirectory() as t:
            wiki = Path(t) / "wiki"
            _write(wiki / "index.md", "See [[nothing-here]].")
            _write(wiki / "log.md", "See [[also-nothing]].")
            findings = vi.collect_structural_lint_findings(wiki)
            for f in findings:
                self.assertNotIn(f["page"], ("index.md", "log.md"))

    def test_empty_or_missing_wiki_returns_empty(self):
        self.assertEqual(vi.collect_structural_lint_findings(Path("/nonexistent/wiki")), [])
        with tempfile.TemporaryDirectory() as t:
            wiki = Path(t) / "wiki"
            wiki.mkdir()
            self.assertEqual(vi.collect_structural_lint_findings(wiki), [])

    def test_no_suggestion_for_unrelated_typo(self):
        with tempfile.TemporaryDirectory() as t:
            wiki = Path(t) / "wiki"
            _write(wiki / "concepts/bat.md", "# Bat\nFlying mammal.")
            _write(wiki / "concepts/note.md", "# Note\nSee [[cat]].")
            findings = vi.collect_structural_lint_findings(wiki)
            broken = next(f for f in findings if f["type"] == "broken-link")
            self.assertEqual(broken["broken_target"], "cat")
            self.assertIsNone(broken["suggested_target"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
