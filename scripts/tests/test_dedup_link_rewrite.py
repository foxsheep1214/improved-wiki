"""Stage 2.4 dedup close-step: inbound-reference rewrite after a merge.

Live failure (2026-07-02): dedup merged concepts (e.g. `斩波器与输出级` →
PRIMARY `开关电源推挽斩波器`) but OTHER generated pages still carried
`[[merged-away-slug]]` wikilinks and bare `related:` entries → 9+ broken
links on disk. Covers:
  - sibling body wikilinks rewritten to the primary ([[x]], [[x|alias]],
    [[concepts/x]] forms; alias text kept);
  - the PRIMARY page's own link to a merged-away slug is DE-LINKED to plain
    text (display text if present, else the bare stem) — never a self-link;
  - frontmatter `related:` bare-stem entries rewritten (quoted/prefixed),
    self-references on the primary dropped, rewritten list de-duplicated;
  - unrelated links and untouched related lines stay byte-identical.

Stdlib unittest only.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _dedup_intra_source as d  # noqa: E402

_RULES = [{
    "primary_slug": "primary-concept", "primary_title": "Primary",
    "duplicate_slugs": ["merged-away"], "merge_strategy": "union",
    "merge_reason": "test",
}]


def _apply(file_blocks):
    return dict(d._dedup_apply_merge_rules(file_blocks, _RULES))


class TestSiblingLinkRewrite(unittest.TestCase):
    def test_sibling_bare_link_rewritten_to_primary(self):
        result = _apply([
            ("concepts/primary-concept.md", "---\ntitle: Primary\n---\nbody"),
            ("concepts/merged-away.md", "---\ntitle: Dup\n---\nbody"),
            ("concepts/sibling.md", "---\ntitle: Sibling\n---\nSee [[merged-away]]."),
        ])
        self.assertNotIn("concepts/merged-away.md", result)
        self.assertIn("[[primary-concept]]", result["concepts/sibling.md"])
        self.assertNotIn("[[merged-away]]", result["concepts/sibling.md"])

    def test_sibling_alias_and_path_forms_rewritten(self):
        result = _apply([
            ("concepts/primary-concept.md", "---\ntitle: Primary\n---\nbody"),
            ("concepts/merged-away.md", "---\ntitle: Dup\n---\nbody"),
            ("concepts/sibling.md",
             "---\ntitle: Sibling\n---\n[[merged-away|斩波器]] and [[concepts/merged-away]]."),
        ])
        sibling = result["concepts/sibling.md"]
        self.assertIn("[[primary-concept|斩波器]]", sibling)
        self.assertIn("[[concepts/primary-concept]]", sibling)
        self.assertNotIn("merged-away", sibling)

    def test_unrelated_links_untouched(self):
        body = "---\ntitle: Sibling\n---\nSee [[other-page]] and [[entities/mc6840]]."
        result = _apply([
            ("concepts/primary-concept.md", "---\ntitle: Primary\n---\nbody"),
            ("concepts/merged-away.md", "---\ntitle: Dup\n---\nbody"),
            ("concepts/sibling.md", body),
        ])
        self.assertEqual(result["concepts/sibling.md"], body)


class TestPrimarySelfLinkDropped(unittest.TestCase):
    def test_primary_link_to_merged_away_delinked_to_plain_text(self):
        result = _apply([
            ("concepts/primary-concept.md",
             "---\ntitle: Primary\n---\nSee [[merged-away]] for detail."),
            ("concepts/merged-away.md", "---\ntitle: Dup\n---\nbody"),
        ])
        primary = result["concepts/primary-concept.md"]
        self.assertIn("See merged-away for detail.", primary)
        self.assertNotIn("[[primary-concept]]", primary)
        self.assertNotIn("[[merged-away]]", primary)

    def test_primary_aliased_link_keeps_alias_text(self):
        result = _apply([
            ("concepts/primary-concept.md",
             "---\ntitle: Primary\n---\nSee [[merged-away|斩波器与输出级]]."),
            ("concepts/merged-away.md", "---\ntitle: Dup\n---\nbody"),
        ])
        primary = result["concepts/primary-concept.md"]
        self.assertIn("See 斩波器与输出级.", primary)
        self.assertNotIn("[[", primary.split("---\n")[-1])


class TestRelatedListRewrite(unittest.TestCase):
    def test_sibling_related_entry_rewritten(self):
        result = _apply([
            ("concepts/primary-concept.md", "---\ntitle: Primary\n---\nbody"),
            ("concepts/merged-away.md", "---\ntitle: Dup\n---\nbody"),
            ("concepts/sibling.md",
             '---\ntitle: Sibling\nrelated: ["merged-away", other-page, "concepts/merged-away"]\n---\nbody'),
        ])
        sibling = result["concepts/sibling.md"]
        self.assertIn('"primary-concept"', sibling)
        self.assertIn('"concepts/primary-concept"', sibling)
        self.assertIn('"other-page"', sibling)
        self.assertNotIn("merged-away", sibling)

    def test_rewritten_related_deduplicated(self):
        result = _apply([
            ("concepts/primary-concept.md", "---\ntitle: Primary\n---\nbody"),
            ("concepts/merged-away.md", "---\ntitle: Dup\n---\nbody"),
            ("concepts/sibling.md",
             '---\ntitle: Sibling\nrelated: ["merged-away", "primary-concept"]\n---\nbody'),
        ])
        sibling = result["concepts/sibling.md"]
        self.assertEqual(sibling.count("primary-concept"), 1)

    def test_primary_related_self_reference_dropped(self):
        result = _apply([
            ("concepts/primary-concept.md",
             '---\ntitle: Primary\nrelated: ["merged-away", "other-page"]\n---\nbody'),
            ("concepts/merged-away.md", "---\ntitle: Dup\n---\nbody"),
        ])
        primary = result["concepts/primary-concept.md"]
        self.assertIn('related: ["other-page"]', primary)
        self.assertNotIn("merged-away", primary)

    def test_related_without_merged_entries_byte_identical(self):
        block = '---\ntitle: Sibling\nrelated: [alpha, "beta-2"]\n---\nbody'
        result = _apply([
            ("concepts/primary-concept.md", "---\ntitle: Primary\n---\nbody"),
            ("concepts/merged-away.md", "---\ntitle: Dup\n---\nbody"),
            ("concepts/sibling.md", block),
        ])
        self.assertEqual(result["concepts/sibling.md"], block)


if __name__ == "__main__":
    unittest.main()
