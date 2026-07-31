"""Tests for rebuild_index_deterministic (rebuild_index.py's core logic).

NashSU parity (llm_wiki 0.6.6 rebuild_wiki_index): a pure frontmatter-scan
index.md rebuild, no LLM call. Covers type grouping, title sort, full relative
targets, title fallback, and empty-category omission.
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


def _write_page(
    wiki_dir: Path,
    rel: str,
    title: str | None,
    body: str = "Body text.",
    page_type: str | None = None,
) -> None:
    path = wiki_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if title is not None:
        kind = page_type or {
            "sources": "source",
            "concepts": "concept",
            "entities": "entity",
            "methodology": "methodology",
        }.get(Path(rel).parts[0], "other")
        content = (
            f"---\ntype: {kind}\ntitle: \"{title}\"\n---\n\n"
            f"# {title}\n\n{body}\n"
        )
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

            self.assertTrue(out.startswith("# Wiki Index\n\n"))
            self.assertIn("## source", out)
            self.assertIn("## concept", out)
            self.assertIn("## methodology", out)
            # BTreeMap parity: type groups sort alphabetically.
            self.assertLess(out.index("## concept"), out.index("## source"))
            # Within Concepts, display title sorts alpha before zeta.
            self.assertLess(
                out.index("[[concepts/alpha|Alpha Concept]]"),
                out.index("[[concepts/zeta|Zeta Concept]]"),
            )
            self.assertIn(
                "- [[concepts/alpha|Alpha Concept]]",
                out,
            )
            self.assertIn("- [[sources/book1|Book One]]", out)
            self.assertIn(
                "- [[methodology/calibration|Calibration Method]]",
                out,
            )

    def test_omits_empty_categories(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki_dir = Path(tmp)
            _write_page(wiki_dir, "sources/only.md", "Only Source")

            out = rebuild_index_deterministic(wiki_dir)

            self.assertIn("## source", out)
            self.assertNotIn("## concept", out)
            self.assertNotIn("## entity", out)

    def test_title_fallback_to_heading_when_no_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki_dir = Path(tmp)
            _write_page(wiki_dir, "entities/thing.md", title=None)

            out = rebuild_index_deterministic(wiki_dir)

            self.assertIn(
                "- [[entities/thing|Untitled Fallback]]",
                out,
            )

    def test_empty_wiki_produces_bare_skeleton(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki_dir = Path(tmp)
            out = rebuild_index_deterministic(wiki_dir)
            self.assertEqual(out, "# Wiki Index\n\n")

    def test_full_paths_disambiguate_same_stem_across_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki_dir = Path(tmp)
            _write_page(
                wiki_dir,
                "concepts/a-vs-b.md",
                "A versus B concept",
            )
            _write_page(
                wiki_dir,
                "comparisons/a-vs-b.md",
                "A versus B comparison",
                page_type="comparison",
            )

            out = rebuild_index_deterministic(wiki_dir)

            self.assertIn(
                "[[concepts/a-vs-b|A versus B concept]]",
                out,
            )
            self.assertIn(
                "[[comparisons/a-vs-b|A versus B comparison]]",
                out,
            )


if __name__ == "__main__":
    unittest.main()
