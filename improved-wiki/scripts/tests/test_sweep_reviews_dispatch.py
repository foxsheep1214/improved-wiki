"""Tests for the NashSU sweep-reviews + review-utils port.

Covers the behavior changes:
  - field-name fix: dispatch reads `review_type`, not `type` (which is always
    "review") — each per-type branch now fires (P0 bug).
  - exact-match page index (no substring false-positives).
  - conservative posture: contradiction / confirm / suggestion stay pending in
    the rule stage; missing-page resolves on exact hit; duplicate resolves on
    ANY missing affected page (incl. all-deleted).
  - normalize_review_title (EN+CN prefix stripping).
  - content-stable review_id + field-union / resolved-wins dedup.
  - extract_json_object brace-depth parser + judge response parsing.

Run:  python3 scripts/tests/test_sweep_reviews_dispatch.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import sweep_reviews as sweep  # noqa: E402
import _review_utils as ru  # noqa: E402
import _stage_3_4_review as review  # noqa: E402


def _make_wiki(tmp: Path, pages, reviews):
    """pages: {subdir: [(stem, title)]}, reviews: [(rtype, title, [affected])]."""
    wiki = tmp / "wiki"
    for sub, items in pages.items():
        d = wiki / sub
        d.mkdir(parents=True, exist_ok=True)
        for stem, title in items:
            (d / f"{stem}.md").write_text(
                f"---\ntype: {sub.rstrip('s')}\ntitle: {title}\n---\n# {title}\nbody\n",
                encoding="utf-8",
            )
    rdir = wiki / "REVIEW"
    for i, (rtype, title, affected) in enumerate(reviews):
        sub = rdir / rtype
        sub.mkdir(parents=True, exist_ok=True)
        md = review._render_review_page(
            rtype, title, "desc", affected, [], "medium", "2026-06-28", "Book",
        )
        (sub / f"item-{i}.md").write_text(md, encoding="utf-8")
    return wiki


class TestFieldNameDispatch(unittest.TestCase):
    """P0: review_type drives dispatch; type is always 'review'."""

    def test_scan_reads_review_type_not_type(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            _make_wiki(tmp, {}, [("missing-page", "Foo", []),
                                 ("duplicate", "Bar", [])])
            items = sweep._scan_reviews(tmp / "wiki")
            types = sorted(r["type"] for r in items)
            self.assertEqual(types, ["duplicate", "missing-page"])
            # The literal frontmatter `type:` is "review" and must NOT leak.
            self.assertNotIn("review", types)

    def test_scan_falls_back_to_type_for_legacy_pages(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            rdir = tmp / "wiki" / "REVIEW" / "missing-page"
            rdir.mkdir(parents=True)
            # Legacy page: only `type:` carries the category (no review_type).
            (rdir / "old.md").write_text(
                "---\ntype: missing-page\ntitle: Legacy\nresolved: false\n---\nbody\n",
                encoding="utf-8",
            )
            items = sweep._scan_reviews(tmp / "wiki")
            self.assertEqual(items[0]["type"], "missing-page")

    def test_each_per_type_branch_dispatches(self):
        """A real generated missing-page review whose page now exists resolves;
        a contradiction does not (proves dispatch keys on review_type)."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            _make_wiki(
                tmp,
                {"concepts": [("attention", "attention")]},
                [("missing-page", "attention", []),
                 ("contradiction", "attention", [])],
            )
            res = sweep.sweep_reviews(tmp, dry_run=True, use_llm=False)
            titles = [r["reason"] for r in res["details"]["resolved"]]
            self.assertEqual(res["rule_resolved"], 1)
            self.assertIn("missing page now exists", titles[0])
            # contradiction stays pending (conservative).
            self.assertEqual(res["details"]["pending_types"].get("contradiction"), 1)


class TestExactMatch(unittest.TestCase):
    def test_exact_match_resolves(self):
        idx = {"by_id": {"phase-margin"}, "by_title": set()}
        self.assertTrue(sweep._page_exists("phase-margin", idx))
        self.assertTrue(sweep._page_exists("Phase Margin", idx))  # kebab norm

    def test_no_substring_false_positive(self):
        idx = {"by_id": {"attention-is-all-you-need"}, "by_title": set()}
        self.assertFalse(sweep._page_exists("attention", idx))

    def test_title_exact_match(self):
        idx = {"by_id": set(), "by_title": {"门控循环单元"}}
        self.assertTrue(sweep._page_exists("门控循环单元", idx))
        self.assertFalse(sweep._page_exists("门控", idx))

    def test_index_is_recursive(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            # Page in an arbitrary nested dir NOT in any hardcoded list.
            d = tmp / "wiki" / "custom-folder" / "nested"
            d.mkdir(parents=True)
            (d / "deep-page.md").write_text(
                "---\ntitle: Deep\n---\nbody\n", encoding="utf-8")
            idx = sweep._build_wiki_index(tmp / "wiki")
            self.assertIn("deep-page", idx["by_id"])
            self.assertIn("deep", idx["by_title"])

    def test_index_excludes_review_pages(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            wiki = _make_wiki(tmp, {"concepts": [("real", "Real")]},
                              [("missing-page", "ghost", [])])
            idx = sweep._build_wiki_index(wiki)
            self.assertIn("real", idx["by_id"])
            self.assertNotIn("item-0", idx["by_id"])


class TestConservativePosture(unittest.TestCase):
    """contradiction / confirm / suggestion are NOT auto-resolved by rules,
    even when affected pages exist or were recently touched."""

    def test_contradiction_stays_pending(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            _make_wiki(tmp, {"concepts": [("x", "x")]},
                       [("contradiction", "x", ["concepts/x.md"])])
            res = sweep.sweep_reviews(tmp, dry_run=True, use_llm=False)
            self.assertEqual(res["resolved"], 0)
            self.assertEqual(res["pending"], 1)

    def test_suggestion_stays_pending(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            _make_wiki(tmp, {"concepts": [("y", "y")]},
                       [("suggestion", "y", ["concepts/y.md"])])
            res = sweep.sweep_reviews(tmp, dry_run=True, use_llm=False)
            self.assertEqual(res["resolved"], 0)

    def test_confirm_stays_pending(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            _make_wiki(tmp, {"concepts": [("z", "z")]},
                       [("confirm", "z", ["concepts/z.md"])])
            res = sweep.sweep_reviews(tmp, dry_run=True, use_llm=False)
            self.assertEqual(res["resolved"], 0)


class TestDuplicateRule(unittest.TestCase):
    """duplicate resolves on ANY missing affected page (incl. all-deleted)."""

    def test_resolves_when_one_affected_gone(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            _make_wiki(tmp, {"concepts": [("kept", "kept")]},
                       [("duplicate", "dup",
                         ["concepts/kept.md", "concepts/deleted.md"])])
            res = sweep.sweep_reviews(tmp, dry_run=True, use_llm=False)
            self.assertEqual(res["rule_resolved"], 1)

    def test_resolves_when_all_affected_gone(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            _make_wiki(tmp, {},
                       [("duplicate", "dup",
                         ["concepts/gone1.md", "concepts/gone2.md"])])
            res = sweep.sweep_reviews(tmp, dry_run=True, use_llm=False)
            self.assertEqual(res["rule_resolved"], 1)

    def test_stays_pending_when_all_still_exist(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            _make_wiki(tmp, {"concepts": [("a", "a"), ("b", "b")]},
                       [("duplicate", "dup", ["concepts/a.md", "concepts/b.md"])])
            res = sweep.sweep_reviews(tmp, dry_run=True, use_llm=False)
            self.assertEqual(res["rule_resolved"], 0)
            self.assertEqual(res["pending"], 1)


class TestNormalizeReviewTitle(unittest.TestCase):
    def test_strips_english_prefix(self):
        self.assertEqual(ru.normalize_review_title("Missing page: Foo Bar"), "foo bar")
        self.assertEqual(ru.normalize_review_title("Possible duplicate: X"), "x")

    def test_strips_chinese_prefix(self):
        self.assertEqual(ru.normalize_review_title("缺失页面：注意力机制"), "注意力机制")
        self.assertEqual(ru.normalize_review_title("重复页面: 卷积"), "卷积")

    def test_collapses_whitespace_and_lowercases(self):
        self.assertEqual(ru.normalize_review_title("  Foo   BAR  "), "foo bar")


class TestStableIdAndDedup(unittest.TestCase):
    def test_id_stable_across_prefix_variants(self):
        self.assertEqual(
            ru.review_id_for("missing-page", "Missing page: 注意力"),
            ru.review_id_for("missing-page", "注意力"),
        )

    def test_id_format(self):
        rid = ru.review_id_for("duplicate", "X")
        self.assertRegex(rid, r"^review-[0-9a-f]{8}$")

    def test_union_field(self):
        self.assertEqual(ru.union_field(["a", "b"], ["b", "c"]), ["a", "b", "c"])
        self.assertIsNone(ru.union_field(None, None))
        self.assertIsNone(ru.union_field([], []))

    def test_dedup_resolved_wins_and_unions_fields(self):
        items = [
            {"type": "missing-page", "title": "注意力",
             "resolved": True, "resolved_action": "auto",
             "affected_pages": ["a.md"], "created_at": 100},
            {"type": "missing-page", "title": "Missing page: 注意力",
             "resolved": False, "affected_pages": ["b.md"], "created_at": 50},
        ]
        out = ru.normalize_review_items(items)
        self.assertEqual(len(out), 1)
        m = out[0]
        self.assertTrue(m["resolved"])              # resolved wins
        self.assertEqual(m["resolved_action"], "auto")
        self.assertEqual(sorted(m["affected_pages"]), ["a.md", "b.md"])  # union
        self.assertEqual(m["created_at"], 50)       # earliest

    def test_dedup_is_idempotent(self):
        items = [{"type": "duplicate", "title": "X", "created_at": 1}]
        once = ru.normalize_review_items(items)
        twice = ru.normalize_review_items(once)
        self.assertEqual([i["id"] for i in once], [i["id"] for i in twice])

    def test_sweep_dedup_resolved_twin_survives(self):
        """A resolved review with the same content as a fresh pending one keeps
        the queue from re-surfacing it (resolved wins → dropped from pending)."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            rdir = tmp / "wiki" / "REVIEW" / "missing-page"
            rdir.mkdir(parents=True)
            base = review._render_review_page(
                "missing-page", "注意力机制", "d", [], [], "low", "2026-06-28", "B")
            (rdir / "fresh.md").write_text(base, encoding="utf-8")
            resolved = base.replace("resolved: false", "resolved: true")
            (rdir / "old.md").write_text(
                resolved.replace("注意力机制", "Missing page: 注意力机制"),
                encoding="utf-8")
            res = sweep.sweep_reviews(tmp, dry_run=True, use_llm=False)
            # Both collapse to one id; resolved wins → 0 pending.
            self.assertEqual(res["total"], 0)


class TestExtractJsonObject(unittest.TestCase):
    def test_bare(self):
        self.assertEqual(sweep.extract_json_object('{"resolved": []}'),
                         '{"resolved": []}')

    def test_fenced(self):
        self.assertEqual(
            sweep.extract_json_object('```json\n{"resolved": ["a"]}\n```'),
            '{"resolved": ["a"]}',
        )

    def test_prose_wrapped(self):
        raw = 'Here you go: {"resolved": ["x"]} hope that helps'
        self.assertEqual(sweep.extract_json_object(raw), '{"resolved": ["x"]}')

    def test_nested_braces_and_strings(self):
        raw = '{"resolved": ["a"], "meta": {"note": "has } brace"}}'
        self.assertEqual(sweep.extract_json_object(raw), raw)

    def test_no_object(self):
        self.assertEqual(sweep.extract_json_object("no json here"), "")


class TestParseJudgeResponse(unittest.TestCase):
    def test_only_batch_ids_kept(self):
        batch = [{"review_id": "review-aaaa", "type": "confirm",
                  "title": "x", "affected_pages": [], "description": ""}]
        got = sweep.parse_judge_response(
            '{"resolved": ["review-aaaa", "review-bbbb"]}', batch)
        self.assertEqual(got, {"review-aaaa"})

    def test_empty_on_garbage(self):
        batch = [{"review_id": "review-aaaa"}]
        self.assertEqual(sweep.parse_judge_response("not json", batch), set())

    def test_empty_resolved(self):
        batch = [{"review_id": "review-aaaa"}]
        self.assertEqual(sweep.parse_judge_response('{"resolved": []}', batch), set())


class TestLlmJudgeBatching(unittest.TestCase):
    """llm_judge runs batches via a stubbed conversation call; verifies
    batch-size cap, early-break, and id collection without a real LLM."""

    def _stub(self, responses):
        calls = {"n": 0, "batches": []}

        def fake_make(runtime_dir, stage_prefix):
            def call(system, user):
                i = calls["n"]
                calls["n"] += 1
                calls["batches"].append((system, user))
                return responses[i] if i < len(responses) else '{"resolved": []}'
            return call
        return fake_make, calls

    def test_collects_resolved_ids_across_batches(self):
        items = [{"review_id": f"review-{i:04x}", "type": "missing-page",
                  "title": f"t{i}", "affected_pages": [], "description": ""}
                 for i in range(45)]  # 2 batches (40 + 5)
        fake_make, calls = self._stub([
            '{"resolved": ["review-0000"]}',
            '{"resolved": ["review-002c"]}',  # 0x2c = 44
        ])
        import _llm_call
        orig = _llm_call.make_conversation_llm_call
        _llm_call.make_conversation_llm_call = fake_make
        try:
            got = sweep._llm_judge_reviews(items, [], Path("/tmp"))
        finally:
            _llm_call.make_conversation_llm_call = orig
        self.assertEqual(got, {"review-0000", "review-002c"})
        self.assertEqual(calls["n"], 2)

    def test_early_break_on_empty_batch(self):
        items = [{"review_id": f"review-{i:04x}", "type": "missing-page",
                  "title": f"t{i}", "affected_pages": [], "description": ""}
                 for i in range(80)]  # 2 full batches
        fake_make, calls = self._stub(['{"resolved": []}'])  # first resolves nothing
        import _llm_call
        orig = _llm_call.make_conversation_llm_call
        _llm_call.make_conversation_llm_call = fake_make
        try:
            got = sweep._llm_judge_reviews(items, [], Path("/tmp"))
        finally:
            _llm_call.make_conversation_llm_call = orig
        self.assertEqual(got, set())
        self.assertEqual(calls["n"], 1)  # stopped after the empty batch

    def test_skips_items_without_id(self):
        items = [{"review_id": "", "type": "confirm", "title": "x",
                  "affected_pages": [], "description": ""}]
        import _llm_call
        called = {"n": 0}

        def fake_make(rd, sp):
            def call(s, u):
                called["n"] += 1
                return '{"resolved": []}'
            return call
        orig = _llm_call.make_conversation_llm_call
        _llm_call.make_conversation_llm_call = fake_make
        try:
            got = sweep._llm_judge_reviews(items, [], Path("/tmp"))
        finally:
            _llm_call.make_conversation_llm_call = orig
        self.assertEqual(got, set())
        self.assertEqual(called["n"], 0)  # no judgeable items → no LLM call


if __name__ == "__main__":
    unittest.main()


class TestHumanGateExclusion(unittest.TestCase):
    """2026-07-11 (#8): review items with `human_gate: true` frontmatter (e.g.
    orphan-delete candidates from wiki-lint-fix.py) must NEVER be sent to the
    LLM judge — the judge only sees page ids + titles, so it cannot know
    inbound-link state; a human decides these. Mechanical, not prompt-based.
    """

    def test_human_gated_item_never_reaches_judge(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            wiki = root / "wiki"
            (wiki / "concepts").mkdir(parents=True)
            (wiki / "concepts" / "lonely.md").write_text(
                "---\ntype: concept\ntitle: Lonely\n---\n\n# L\nbody",
                encoding="utf-8")
            rdir = wiki / "REVIEW" / "suggestion"
            rdir.mkdir(parents=True)
            (rdir / "2026-07-10-lint-orphan-delete-concepts-lonely.md").write_text(
                "---\n"
                "type: review\n"
                "review_type: suggestion\n"
                'title: "Orphan delete candidate: concepts/lonely.md"\n'
                "created: 2026-07-10\n"
                "resolved: false\n"
                "human_gate: true\n"
                "affected_pages:\n"
                "  - concepts/lonely.md\n"
                "---\n\n# Orphan delete candidate: concepts/lonely.md\nbody\n",
                encoding="utf-8")

            judged_batches = []
            original = sweep._llm_judge_reviews

            def spy(pending, pages, runtime_dir):
                judged_batches.append(list(pending))
                return set()

            sweep._llm_judge_reviews = spy
            try:
                result = sweep.sweep_reviews(root, dry_run=True, use_llm=True)
            finally:
                sweep._llm_judge_reviews = original

            # The judge was never handed the human-gated item (either no call
            # at all, or a call whose pool excludes it).
            for batch in judged_batches:
                titles = [r.get("title", "") for r in batch]
                self.assertFalse(
                    any("Orphan delete candidate" in t for t in titles),
                    f"human-gated item leaked into judge pool: {titles}")
            # And it stays pending, not resolved.
            self.assertEqual(result.get("resolved", 0), 0)
