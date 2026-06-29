"""Tests for _lint_fixes — ported from NashSU lint-fixes.ts (v0.5.1).

Covers make_query_slug, append_wikilink, rewrite_wikilink_target, and
ensure_broken_link_stub (uses tempfile). Stdlib unittest only.

Run:  python3 scripts/tests/test_lint_fixes.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _lint_fixes as f  # noqa: E402


class TestMakeQuerySlug(unittest.TestCase):
    def test_basic_kebab(self):
        self.assertEqual(f.make_query_slug("Foo Bar Baz"), "foo-bar-baz")

    def test_keeps_cjk(self):
        self.assertEqual(f.make_query_slug("功率变换器"), "功率变换器")

    def test_nfkc_normalizes_fullwidth(self):
        self.assertEqual(f.make_query_slug("ＡＢＣ"), "abc")

    def test_strips_punctuation(self):
        self.assertEqual(f.make_query_slug("Foo: Bar! (v2)"), "foo-bar-v2")

    def test_empty_falls_back_to_query(self):
        self.assertEqual(f.make_query_slug("!!!"), "query")

    def test_truncates_to_50(self):
        self.assertLessEqual(len(f.make_query_slug("a" * 200)), 50)

    def test_strips_underscores(self):
        # NashSU /[^\p{L}\p{N}-]/gu strips '_'; Python \w would keep it.
        self.assertEqual(f.make_query_slug("foo_bar"), "foobar")

    def test_strips_leading_trailing_underscores(self):
        self.assertEqual(f.make_query_slug("_x_"), "x")

    def test_underscore_does_not_become_hyphen(self):
        self.assertEqual(f.make_query_slug("a_b_c"), "abc")


class TestAppendWikilink(unittest.TestCase):
    def test_appends_under_existing_related(self):
        content = "---\ntype: entity\n---\n\n# Foo\n\n## Related\n\n- [[bar]]\n"
        out = f.append_wikilink(content, "baz")
        self.assertIn("- [[baz]]", out)
        self.assertIn("- [[bar]]", out)

    def test_creates_related_heading_when_absent(self):
        content = "---\ntype: entity\n---\n\n# Foo\n\nbody\n"
        out = f.append_wikilink(content, "baz")
        self.assertIn("## Related\n", out)
        self.assertIn("- [[baz]]", out)

    def test_noop_when_link_present(self):
        content = "---\ntype: entity\n---\n\n## Related\n\n- [[baz]]\n"
        self.assertEqual(f.append_wikilink(content, "baz"), content)

    def test_noop_case_insensitive(self):
        content = "---\ntype: entity\n---\n\n## Related\n\n- [[Baz]]\n"
        self.assertEqual(f.append_wikilink(content, "baz"), content)


class TestRewriteWikilinkTarget(unittest.TestCase):
    def test_rewrites_plain(self):
        content = "See [[foo-barr]] for more.\n"
        out = f.rewrite_wikilink_target(content, "foo-barr", "foo-bar")
        self.assertIn("[[foo-bar]]", out)
        self.assertNotIn("[[foo-barr]]", out)

    def test_preserves_alias(self):
        content = "See [[foo-barr|the foo]] for more.\n"
        out = f.rewrite_wikilink_target(content, "foo-barr", "foo-bar")
        self.assertIn("[[foo-bar|the foo]]", out)

    def test_leaves_other_links(self):
        content = "[[a]] and [[foo-barr]] and [[b]]\n"
        out = f.rewrite_wikilink_target(content, "foo-barr", "foo-bar")
        self.assertIn("[[a]]", out)
        self.assertIn("[[b]]", out)
        self.assertIn("[[foo-bar]]", out)

    def test_case_insensitive_match(self):
        content = "[[Foo-Barr]]\n"
        out = f.rewrite_wikilink_target(content, "foo-barr", "foo-bar")
        self.assertIn("[[foo-bar]]", out)


class TestStub(unittest.TestCase):
    def test_relative_path_simple(self):
        self.assertEqual(
            f.stub_relative_path_from_broken_target("missing-thing"),
            "queries/missing-thing.md",
        )

    def test_relative_path_nested(self):
        rel = f.stub_relative_path_from_broken_target("concepts/missing-thing")
        self.assertTrue(rel.startswith("concepts/"))
        self.assertTrue(rel.endswith(".md"))

    def test_ensure_stub_creates_then_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            full, rel, created = f.ensure_broken_link_stub(td, "missing-thing")
            self.assertTrue(created)
            self.assertTrue(full.exists())
            self.assertEqual(rel, "queries/missing-thing.md")
            self.assertIn("type: query", full.read_text(encoding="utf-8"))
            _, _, created2 = f.ensure_broken_link_stub(td, "missing-thing")
            self.assertFalse(created2)

    def test_stub_title_humanized(self):
        self.assertEqual(
            f.stub_title_from_broken_target("missing-thing"),
            "missing thing",
        )

    def test_stub_uses_utc_date(self):
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as td:
            full, _, _ = f.ensure_broken_link_stub(td, "missing-thing")
            text = full.read_text(encoding="utf-8")
            utc_today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self.assertIn(f"created: {utc_today}", text)
            self.assertIn(f"updated: {utc_today}", text)


class TestNormalizeWikiRefKey(unittest.TestCase):
    def test_collapses_case_space_hyphen_underscore(self):
        for variant in ("KV Cache", "kv-cache", "kv_cache", "KV-Cache"):
            self.assertEqual(f.normalize_wiki_ref_key(variant), "kvcache")

    def test_strips_path_and_md(self):
        self.assertEqual(
            f.normalize_wiki_ref_key("wiki/concepts/kv-cache.md"), "kvcache")

    def test_backslash_path(self):
        self.assertEqual(
            f.normalize_wiki_ref_key("concepts\\kv-cache.md"), "kvcache")


class TestBuildDeletedKeys(unittest.TestCase):
    def test_includes_slug_and_title(self):
        keys = f.build_deleted_keys([("kv-cache", "KV Cache")])
        self.assertIn("kvcache", keys)

    def test_skips_empty(self):
        keys = f.build_deleted_keys([("foo", "")])
        self.assertEqual(keys, {"foo"})


class TestExtractFrontmatterTitle(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(
            f.extract_frontmatter_title("---\ntitle: KV Cache\n---\n"), "KV Cache")

    def test_quoted(self):
        self.assertEqual(
            f.extract_frontmatter_title('---\ntitle: "KV Cache"\n---\n'), "KV Cache")

    def test_missing(self):
        self.assertEqual(f.extract_frontmatter_title("---\ntype: x\n---\n"), "")


class TestCleanIndexListing(unittest.TestCase):
    def test_drops_deleted_entry_keeps_others(self):
        text = (
            "# Index\n"
            "- [[kv-cache]] caching\n"
            "- [[attention]] attn\n"
        )
        out = f.clean_index_listing(text, {"kvcache"})
        self.assertNotIn("[[kv-cache]]", out)
        self.assertIn("[[attention]]", out)

    def test_title_form_entry_matched(self):
        text = "- [[KV Cache]] caching\n- [[Attention]] attn\n"
        out = f.clean_index_listing(text, {"kvcache"})
        self.assertNotIn("KV Cache", out)
        self.assertIn("Attention", out)

    def test_no_keys_is_noop(self):
        text = "- [[kv-cache]] x\n"
        self.assertEqual(f.clean_index_listing(text, set()), text)

    def test_substring_sibling_not_wiped(self):
        # deleted slug "ai" must not take down [[OpenAI]] (Bug B).
        text = "- [[ai]] x\n- [[OpenAI]] y\n"
        out = f.clean_index_listing(text, {"ai"})
        self.assertNotIn("[[ai]]", out)
        self.assertIn("[[OpenAI]]", out)


class TestStripDeletedWikilinks(unittest.TestCase):
    def test_plain_to_text(self):
        self.assertEqual(
            f.strip_deleted_wikilinks("see [[kv-cache]] here", {"kvcache"}),
            "see kv-cache here")

    def test_alias_to_display(self):
        self.assertEqual(
            f.strip_deleted_wikilinks("see [[kv-cache|the cache]] here", {"kvcache"}),
            "see the cache here")

    def test_surviving_link_untouched(self):
        self.assertEqual(
            f.strip_deleted_wikilinks("[[attention]]", {"kvcache"}),
            "[[attention]]")

    def test_substring_sibling_not_stripped(self):
        out = f.strip_deleted_wikilinks("[[ai]] [[OpenAI]]", {"ai"})
        self.assertEqual(out, "ai [[OpenAI]]")


if __name__ == "__main__":
    unittest.main()
