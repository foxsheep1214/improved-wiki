"""Tests for rebuild_index_deterministic (rebuild_index.py's core logic).

NashSU parity (llm_wiki 0.6.4 rebuild_wiki_index): a pure frontmatter-scan
index.md rebuild, no LLM call. Covers section grouping, alphabetical sort,
title fallback, and empty-category omission — the same behaviors
_scan_wiki_inventory already guarantees, exercised through the public
rebuild function.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_script_dir = Path(__file__).resolve().parent.parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))
from _stage_3_write import rebuild_index_deterministic  # noqa: E402


def _write_page(wiki_dir: Path, rel: str, title: str | None, body: str = "Body text.") -> None:
    path = wiki_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if title is not None:
        content = f'---\ntitle: "{title}"\n---\n\n# {title}\n\n{body}\n'
    else:
        content = f"# Untitled Fallback\n\n{body}\n"
    path.write_text(content, encoding="utf-8")


class TestRebuildIndexDeterministic(unittest.TestCase):
    def test_groups_by_section_sorted_alphabetically(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki_dir = Path(tmp)
            _write_page(wiki_dir, "concepts/zeta.md", "Zeta Concept")
            _write_page(wiki_dir, "concepts/alpha.md", "Alpha Concept")
            _write_page(wiki_dir, "sources/book1.md", "Book One")
            _write_page(
                wiki_dir,
                "methodology/calibration.md",
                "Calibration Method",
            )

            out = rebuild_index_deterministic(wiki_dir)

            self.assertTrue(out.startswith("# Index\n\n"))
            self.assertIn("## Sources（来源）", out)
            self.assertIn("## Concepts（概念）", out)
            self.assertIn("## Methodology（方法论）", out)
            # Sources section must precede Concepts (declared _INDEX_CATEGORIES order).
            self.assertLess(out.index("## Sources"), out.index("## Concepts"))
            # Within Concepts, alpha sorts before zeta.
            self.assertLess(out.index("[[alpha]]"), out.index("[[zeta]]"))
            self.assertIn("- [[alpha]] — Alpha Concept", out)
            self.assertIn("- [[book1]] — Book One", out)
            self.assertIn("- [[calibration]] — Calibration Method", out)

    def test_omits_empty_categories(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki_dir = Path(tmp)
            _write_page(wiki_dir, "sources/only.md", "Only Source")

            out = rebuild_index_deterministic(wiki_dir)

            self.assertIn("## Sources（来源）", out)
            self.assertNotIn("## Concepts", out)
            self.assertNotIn("## Entities", out)

    def test_title_fallback_to_heading_when_no_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki_dir = Path(tmp)
            _write_page(wiki_dir, "entities/thing.md", title=None)

            out = rebuild_index_deterministic(wiki_dir)

            self.assertIn("- [[thing]] — Untitled Fallback", out)

    def test_empty_wiki_produces_bare_skeleton(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki_dir = Path(tmp)
            out = rebuild_index_deterministic(wiki_dir)
            self.assertEqual(out, "# Index\n")


if __name__ == "__main__":
    unittest.main()
