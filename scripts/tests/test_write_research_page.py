"""Deterministic Deep Research write-path parity with NashSU v0.6.7."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import write_research_page as writer  # noqa: E402


class TestResearchFilename(unittest.TestCase):
    def test_unicode_nfkc_slug_rules(self):
        self.assertEqual(
            writer.make_query_slug("  ＧａＮ 驱动_设计 / 2026! 🚀 "),
            "gan-驱动设计-2026",
        )
        self.assertEqual(writer.make_query_slug("🚀?!"), "query")
        self.assertEqual(writer.make_query_slug("x" * 60), "x" * 50)

    def test_filename_is_utc_but_created_is_local_calendar_date(self):
        filename, created = writer.make_deep_research_filename(
            "雷达",
            datetime(2026, 8, 3, 16, 30, tzinfo=timezone.utc),
            local_timezone=timezone(timedelta(hours=8)),
        )
        self.assertEqual(filename, "research-雷达-2026-08-03-163000.md")
        self.assertEqual(created, "2026-08-04")


class TestResearchSources(unittest.TestCase):
    def test_url_then_fallback_dedup_is_case_insensitive_and_capped(self):
        sources = [
            {"title": "A", "url": "HTTPS://EXAMPLE.COM/A", "snippet": "one", "source": "web"},
            {"title": "duplicate URL", "url": "https://example.com/a", "snippet": "two", "source": "other"},
            {"title": "Local", "url": "", "snippet": "same", "source": "AnyTXT"},
            {"title": "LOCAL", "url": "", "snippet": "SAME", "source": "anytxt"},
            *[
                {"title": f"S{i}", "url": f"https://example.com/{i}", "snippet": "x", "source": "web"}
                for i in range(30)
            ],
        ]
        normalized = writer.normalize_research_sources({"results": sources})
        self.assertEqual(len(normalized), writer.MAX_RESEARCH_SOURCES)
        self.assertEqual([item["title"] for item in normalized[:3]], ["A", "Local", "S0"])


class TestResearchPage(unittest.TestCase):
    def test_exact_envelope_think_cleanup_and_code_generated_references(self):
        page = writer.build_research_page(
            'GaN "driver"',
            "<think>secret</think>\n## Findings\nEvidence [1].\n<thinking>unfinished",
            [{
                "title": "Source One",
                "url": "https://example.com/one",
                "snippet": "not copied into references",
                "source": "example.com",
            }],
            "2026-08-03",
        )
        self.assertTrue(page.startswith(
            "---\n"
            "type: query\n"
            'title: "Research: GaN \\"driver\\""\n'
            "created: 2026-08-03\n"
            "origin: deep-research\n"
            "tags: [research]\n"
            "---\n\n"
            '# Research: GaN "driver"\n\n'
        ))
        self.assertIn("## Findings\nEvidence [1].", page)
        self.assertNotIn("secret", page)
        self.assertNotIn("thinking", page.lower())
        self.assertTrue(page.endswith(
            "## References\n\n"
            "1. [Source One](https://example.com/one) — example.com\n"
        ))


class TestWriterCli(unittest.TestCase):
    def test_writes_one_query_page_and_does_not_auto_ingest(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            (root / "wiki").mkdir()
            synthesis_file = root / "synthesis.txt"
            sources_file = root / "sources.json"
            synthesis_file.write_text("## Result\nGrounded [1].", encoding="utf-8")
            sources_file.write_text(json.dumps([{
                "title": "One",
                "url": "https://example.com/one",
                "snippet": "grounding snippet",
                "source": "example.com",
            }]), encoding="utf-8")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                status = writer.main([
                    "--project", str(root),
                    "--topic", "Test Topic",
                    "--synthesis-file", str(synthesis_file),
                    "--sources-file", str(sources_file),
                    "--now", "2026-08-03T01:02:03Z",
                ])

            self.assertEqual(status, 0)
            rel = stdout.getvalue().strip()
            self.assertEqual(rel, "wiki/queries/research-test-topic-2026-08-03-010203.md")
            self.assertTrue((root / rel).is_file())
            wiki_markdown = sorted((root / "wiki").rglob("*.md"))
            self.assertEqual(wiki_markdown, [root / rel])
            self.assertFalse((root / ".llm-wiki").exists())

    def test_zero_sources_writes_nothing(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            (root / "wiki").mkdir()
            synthesis_file = root / "synthesis.txt"
            sources_file = root / "sources.json"
            synthesis_file.write_text("unused", encoding="utf-8")
            sources_file.write_text("[]", encoding="utf-8")

            with redirect_stderr(io.StringIO()):
                status = writer.main([
                    "--project", str(root),
                    "--topic", "None",
                    "--synthesis-file", str(synthesis_file),
                    "--sources-file", str(sources_file),
                ])
            self.assertEqual(status, 3)
            self.assertFalse((root / "wiki" / "queries").exists())


if __name__ == "__main__":
    unittest.main()
