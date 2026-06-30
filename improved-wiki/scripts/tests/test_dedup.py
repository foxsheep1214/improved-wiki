"""Tests for _dedup — ported from NashSU dedup.test.ts.

Covers the three stages: extract_entity_summary, parse_detector_response,
detect_duplicate_groups (mock LLM), rewrite_cross_references, rewrite_index_md,
merge_duplicate_group (mock LLM). Stdlib unittest only.

Run:  python3 scripts/tests/test_dedup.py
"""
from __future__ import annotations

import re as _re
import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _dedup as d  # noqa: E402
from _frontmatter_array import parse_frontmatter_array  # noqa: E402


def PAGE(fm: str, body: str = "body") -> str:
    return f"---\n{fm}\n---\n\n{body}"


# ── Stage 1: extract_entity_summary ─────────────────────────────────────────

class TestExtractEntitySummary(unittest.TestCase):
    def test_returns_none_for_no_frontmatter(self):
        self.assertIsNone(d.extract_entity_summary("wiki/entities/foo.md", "# Just body"))

    def test_derives_slug_from_filename(self):
        s = d.extract_entity_summary(
            "wiki/entities/accumulibacter.md",
            PAGE("type: entity\ntitle: Accumulibacter", "body"),
        )
        self.assertEqual(s.slug, "accumulibacter")

    def test_uses_frontmatter_description(self):
        s = d.extract_entity_summary(
            "wiki/entities/foo.md",
            PAGE('type: entity\ntitle: Foo\ndescription: "A short summary."\ntags: [a, b]', "Body text"),
        )
        self.assertEqual(s.description, "A short summary.")
        self.assertEqual(s.tags, ["a", "b"])

    def test_falls_back_to_first_body_paragraph(self):
        s = d.extract_entity_summary(
            "wiki/entities/foo.md",
            PAGE("type: entity\ntitle: Foo", "# Foo\n\nFirst real paragraph here."),
        )
        self.assertEqual(s.description, "First real paragraph here.")

    def test_truncates_long_descriptions(self):
        long = "x" * 400
        s = d.extract_entity_summary(
            "wiki/entities/foo.md",
            PAGE(f'type: entity\ntitle: Foo\ndescription: "{long}"', "body"),
        )
        self.assertLessEqual(len(s.description), 200)
        self.assertTrue(s.description.endswith("…"))


# ── Stage 2: parse_detector_response ────────────────────────────────────────

class TestParseDetectorResponse(unittest.TestCase):
    def test_clean_json(self):
        raw = '{"groups": [{"slugs": ["a", "b"], "reason": "same thing", "confidence": "high"}]}'
        self.assertEqual(d.parse_detector_response(raw), [
            {"slugs": ["a", "b"], "reason": "same thing", "confidence": "high"},
        ])

    def test_strips_code_fences(self):
        raw = '```json\n{"groups": [{"slugs": ["a","b"], "reason": "x", "confidence": "high"}]}\n```'
        out = d.parse_detector_response(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["slugs"], ["a", "b"])

    def test_strips_preamble(self):
        raw = 'Sure, here are the duplicates:\n\n{"groups": [{"slugs": ["foo","bar"], "reason": "synonyms", "confidence": "medium"}]}\n\nLet me know.'
        out = d.parse_detector_response(raw)
        self.assertEqual(out[0]["slugs"], ["foo", "bar"])
        self.assertEqual(out[0]["confidence"], "medium")

    def test_rejects_groups_under_two_slugs(self):
        raw = '{"groups": [{"slugs": ["only-one"], "reason": "x", "confidence": "high"}]}'
        self.assertEqual(d.parse_detector_response(raw), [])

    def test_defaults_invalid_confidence_to_low(self):
        raw = '{"groups": [{"slugs": ["a","b"], "reason": "", "confidence": "extremely-high"}]}'
        self.assertEqual(d.parse_detector_response(raw)[0]["confidence"], "low")

    def test_returns_empty_for_malformed(self):
        self.assertEqual(d.parse_detector_response("not json at all"), [])
        self.assertEqual(d.parse_detector_response('{"groups": [unclosed'), [])
        self.assertEqual(d.parse_detector_response(""), [])

    def test_returns_empty_when_no_groups_field(self):
        self.assertEqual(d.parse_detector_response('{"other_field": []}'), [])

    def test_survives_braces_in_reason_strings(self):
        raw = '{"groups": [{"slugs": ["a","b"], "reason": "Same thing { really }", "confidence": "high"}]}'
        self.assertEqual(d.parse_detector_response(raw)[0]["reason"], "Same thing { really }")


# ── Stage 2: detect_duplicate_groups (mock LLM) ────────────────────────────

def summary(slug: str, title: str, type: str = "entity") -> d.EntitySummary:
    return d.EntitySummary(slug=slug, path=f"wiki/entities/{slug}.md", type=type, title=title, tags=[])


class TestDetectDuplicateGroups(unittest.TestCase):
    def test_returns_empty_for_fewer_than_two_summaries(self):
        calls = []
        def llm(sys_p, user):
            calls.append(user)
            return '{"groups":[]}'
        self.assertEqual(d.detect_duplicate_groups([summary("foo", "Foo")], llm), [])
        self.assertEqual(calls, [])

    def test_filters_groups_not_in_input(self):
        def llm(sys_p, user):
            return '{"groups": [{"slugs": ["real-a","real-b"], "reason": "x", "confidence": "high"}, {"slugs": ["fake","real-a"], "reason": "x", "confidence": "high"}]}'
        result = d.detect_duplicate_groups([summary("real-a", "A"), summary("real-b", "B")], llm)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["slugs"], ["real-a", "real-b"])

    def test_filters_not_duplicates_whitelist(self):
        def llm(sys_p, user):
            return '{"groups": [{"slugs": ["foo","bar"], "reason": "x", "confidence": "high"}, {"slugs": ["x","y"], "reason": "x", "confidence": "high"}]}'
        result = d.detect_duplicate_groups(
            [summary("foo", "Foo"), summary("bar", "Bar"), summary("x", "X"), summary("y", "Y")],
            llm, not_duplicates=[["foo", "bar"]],
        )
        self.assertEqual([sorted(g["slugs"]) for g in result], [["x", "y"]])

    def test_whitelist_case_insensitive_order_independent(self):
        def llm(sys_p, user):
            return '{"groups": [{"slugs": ["foo","bar"], "reason": "", "confidence": "high"}]}'
        result = d.detect_duplicate_groups(
            [summary("foo", "Foo"), summary("bar", "Bar")],
            llm, not_duplicates=[["BAR", "FOO"]],
        )
        self.assertEqual(result, [])

    def test_user_message_format(self):
        captured = {}
        def llm(sys_p, user):
            captured["user"] = user
            return '{"groups":[]}'
        d.detect_duplicate_groups([
            d.EntitySummary("foo", "wiki/entities/foo.md", "entity", "Foo",
                            tags=["t1", "t2"], description="Short desc."),
            summary("bar", "Bar"),
        ], llm)
        self.assertIn("type=entity", captured["user"])
        self.assertIn("slug=foo", captured["user"])
        self.assertIn('"Foo"', captured["user"])
        self.assertIn("[t1, t2]", captured["user"])
        self.assertIn("Short desc.", captured["user"])


# ── Stage 3: rewrite_cross_references ──────────────────────────────────────

class TestRewriteCrossReferences(unittest.TestCase):
    def test_rewrites_bare_wikilinks(self):
        out = d.rewrite_cross_references("See [[old-slug]] for context.", {"old-slug": "new-slug"})
        self.assertEqual(out, "See [[new-slug]] for context.")

    def test_preserves_alias(self):
        out = d.rewrite_cross_references("See [[old-slug|the old display]] here.", {"old-slug": "new-slug"})
        self.assertEqual(out, "See [[new-slug|the old display]] here.")

    def test_does_not_touch_unrelated_slugs(self):
        out = d.rewrite_cross_references("Both [[paos]] and [[unrelated]] are mentioned.", {"paos": "phosphorus-accumulating-organisms"})
        self.assertIn("[[phosphorus-accumulating-organisms]]", out)
        self.assertIn("[[unrelated]]", out)

    def test_rewrites_related_inline(self):
        out = d.rewrite_cross_references(
            PAGE("type: entity\ntitle: Foo\nrelated: [old-slug, kept]"), {"old-slug": "new-slug"}
        )
        self.assertEqual(parse_frontmatter_array(out, "related"), ["new-slug", "kept"])

    def test_rewrites_related_block(self):
        out = d.rewrite_cross_references(
            PAGE("type: entity\ntitle: Foo\nrelated:\n  - old-slug\n  - kept"), {"old-slug": "new-slug"}
        )
        self.assertEqual(parse_frontmatter_array(out, "related"), ["new-slug", "kept"])

    def test_dedupes_related_when_canonical_already_present(self):
        out = d.rewrite_cross_references(
            PAGE("type: entity\nrelated: [old-slug, new-slug, kept]"), {"old-slug": "new-slug"}
        )
        self.assertEqual(parse_frontmatter_array(out, "related"), ["new-slug", "kept"])

    def test_unchanged_when_no_redirects_apply(self):
        text = PAGE("type: entity\nrelated: [a, b]", "[[c]] and [[d]] here.")
        self.assertEqual(d.rewrite_cross_references(text, {"nonexistent": "other"}), text)

    def test_multiple_slugs_one_pass(self):
        out = d.rewrite_cross_references("[[old-a]] and [[old-b]] and [[keep-me]].", {"old-a": "canonical", "old-b": "canonical"})
        self.assertEqual(out, "[[canonical]] and [[canonical]] and [[keep-me]].")


# ── Stage 3: rewrite_index_md ───────────────────────────────────────────────

class TestRewriteIndexMd(unittest.TestCase):
    def test_removes_markdown_link_lines(self):
        text = "\n".join([
            "## Entities",
            "- [Accumulibacter](entities/accumulibacter.md)",
            "- [DPAOs (deprecated)](entities/dpaos.md)",
            "- [DPAO](entities/dpao.md)",
        ])
        out = d.rewrite_index_md(text, {"dpaos"})
        self.assertNotIn("dpaos", out)
        self.assertIn("accumulibacter", out)
        self.assertIn("[DPAO](entities/dpao.md)", out)

    def test_removes_wikilink_lines(self):
        text = "\n".join([
            "## Concepts",
            "- [[vfa]] — Volatile fatty acids",
            "- [[volatile-fatty-acids]] — VFAs (long form)",
            "- [[pha]] — PHA storage polymer",
        ])
        out = d.rewrite_index_md(text, {"volatile-fatty-acids"})
        self.assertNotIn("volatile-fatty-acids", out)
        self.assertIn("[[vfa]]", out)
        self.assertIn("[[pha]]", out)

    def test_preserves_headings_and_prose(self):
        text = "\n".join([
            "# Wiki Index", "", "Generated overview.", "",
            "## Entities", "- [DPAOs](entities/dpaos.md)", "- [DPAO](entities/dpao.md)", "",
            "## Concepts", "Some intro prose here, no slugs.",
        ])
        out = d.rewrite_index_md(text, {"dpaos"})
        self.assertIn("# Wiki Index", out)
        self.assertIn("## Entities", out)
        self.assertIn("[DPAO](entities/dpao.md)", out)
        self.assertIn("Some intro prose here", out)
        self.assertNotIn("dpaos.md", out)

    def test_noop_when_empty_set(self):
        text = "- [Foo](entities/foo.md)\n- [Bar](entities/bar.md)"
        self.assertEqual(d.rewrite_index_md(text, set()), text)


# ── Stage 3: merge_duplicate_group (mock LLM) ──────────────────────────────

FIXED_TODAY = lambda: "2026-04-30"  # noqa: E731


class TestMergeDuplicateGroup(unittest.TestCase):
    def test_throws_when_canonical_not_in_group(self):
        with self.assertRaisesRegex(ValueError, r"canonicalSlug"):
            d.merge_duplicate_group(
                [{"slug": "a", "path": "wiki/entities/a.md", "content": PAGE("type: entity", "ax")},
                 {"slug": "b", "path": "wiki/entities/b.md", "content": PAGE("type: entity", "bx")}],
                "z", [], lambda s, u: "",
            )

    def test_throws_when_group_under_two_pages(self):
        with self.assertRaisesRegex(ValueError, r"at least 2"):
            d.merge_duplicate_group(
                [{"slug": "a", "path": "wiki/entities/a.md", "content": PAGE("type: entity", "x")}],
                "a", [], lambda s, u: "",
            )

    def test_merges_bodies_unions_frontmatter_stamps_updated(self):
        pageA = PAGE(
            'type: entity\ntitle: Accumulibacter\ncreated: 2026-04-09\nupdated: 2026-04-09\ntags: [microbiology, ebpr]\nrelated: [dpao, vfa]\nsources: ["doc-A.pdf"]',
            "## Anaerobic Phase\n\nDescription from page A.",
        )
        pageB = PAGE(
            'type: entity\ntitle: 聚磷菌\ncreated: 2026-04-15\nupdated: 2026-04-15\ntags: [paos, propionate]\nrelated: [pha]\nsources: ["doc-B.pdf"]',
            "## 厌氧阶段\n\n来自页面 B 的描述。",
        )
        llm_merged = PAGE(
            'type: entity\ntitle: Accumulibacter\ncreated: 2026-04-09\nupdated: 2026-04-09\ntags: [microbiology, ebpr]\nrelated: [dpao, vfa]\nsources: ["doc-A.pdf"]',
            "## Anaerobic Phase\n\nDescription from page A.\n\n## 厌氧阶段\n\n来自页面 B 的描述。",
        )
        result = d.merge_duplicate_group(
            [{"slug": "accumulibacter", "path": "wiki/entities/accumulibacter.md", "content": pageA},
             {"slug": "聚磷菌", "path": "wiki/entities/聚磷菌.md", "content": pageB}],
            "accumulibacter", [], lambda s, u: llm_merged, today=FIXED_TODAY,
        )
        self.assertIn("Anaerobic Phase", result.canonical_content)
        self.assertIn("厌氧阶段", result.canonical_content)
        self.assertEqual(sorted(parse_frontmatter_array(result.canonical_content, "sources")),
                         sorted(["doc-A.pdf", "doc-B.pdf"]))
        self.assertEqual(sorted(parse_frontmatter_array(result.canonical_content, "tags")),
                         sorted(["ebpr", "microbiology", "paos", "propionate"]))
        self.assertEqual(sorted(parse_frontmatter_array(result.canonical_content, "related")),
                         sorted(["dpao", "pha", "vfa"]))
        self.assertIn("updated: 2026-04-30", result.canonical_content)
        # canonical_path / pages_to_delete / backup bookkeeping is asserted
        # end-to-end in test_cross_source_dedup.py::TestApply — out of scope
        # for this body-merge/union/stamp test.

    def test_rewrites_cross_refs_in_other_pages(self):
        pageA = PAGE("type: entity\ntitle: A\nrelated: [bar]", "body a")
        pageB = PAGE("type: entity\ntitle: B\nrelated: [bar]", "body b")
        referencing = PAGE("type: concept\ntitle: Other\nrelated: [a, b, kept]",
                           "See [[a]] and [[b|the b]] and [[unrelated]].")
        result = d.merge_duplicate_group(
            [{"slug": "a", "path": "wiki/entities/a.md", "content": pageA},
             {"slug": "b", "path": "wiki/entities/b.md", "content": pageB}],
            "a", [{"path": "wiki/concepts/other.md", "content": referencing}],
            lambda s, u: PAGE("type: entity\ntitle: A\n", "merged body"), today=FIXED_TODAY,
        )
        self.assertEqual(len(result.rewrites), 1)
        rewritten = result.rewrites[0]["new_content"]
        self.assertIn("[[a]]", rewritten)
        self.assertIn("[[a|the b]]", rewritten)
        self.assertIn("[[unrelated]]", rewritten)
        self.assertIsNone(_re.search(r"\[\[b(\|[^\]]*)?\]\]", rewritten))
        self.assertEqual(parse_frontmatter_array(rewritten, "related"), ["a", "kept"])

    def test_does_not_include_unchanged_pages_in_rewrites(self):
        irrelevant = PAGE("type: concept\nrelated: [unrelated-slug]", "[[totally-different]] page.")
        result = d.merge_duplicate_group(
            [{"slug": "a", "path": "wiki/entities/a.md", "content": PAGE("type: entity", "x")},
             {"slug": "b", "path": "wiki/entities/b.md", "content": PAGE("type: entity", "y")}],
            "a", [{"path": "wiki/concepts/irrelevant.md", "content": irrelevant}],
            lambda s, u: PAGE("type: entity\ntitle: A\n", "merged"), today=FIXED_TODAY,
        )
        self.assertEqual(result.rewrites, [])
        self.assertNotIn("wiki/concepts/irrelevant.md", [b["path"] for b in result.backup])

    def test_backup_snapshots_pre_merge_state(self):
        pageA = PAGE("type: entity\ntitle: A", "body a")
        pageB = PAGE("type: entity\ntitle: B", "body b")
        refOrig = PAGE("type: concept\nrelated: [b]", "[[b]]")
        result = d.merge_duplicate_group(
            [{"slug": "a", "path": "wiki/entities/a.md", "content": pageA},
             {"slug": "b", "path": "wiki/entities/b.md", "content": pageB}],
            "a", [{"path": "wiki/concepts/ref.md", "content": refOrig}],
            lambda s, u: PAGE("type: entity\ntitle: A", "merged"), today=FIXED_TODAY,
        )
        self.assertEqual(sorted(b["path"] for b in result.backup),
                         sorted(["wiki/concepts/ref.md", "wiki/entities/a.md", "wiki/entities/b.md"]))
        ref_backup = next(b for b in result.backup if b["path"] == "wiki/concepts/ref.md")
        self.assertEqual(ref_backup["content"], refOrig)

    def test_rejects_llm_output_without_frontmatter(self):
        pageA = PAGE("type: entity\ntitle: A", "body a content here")
        pageB = PAGE("type: entity\ntitle: B", "body b content here")
        with self.assertRaisesRegex(ValueError, r"no frontmatter"):
            d.merge_duplicate_group(
                [{"slug": "a", "path": "wiki/entities/a.md", "content": pageA},
                 {"slug": "b", "path": "wiki/entities/b.md", "content": pageB}],
                "a", [], lambda s, u: "Sorry, I can't merge these pages.",
                today=FIXED_TODAY,
            )

    def test_rejects_llm_output_with_shrunk_body(self):
        pageA = PAGE("type: entity\ntitle: A", "A" * 200)
        pageB = PAGE("type: entity\ntitle: B", "B" * 200)
        shrunken = PAGE("type: entity\ntitle: A", "short summary")
        with self.assertRaisesRegex(ValueError, r"threshold|truncation"):
            d.merge_duplicate_group(
                [{"slug": "a", "path": "wiki/entities/a.md", "content": pageA},
                 {"slug": "b", "path": "wiki/entities/b.md", "content": pageB}],
                "a", [], lambda s, u: shrunken, today=FIXED_TODAY,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
