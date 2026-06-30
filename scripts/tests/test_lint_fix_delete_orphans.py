"""Tests for wiki-lint-fix.py:cascade_delete_orphans — ported from NashSU
lint-view.tsx:handleDeleteOrphan -> wiki-page-delete.ts:cascadeDeleteWikiPagesWithRefs.

Covers the cascade: file delete + index.md listing cleanup + body wikilink
stripping + related: frontmatter filtering, plus dry-run, idempotence, and the
aggregate-file delete guard. Stdlib unittest only.

Run:  python3 scripts/tests/test_lint_fix_delete_orphans.py
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# wiki-lint-fix.py has a hyphen → import by path.
_spec = importlib.util.spec_from_file_location(
    "wiki_lint_fix", _SCRIPTS_DIR / "wiki-lint-fix.py")
wlf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wlf)


def _page(type_: str, title: str, body: str = "", related=None) -> str:
    rel = related or []
    rel_inline = "[" + ", ".join(f'"{r}"' for r in rel) + "]"
    return (
        "---\n"
        f"type: {type_}\n"
        f'title: "{title}"\n'
        f"related: {rel_inline}\n"
        "---\n\n"
        f"{body}\n"
    )


class TestCascadeDeleteOrphans(unittest.TestCase):
    def _make_wiki(self) -> Path:
        td = tempfile.mkdtemp()
        wiki = Path(td) / "wiki"
        (wiki / "concepts").mkdir(parents=True)
        # The orphan to be deleted.
        (wiki / "concepts" / "kv-cache.md").write_text(
            _page("concept", "KV Cache", "Orphan page body."), encoding="utf-8")
        # A surviving page that references the orphan in body + related.
        (wiki / "concepts" / "attention.md").write_text(
            _page("concept", "Attention",
                  "See [[kv-cache]] and [[KV Cache|the cache]] and [[attention]].",
                  related=["kv-cache", "attention"]),
            encoding="utf-8")
        # index.md listing both.
        (wiki / "index.md").write_text(
            "# Index\n- [[kv-cache]] caching\n- [[attention]] attn\n",
            encoding="utf-8")
        return wiki

    def test_apply_deletes_and_cascades(self):
        wiki = self._make_wiki()
        summary = wlf.cascade_delete_orphans(
            wiki, ["concepts/kv-cache.md"], dry_run=False)
        self.assertEqual(summary["deleted"], 1)
        self.assertFalse((wiki / "concepts" / "kv-cache.md").exists())

        attn = (wiki / "concepts" / "attention.md").read_text(encoding="utf-8")
        # body wikilinks to orphan stripped, alias preserved, sibling kept.
        self.assertNotIn("[[kv-cache]]", attn)
        self.assertNotIn("[[KV Cache|the cache]]", attn)
        self.assertIn("the cache", attn)
        self.assertIn("[[attention]]", attn)
        # related: filtered.
        self.assertNotIn("kv-cache", attn.split("---")[1])  # not in frontmatter
        self.assertIn("attention", attn)

        index = (wiki / "index.md").read_text(encoding="utf-8")
        self.assertNotIn("[[kv-cache]]", index)
        self.assertIn("[[attention]]", index)

    def test_dry_run_changes_nothing(self):
        wiki = self._make_wiki()
        summary = wlf.cascade_delete_orphans(
            wiki, ["concepts/kv-cache.md"], dry_run=True)
        self.assertEqual(summary["deleted"], 1)
        # nothing actually removed / rewritten.
        self.assertTrue((wiki / "concepts" / "kv-cache.md").exists())
        index = (wiki / "index.md").read_text(encoding="utf-8")
        self.assertIn("[[kv-cache]]", index)

    def test_idempotent_second_run(self):
        wiki = self._make_wiki()
        wlf.cascade_delete_orphans(wiki, ["concepts/kv-cache.md"], dry_run=False)
        # second run: target already gone, no surviving refs.
        summary2 = wlf.cascade_delete_orphans(
            wiki, ["concepts/kv-cache.md"], dry_run=False)
        self.assertEqual(summary2["deleted"], 0)
        self.assertEqual(summary2["missing"], 1)
        self.assertEqual(summary2["rewritten"], 0)

    def test_aggregate_file_never_deleted(self):
        wiki = self._make_wiki()
        summary = wlf.cascade_delete_orphans(
            wiki, ["index.md"], dry_run=False)
        self.assertEqual(summary["deleted"], 0)
        self.assertEqual(summary["skipped"], 1)
        self.assertTrue((wiki / "index.md").exists())

    def test_title_form_related_filtered(self):
        wiki = self._make_wiki()
        # rewrite attention to reference orphan by TITLE form in related.
        (wiki / "concepts" / "attention.md").write_text(
            _page("concept", "Attention", "body",
                  related=["KV Cache"]),
            encoding="utf-8")
        wlf.cascade_delete_orphans(wiki, ["concepts/kv-cache.md"], dry_run=False)
        fm = (wiki / "concepts" / "attention.md").read_text(
            encoding="utf-8").split("---")[1]
        self.assertNotIn("KV Cache", fm)


if __name__ == "__main__":
    unittest.main()
