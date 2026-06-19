"""Tests for _frontmatter_array — port of NashSU sources-merge.ts contract.

Covers the block+inline array parsing the skill's legacy `_frontmatter`
parser could not do. Stdlib unittest only.

Run:  python3 scripts/tests/test_frontmatter_array.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _frontmatter_array as fa  # noqa: E402


def PAGE(fm: str, body: str = "body") -> str:
    return f"---\n{fm}\n---\n\n{body}"


class TestParseFrontmatterArray(unittest.TestCase):
    def test_inline_quoted_and_bare(self):
        self.assertEqual(fa.parse_frontmatter_array(PAGE('tags: ["a", "b"]'), "tags"), ["a", "b"])
        self.assertEqual(fa.parse_frontmatter_array(PAGE("tags: [a, b]"), "tags"), ["a", "b"])

    def test_block_form(self):
        self.assertEqual(
            fa.parse_frontmatter_array(PAGE("related:\n  - old-slug\n  - kept"), "related"),
            ["old-slug", "kept"],
        )

    def test_empty_inline(self):
        self.assertEqual(fa.parse_frontmatter_array(PAGE("tags: []"), "tags"), [])

    def test_missing_field(self):
        self.assertEqual(fa.parse_frontmatter_array(PAGE("type: entity"), "tags"), [])

    def test_no_frontmatter(self):
        self.assertEqual(fa.parse_frontmatter_array("# just body", "tags"), [])

    def test_whole_word_match_only(self):
        # parse "rel" must NOT match "related: [...]"
        self.assertEqual(fa.parse_frontmatter_array(PAGE("related: [a, b]"), "rel"), [])

    def test_inline_with_commas_in_quotes(self):
        self.assertEqual(
            fa.parse_frontmatter_array(PAGE('sources: ["a, b.pdf", "c.pdf"]'), "sources"),
            ["a, b.pdf", "c.pdf"],
        )


class TestWriteFrontmatterArray(unittest.TestCase):
    def test_replaces_inline_in_place(self):
        out = fa.write_frontmatter_array(PAGE("type: entity\ntags: [a, b]"), "tags", ["x", "y"])
        self.assertEqual(fa.parse_frontmatter_array(out, "tags"), ["x", "y"])
        self.assertIn("type: entity", out)

    def test_normalizes_block_to_inline(self):
        out = fa.write_frontmatter_array(PAGE("related:\n  - a\n  - b"), "related", ["a"])
        self.assertIn('related: ["a"]', out)
        self.assertNotIn("  - a", out)

    def test_appends_when_absent(self):
        out = fa.write_frontmatter_array(PAGE("type: entity"), "tags", ["x"])
        self.assertEqual(fa.parse_frontmatter_array(out, "tags"), ["x"])
        self.assertIn("type: entity", out)

    def test_no_frontmatter_unchanged(self):
        text = "# just body"
        self.assertEqual(fa.write_frontmatter_array(text, "tags", ["x"]), text)


class TestMergeLists(unittest.TestCase):
    def test_case_insensitive_dedup_first_seen_casing(self):
        self.assertEqual(fa.merge_lists(["Doc-A.pdf"], ["doc-a.pdf", "B.pdf"]), ["Doc-A.pdf", "B.pdf"])


class TestMergeArrayFieldsIntoContent(unittest.TestCase):
    def test_unions_sources(self):
        existing = PAGE('sources: ["doc-A.pdf"]')
        new = PAGE('sources: ["doc-B.pdf"]')
        out = fa.merge_array_fields_into_content(new, existing, ["sources"])
        self.assertEqual(
            sorted(fa.parse_frontmatter_array(out, "sources")), ["doc-A.pdf", "doc-B.pdf"]
        )

    def test_noop_returns_new_verbatim(self):
        existing = PAGE('sources: ["doc-A.pdf"]')
        new = PAGE('sources: ["doc-A.pdf"]')
        self.assertIs(fa.merge_array_fields_into_content(new, existing, ["sources"]), new)

    def test_existing_field_absent_skips(self):
        existing = PAGE("type: entity")
        new = PAGE('sources: ["doc-B.pdf"]')
        self.assertIs(fa.merge_array_fields_into_content(new, existing, ["sources"]), new)

    def test_existing_none_returns_new(self):
        new = PAGE('sources: ["x"]')
        self.assertIs(fa.merge_array_fields_into_content(new, None, ["sources"]), new)

    def test_existing_no_frontmatter_returns_new(self):
        new = PAGE('sources: ["x"]')
        self.assertIs(fa.merge_array_fields_into_content(new, "no fm here", ["sources"]), new)


if __name__ == "__main__":
    unittest.main(verbosity=2)
