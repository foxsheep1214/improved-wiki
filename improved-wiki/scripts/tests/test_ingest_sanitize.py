"""Tests for _ingest_sanitize — ported from NashSU ingest-sanitize.ts.

Covers the four corruption patterns: outer code fence, `frontmatter:` prefix,
missing opening fence, and wikilink-list inside frontmatter. Plus conservative
negatives: body fences / body `frontmatter:` prose must be left alone.

Run:  python3 scripts/tests/test_ingest_sanitize.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _ingest_sanitize import sanitize_ingested_file_content as sanitize  # noqa: E402


class TestStripOuterCodeFence(unittest.TestCase):
    def test_strips_yaml_wrapper(self):
        content = "```yaml\n---\ntype: entity\ntitle: Foo\n---\n\n# Body\n```"
        out = sanitize(content)
        self.assertTrue(out.startswith("---\n"))
        self.assertNotIn("```yaml", out)
        self.assertIn("# Body", out)

    def test_strips_md_and_markdown_and_bare_fences(self):
        for fence in ("```md", "```markdown", "```"):
            with self.subTest(fence=fence):
                content = f"{fence}\n---\ntype: entity\n---\n\nbody\n```"
                out = sanitize(content)
                self.assertTrue(out.startswith("---\n"))

    def test_leaves_body_fence_untouched(self):
        content = "---\ntype: entity\n---\n\n# Body\n\n```python\nx = 1\n```\n"
        self.assertEqual(sanitize(content), content)

    def test_no_closing_fence_means_no_strip(self):
        content = "```yaml\n---\ntype: entity\n---\n\nbody\n"
        self.assertEqual(sanitize(content), content)


class TestStripFrontmatterKeyPrefix(unittest.TestCase):
    def test_strips_prefix_when_followed_by_fence(self):
        content = "frontmatter:\n---\ntype: entity\n---\n\nbody\n"
        out = sanitize(content)
        self.assertTrue(out.startswith("---\n"))
        self.assertNotIn("frontmatter:", out.split("---", 1)[0])

    def test_leaves_body_prose_mention_alone(self):
        content = "---\ntype: entity\n---\n\nThe frontmatter: is mentioned here.\n"
        self.assertEqual(sanitize(content), content)


class TestAddMissingOpeningFence(unittest.TestCase):
    def test_adds_opening_fence(self):
        content = "type: entity\ntitle: Foo\n---\n\n# Body\n"
        out = sanitize(content)
        self.assertTrue(out.startswith("---\n"))
        self.assertEqual(out.count("---"), 2)

    def test_leaves_already_fenced_alone(self):
        content = "---\ntype: entity\n---\n\nbody\n"
        self.assertEqual(sanitize(content), content)

    def test_does_not_add_when_body_starts_with_heading(self):
        content = "# Title\n\nsome text\n"
        self.assertEqual(sanitize(content), content)


class TestRepairWikilinkListsInFrontmatter(unittest.TestCase):
    def test_repairs_related_wikilink_list(self):
        content = "---\ntype: entity\nrelated: [[a]], [[b]], [[c]]\n---\n\nbody\n"
        out = sanitize(content)
        self.assertIn('related: ["[[a]]", "[[b]]", "[[c]]"]', out)

    def test_leaves_body_wikilinks_alone(self):
        content = "---\ntype: entity\n---\n\nSee [[a]] and [[b]].\n"
        self.assertEqual(sanitize(content), content)

    def test_leaves_valid_inline_array_alone(self):
        content = '---\ntype: entity\nrelated: ["[[a]]", "[[b]]"]\n---\n\nbody\n'
        self.assertEqual(sanitize(content), content)


class TestIdempotent(unittest.TestCase):
    def test_clean_content_unchanged(self):
        content = '---\ntype: entity\ntitle: "Foo Bar"\nrelated: ["[[a]]", "[[b]]"]\n---\n\n# Foo\n\nbody\n'
        self.assertEqual(sanitize(content), content)

    def test_double_sanitize_stable(self):
        content = "```yaml\n---\ntype: entity\nrelated: [[a]], [[b]]\n---\n\nbody\n```"
        once = sanitize(content)
        twice = sanitize(once)
        self.assertEqual(once, twice)


if __name__ == "__main__":
    unittest.main()
