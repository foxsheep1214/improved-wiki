"""Tests for _frontmatter parse/write/merge hardening.

Covers the MEDIUM gaps vs NashSU frontmatter.ts + sources-merge.ts:
  - parse_frontmatter: read-time ```yaml wrapper fallback + line-anchored fence.
  - write_frontmatter: YAML-safe quoting (values with ``:`` / leading ``[`` / list items).
  - merge_array_fields_into_content: block-style arrays from existing survive.

Run:  python3 scripts/tests/test_frontmatter.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _frontmatter import (  # noqa: E402
    parse_frontmatter,
    write_frontmatter,
    merge_array_fields_into_content,
)


class TestParseFrontmatterFallback(unittest.TestCase):
    def test_parses_normal(self):
        fm, body = parse_frontmatter("---\ntype: entity\ntitle: Foo\n---\n\nbody\n")
        self.assertEqual(fm["type"], "entity")
        self.assertIn("body", body)

    def test_strips_yaml_wrapper_read_time(self):
        content = "```yaml\n---\ntype: entity\ntitle: Foo\n---\n\nbody\n```"
        fm, body = parse_frontmatter(content)
        self.assertEqual(fm.get("type"), "entity")
        self.assertEqual(fm.get("title"), "Foo")
        self.assertIn("body", body)
        self.assertNotIn("```", body)

    def test_no_frontmatter_returns_empty(self):
        fm, body = parse_frontmatter("# Just a heading\n\nbody\n")
        self.assertEqual(fm, {})
        self.assertIn("body", body)


class TestWriteFrontmatterQuoting(unittest.TestCase):
    def test_scalar_with_colon_quoted(self):
        out = write_frontmatter({"title": "Foo: Bar"}, "body")
        self.assertIn('title: "Foo: Bar"', out)

    def test_simple_scalar_bare(self):
        out = write_frontmatter({"type": "entity", "created": "2026-06-24"}, "body")
        self.assertIn("type: entity", out)
        self.assertIn("created: 2026-06-24", out)

    def test_list_items_quoted(self):
        out = write_frontmatter({"related": ["[[a]]", "[[b]]"]}, "body")
        self.assertNotIn("[[[", out)
        self.assertIn('"[[a]]"', out)
        self.assertIn('"[[b]]"', out)

    def test_list_with_spaces_quoted(self):
        out = write_frontmatter({"tags": ["foo bar", "baz"]}, "body")
        self.assertIn('"foo bar"', out)

    def test_scalar_starting_with_bracket_quoted(self):
        out = write_frontmatter({"note": "[reserved]"}, "body")
        self.assertIn('"[reserved]"', out)


class TestMergeArrayFieldsBlockStyle(unittest.TestCase):
    def test_block_style_existing_array_preserved(self):
        existing = (
            "---\ntype: entity\nrelated:\n  - alpha\n  - beta\n---\n\nold body\n"
        )
        new = "---\ntype: entity\nrelated: [gamma]\n---\n\nnew body\n"
        out = merge_array_fields_into_content(new, existing)
        self.assertIn("alpha", out)
        self.assertIn("beta", out)
        self.assertIn("gamma", out)


if __name__ == "__main__":
    unittest.main()
