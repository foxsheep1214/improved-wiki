"""Tests for enrich_wikilinks_retroactive — source-link backfill (audit ③)."""
import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import enrich_wikilinks_retroactive as ewr  # noqa: E402


class TestSourceSlug(unittest.TestCase):
    def test_pdf_raw_path(self):
        self.assertEqual(ewr.source_slug_from_raw("raw/Datasheet/X.pdf"), "sources/Datasheet/X")

    def test_strips_quotes(self):
        self.assertEqual(ewr.source_slug_from_raw('"raw/Book/Y.pdf"'), "sources/Book/Y")

    def test_pptx(self):
        self.assertEqual(ewr.source_slug_from_raw("raw/P/z.pptx"), "sources/P/z")


class TestBackfill(unittest.TestCase):
    def _page(self, sources_body, body):
        return f'---\ntype: concept\ntitle: X\nsources: [{sources_body}]\n---\n\n{body}'

    def test_appends_sources_section_when_missing(self):
        page = self._page('"raw/Datasheet/TI.pdf"', "# Buck\nA buck converter.")
        out, n = ewr.backfill_source_links(page)
        self.assertEqual(n, 1)
        self.assertIn("## Sources", out)
        self.assertIn("[[sources/Datasheet/TI]]", out)

    def test_no_double_add_when_already_linked(self):
        body = "# Buck\nSee [[sources/Datasheet/TI]].\n"
        page = self._page('"raw/Datasheet/TI.pdf"', body)
        out, n = ewr.backfill_source_links(page)
        self.assertEqual(n, 0)
        self.assertEqual(out, page)

    def test_already_linked_by_basename(self):
        body = "# X\nRef [[TI]].\n"
        page = self._page('"raw/Datasheet/TI.pdf"', body)
        _, n = ewr.backfill_source_links(page)
        self.assertEqual(n, 0)

    def test_multiple_sources(self):
        page = self._page('"raw/A.pdf", "raw/B.pdf"', "# X\nbody")
        out, n = ewr.backfill_source_links(page)
        self.assertEqual(n, 2)
        self.assertIn("[[sources/A]]", out)
        self.assertIn("[[sources/B]]", out)

    def test_extends_existing_sources_section(self):
        body = "# X\n\n## Sources\n\n- [[sources/already]]\n"
        page = self._page('"raw/already.pdf", "raw/new.pdf"', body)
        out, n = ewr.backfill_source_links(page)
        self.assertEqual(n, 1)
        self.assertIn("[[sources/new]]", out)
        self.assertIn("[[sources/already]]", out)

    def test_idempotent(self):
        page = self._page('"raw/A.pdf"', "# X\nbody")
        once, _ = ewr.backfill_source_links(page)
        twice, n = ewr.backfill_source_links(once)
        self.assertEqual(once, twice)
        self.assertEqual(n, 0)

    def test_no_sources_field_untouched(self):
        page = "---\ntype: concept\ntitle: X\n---\n# X\nbody"
        out, n = ewr.backfill_source_links(page)
        self.assertEqual(out, page)
        self.assertEqual(n, 0)

    def test_body_preserved(self):
        page = self._page('"raw/A.pdf"', "## Section\n\nImportant text.\n")
        out, _ = ewr.backfill_source_links(page)
        self.assertIn("## Section\n\nImportant text.\n", out)


class TestFixBrokenScoreGate(unittest.TestCase):
    """2026-07-12: --fix-broken applies only suggestions at/above the shared
    BROKEN_LINK_AUTO_REWRITE_MIN_SCORE gate; lower scores are listed for
    manual handling, never rewritten."""

    def _run(self, findings):
        import contextlib
        import io
        import tempfile
        from unittest import mock
        import _lint_suggest
        with tempfile.TemporaryDirectory() as t:
            wiki = Path(t) / "wiki"
            (wiki / "concepts").mkdir(parents=True)
            page = wiki / "concepts" / "a.md"
            page.write_text(
                "---\ntype: concept\ntitle: A\n---\n\n"
                "See [[hi-score-typo]] and [[lo-score-typo]].\n",
                encoding="utf-8")
            out = io.StringIO()
            with mock.patch.object(_lint_suggest, "run_structural_lint",
                                   return_value=findings), \
                 contextlib.redirect_stdout(out):
                fp, fl = ewr.fix_broken_links(wiki, apply=True)
            return fp, fl, page.read_text(encoding="utf-8"), out.getvalue()

    def test_above_gate_rewritten_below_gate_listed(self):
        findings = [
            {"type": "broken-link", "page": "concepts/a.md",
             "broken_target": "hi-score-typo",
             "suggested_target": "hi-score.md", "suggested_score": 0.96},
            {"type": "broken-link", "page": "concepts/a.md",
             "broken_target": "lo-score-typo",
             "suggested_target": "lo-score.md", "suggested_score": 0.82},
        ]
        fp, fl, content, printed = self._run(findings)
        self.assertEqual((fp, fl), (1, 1))
        self.assertIn("[[hi-score]]", content)          # rewritten
        self.assertIn("[[lo-score-typo]]", content)     # untouched
        self.assertIn("below the auto-rewrite gate", printed)
        self.assertIn("lo-score", printed)

    def test_missing_score_treated_as_below_gate(self):
        findings = [
            {"type": "broken-link", "page": "concepts/a.md",
             "broken_target": "hi-score-typo",
             "suggested_target": "hi-score.md"},  # no suggested_score
        ]
        fp, fl, content, printed = self._run(findings)
        self.assertEqual((fp, fl), (0, 0))
        self.assertIn("[[hi-score-typo]]", content)


if __name__ == "__main__":
    unittest.main()
