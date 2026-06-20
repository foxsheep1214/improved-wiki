"""Tests for normalize_page_types — type/role normalization (audit point ②)."""
import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import normalize_page_types as npt  # noqa: E402


def _page(fm_type: str, extra: str = "", body: str = "# X\n") -> str:
    return f"---\ntype: {fm_type}\n{extra}---\n\n{body}"


class TestNormalizeFrontmatter(unittest.TestCase):
    def test_role_as_type_becomes_entity_plus_role(self):
        out, changes = npt.normalize_frontmatter(_page("person"))
        self.assertIn("type: entity", out)
        self.assertIn("role: person", out)
        self.assertNotIn("type: person", out)
        self.assertEqual(len(changes), 1)

    def test_each_role_type_migrated(self):
        for role in npt.ROLE_TYPES:
            out, changes = npt.normalize_frontmatter(_page(role))
            self.assertIn("type: entity", out)
            self.assertIn(f"role: {role}", out)
            self.assertTrue(changes)

    def test_entities_typo_fixed(self):
        out, changes = npt.normalize_frontmatter(_page("entities"))
        self.assertIn("type: entity", out)
        self.assertNotIn("type: entities", out)

    def test_canonical_type_untouched(self):
        for ty in ("concept", "entity", "source", "query", "comparison"):
            out, changes = npt.normalize_frontmatter(_page(ty))
            self.assertEqual(out, _page(ty))
            self.assertEqual(changes, [])

    def test_existing_role_not_clobbered(self):
        page = "---\ntype: person\nrole: engineer\ntitle: X\n---\n# X\n"
        out, changes = npt.normalize_frontmatter(page)
        self.assertIn("type: entity", out)
        self.assertIn("role: engineer", out)
        self.assertNotIn("role: person", out)

    def test_idempotent(self):
        page = _page("organization")
        once, _ = npt.normalize_frontmatter(page)
        twice, changes = npt.normalize_frontmatter(once)
        self.assertEqual(once, twice)
        self.assertEqual(changes, [])

    def test_no_frontmatter_untouched(self):
        page = "# No frontmatter here\njust body"
        out, changes = npt.normalize_frontmatter(page)
        self.assertEqual(out, page)
        self.assertEqual(changes, [])

    def test_body_preserved(self):
        page = "---\ntype: system\n---\n\n## Section\n\nDetailed body text.\n"
        out, _ = npt.normalize_frontmatter(page)
        self.assertIn("## Section\n\nDetailed body text.\n", out)


if __name__ == "__main__":
    unittest.main()
