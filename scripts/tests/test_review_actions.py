"""Review action routing — port of NashSU review-view.tsx handleResolve.

improved-wiki previously described this routing in prose inside
process-reviews.md, and the prose had drifted from the real source in four
measurable ways (audited 2026-08-05 against llm_wiki-0.6.7):

1. Options were documented as a fixed Deep Research / Create Page / Skip
   triple. In NashSU they are per-item data (`ReviewItem.options`), the Deep
   Research button is UI-added for suggestion/missing-page ONLY
   (review-view.tsx:491), and a REVIEW block with no OPTIONS line falls back
   to `Approve | Skip`, not `Create Page | Skip` (ingest.ts:2029-2032).
2. The Create Page type-routing keywords were invented
   (person/tool/org/product/型号 …). Real ones match the literal words
   entity/entities/实体 and concept/concepts/概念 only, over
   `action + title + description` (review-create-page.ts:12-13, 57-67).
3. Three action branches were missing entirely: `open:` (view WITHOUT
   resolving), `delete:`, and `save:`.
4. The no-search-source fallback was documented as "never auto-create a
   page". That holds for the explicit `__deep_research__` button
   (review-view.tsx:72-75) but the heuristic research branch does exactly the
   opposite — it falls through to create a page (:171-177).

These tests pin the real behaviour so the routing cannot silently drift back
into prose. Stdlib unittest only — no pytest, no network, no LLM calls.
"""
from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import review_actions as ra  # noqa: E402


def _item(rtype="suggestion", title="T", description="D", **kw):
    base = {
        "review_type": rtype,
        "title": title,
        "description": description,
        "affected_pages": [],
        "search_queries": [],
    }
    base.update(kw)
    return base


# ── ① options are per-item data, not a fixed triple ──────────────────────────

class OptionsComeFromTheType(unittest.TestCase):
    def test_the_four_real_types_get_create_page_skip(self):
        for rtype in ("contradiction", "duplicate", "missing-page",
                      "suggestion"):
            self.assertEqual(ra.default_options_for(rtype),
                             ["Create Page", "Skip"], rtype)

    def test_confirm_falls_back_to_approve_skip(self):
        """confirm is NashSU's unrecognized-type bucket (ingest.ts:2016-2020);
        the prompt never asks for an OPTIONS line for it, so the parser's
        `Approve | Skip` default is what a confirm item actually carries."""
        self.assertEqual(ra.default_options_for("confirm"),
                         ["Approve", "Skip"])

    def test_deep_research_button_is_gated_to_two_types(self):
        self.assertTrue(ra.offers_deep_research("suggestion"))
        self.assertTrue(ra.offers_deep_research("missing-page"))
        for rtype in ("contradiction", "duplicate", "confirm"):
            self.assertFalse(ra.offers_deep_research(rtype), rtype)

    def test_buttons_for_item_matches_the_real_panel(self):
        self.assertEqual(ra.buttons_for("suggestion"),
                         ["Deep Research", "Create Page", "Skip"])
        self.assertEqual(ra.buttons_for("missing-page"),
                         ["Deep Research", "Create Page", "Skip"])
        self.assertEqual(ra.buttons_for("contradiction"),
                         ["Create Page", "Skip"])
        self.assertEqual(ra.buttons_for("duplicate"),
                         ["Create Page", "Skip"])
        self.assertEqual(ra.buttons_for("confirm"), ["Approve", "Skip"])


# ── ② Create Page type routing ───────────────────────────────────────────────

class PageTypeRoutingMatchesNashsu(unittest.TestCase):
    def test_literal_entity_word_routes_to_entities(self):
        d = ra.create_review_page_drafts(
            _item("suggestion", "Add entity page for Foo"), "Create Page")
        self.assertEqual(d[0]["dir"], "entities")

    def test_chinese_entity_word_routes_to_entities(self):
        d = ra.create_review_page_drafts(
            _item("suggestion", "补充实体 Foo"), "Create Page")
        self.assertEqual(d[0]["dir"], "entities")

    def test_invented_keywords_do_not_route(self):
        """person/tool/org/product/型号 were improved-wiki's own invention;
        NashSU routes these to the default (queries), not entities."""
        for title in ("Add a page for this person",
                      "New tool worth documenting",
                      "缺少该型号的说明"):
            d = ra.create_review_page_drafts(
                _item("suggestion", title), "Create Page")
            self.assertEqual(d[0]["dir"], "queries", title)

    def test_the_action_itself_participates_in_matching(self):
        """detectPageType matches over `action + title + description`, so the
        action alone can decide the route (review-create-page.ts:58)."""
        d = ra.create_review_page_drafts(
            _item("suggestion", "Foo", "Bar"), "Create concept page")
        self.assertEqual(d[0]["dir"], "concepts")

    def test_comparison_and_synthesis(self):
        self.assertEqual(
            ra.create_review_page_drafts(
                _item("suggestion", "A vs B 比较"), "Create Page")[0]["dir"],
            "comparisons")
        self.assertEqual(
            ra.create_review_page_drafts(
                _item("suggestion", "Cross-source synthesis"),
                "Create Page")[0]["dir"],
            "synthesis")

    def test_type_fallbacks(self):
        cases = {
            "missing-page": "concepts",
            "contradiction": "queries",
            "suggestion": "queries",
            "duplicate": "queries",
            "confirm": "queries",
        }
        for rtype, expected in cases.items():
            d = ra.create_review_page_drafts(
                _item(rtype, "Plain title", "Plain body"), "Go")
            self.assertEqual(d[0]["dir"], expected, rtype)

    def test_missing_page_splits_into_one_draft_per_candidate(self):
        d = ra.create_review_page_drafts(
            _item("missing-page", "缺少 甲, 乙 页面"), "Create Page")
        self.assertEqual([x["title"] for x in d], ["甲", "乙"])

    def test_non_missing_page_yields_exactly_one_draft(self):
        d = ra.create_review_page_drafts(
            _item("suggestion", "Alpha, Beta and Gamma"), "Create Page")
        self.assertEqual(len(d), 1)

    def test_candidate_titles_are_cleaned(self):
        d = ra.create_review_page_drafts(
            _item("missing-page", "Missing page: [[concepts/cfar-loss]]"),
            "Create Page")
        self.assertEqual([x["title"] for x in d], ["concepts/cfar-loss"])

    def test_no_garbage_second_candidate(self):
        """The two documented divergences from NashSU. Verbatim NashSU emits a
        junk sibling on both of these phrasings (verified against node):
        ": [[concepts/cfar-loss" and "s: Alpha". Reproducing that would create
        a junk page beside every real one — 43 of them on RadarWiki's current
        missing-page backlog."""
        for title, expected in [
            ("Missing page: [[concepts/cfar-loss]]", ["concepts/cfar-loss"]),
            ("Missing pages: Alpha, Beta and Gamma",
             ["Alpha", "Beta", "Gamma"]),
        ]:
            d = ra.create_review_page_drafts(_item("missing-page", title),
                                             "Create Page")
            self.assertEqual([x["title"] for x in d], expected, title)

    def test_empty_title_becomes_untitled(self):
        d = ra.create_review_page_drafts(
            _item("suggestion", "Create:", ""), "Create Page")
        self.assertEqual(d[0]["title"], "Untitled")


# ── ③ the missing action branches ────────────────────────────────────────────

class OpenDoesNotResolve(unittest.TestCase):
    """review-view.tsx:132-153 — "Viewing is not the same as accepting / fixing
    it." This is the branch whose absence let an agent mark an item resolved
    merely for having looked at the page."""

    def test_open_prefix(self):
        d = ra.route_review_action(_item(), "open:wiki/concepts/foo.md",
                                   has_search_source=True)
        self.assertEqual(d["kind"], "open_page")
        self.assertFalse(d["resolves"])
        self.assertEqual(d["page"], "wiki/concepts/foo.md")

    def test_bare_open_words(self):
        for action in ("open", "View", " 查看 ", "打开页面"):
            d = ra.route_review_action(
                _item(affected_pages=["concepts/foo.md"]), action,
                has_search_source=True)
            self.assertEqual(d["kind"], "open_page", action)
            self.assertFalse(d["resolves"], action)

    def test_bare_open_falls_back_to_affected_then_source(self):
        d = ra.route_review_action(
            _item(affected_pages=[], source_path="raw/book.pdf"), "open",
            has_search_source=True)
        self.assertEqual(d["page"], "raw/book.pdf")

    def test_open_with_nothing_to_open_is_a_noop(self):
        d = ra.route_review_action(_item(), "open", has_search_source=True)
        self.assertEqual(d["kind"], "noop")
        self.assertFalse(d["resolves"])


class DeleteAndSave(unittest.TestCase):
    def test_delete_branch(self):
        d = ra.route_review_action(_item(), "delete:wiki/concepts/dup.md",
                                   has_search_source=True)
        self.assertEqual(d["kind"], "delete_file")
        self.assertEqual(d["path"], "wiki/concepts/dup.md")
        self.assertEqual(d["resolve_reason"], "Deleted")
        self.assertTrue(d["resolves"])

    def test_save_branch_decodes_base64(self):
        payload = base64.b64encode("# Hello\n\nBody.".encode()).decode()
        d = ra.route_review_action(_item(), f"save:{payload}",
                                   has_search_source=True)
        self.assertEqual(d["kind"], "save_page")
        self.assertEqual(d["content"], "# Hello\n\nBody.")
        self.assertEqual(d["resolve_reason"], "Saved to Wiki")

    def test_save_branch_survives_undecodable_payload(self):
        d = ra.route_review_action(_item(), "save:!!!not-base64!!!",
                                   has_search_source=True)
        self.assertEqual(d["kind"], "save_page")
        self.assertEqual(d["resolve_reason"], "Save failed")


class DismissalActionsJustResolve(unittest.TestCase):
    def test_skip_and_approve_do_not_create_pages(self):
        for action in ("Skip", "skip", "Approve", "忽略", "keep existing",
                       "no"):
            d = ra.route_review_action(_item(), action,
                                       has_search_source=True)
            self.assertEqual(d["kind"], "resolve", action)
            self.assertEqual(d["resolve_reason"], action)

    def test_anything_else_creates_a_page(self):
        d = ra.route_review_action(_item(), "Create Page",
                                   has_search_source=True)
        self.assertEqual(d["kind"], "create_page")


# ── ④ the two research paths diverge on missing search sources ───────────────

class ResearchFallbackIsBranchSpecific(unittest.TestCase):
    def test_explicit_button_blocks_and_leaves_pending(self):
        d = ra.route_review_action(
            _item(search_queries=["q1", "q2"]), "__deep_research__",
            has_search_source=False)
        self.assertEqual(d["kind"], "blocked_no_search_source")
        self.assertFalse(d["resolves"])

    def test_explicit_button_runs_research_when_configured(self):
        d = ra.route_review_action(
            _item(title="Research: Foo", search_queries=["q1", "q2"]),
            "__deep_research__", has_search_source=True)
        self.assertEqual(d["kind"], "deep_research")
        self.assertEqual(d["topic"], "Foo")
        self.assertEqual(d["search_queries"], ["q1", "q2"])

    def test_heuristic_branch_falls_through_to_create_page(self):
        """The documented rule "never auto-create a page" was only ever true
        of the explicit button. review-view.tsx:171-177 does the opposite."""
        d = ra.route_review_action(_item(), "Research this further",
                                   has_search_source=False)
        self.assertEqual(d["kind"], "create_page")

    def test_heuristic_branch_researches_when_configured(self):
        d = ra.route_review_action(
            _item(search_queries=["seed"]), "Research Foo Bar",
            has_search_source=True)
        self.assertEqual(d["kind"], "deep_research")
        self.assertEqual(d["topic"], "Foo Bar")

    def test_heuristic_branch_drops_the_seed_queries(self):
        """Unlike the explicit button, the heuristic branch passes `undefined`
        for searchQueries (review-view.tsx:181) — the seeds are not reused."""
        d = ra.route_review_action(
            _item(search_queries=["seed"]), "Research Foo",
            has_search_source=True)
        self.assertEqual(d["search_queries"], [])

    def test_internal_sentinels_are_not_heuristic_research(self):
        d = ra.route_review_action(_item(), "__create_page__:research it",
                                   has_search_source=True)
        self.assertEqual(d["kind"], "create_page")


if __name__ == "__main__":
    unittest.main()


# ── ① options reach the review file, so the panel is reconstructable ─────────

class RenderedReviewCarriesItsOptions(unittest.TestCase):
    """NashSU stores `options` ON the item; improved-wiki's review file is that
    item's persistent form, so it has to carry them too. Without this the
    available actions exist only in a doc, which is how the fixed
    Deep Research / Create Page / Skip triple drifted from the real panel."""

    def setUp(self):
        import _stage_3_review as review
        self.render = review._render_review_page

    def _options_line(self, rtype):
        md = self.render(rtype, "T", "D", [], [], "low", "2026-08-05", "src")
        head = md.split("\n---", 1)[0]
        for line in head.splitlines():
            if line.startswith("options:"):
                return line
        return ""

    def test_prompted_types_record_create_page_skip(self):
        for rtype in ("contradiction", "duplicate", "duplicate"):
            self.assertEqual(self._options_line(rtype),
                             'options: ["Create Page", "Skip"]', rtype)

    def test_research_types_record_the_deep_research_button_too(self):
        for rtype in ("suggestion", "missing-page"):
            line = self._options_line(rtype)
            self.assertIn("Deep Research", line, rtype)

    def test_confirm_records_approve_not_create_page(self):
        line = self._options_line("confirm")
        self.assertIn("Approve", line)
        self.assertNotIn("Create Page", line)
        self.assertNotIn("Deep Research", line)

    def test_options_match_buttons_for(self):
        for rtype in ("confirm", "suggestion", "missing-page",
                      "contradiction", "duplicate"):
            expected = 'options: [' + ", ".join(
                f'"{b}"' for b in ra.buttons_for(rtype)) + ']'
            self.assertEqual(self._options_line(rtype), expected, rtype)
