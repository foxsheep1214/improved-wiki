"""Batch Deep Research over review items (NashSU v0.6.10 parity).

NashSU 0.6.10 extended its review panel: the select-all checkbox that already
fed batch resolve/dismiss now also feeds Deep Research, and a completed or
failed research task can be rerun. `review-batch-research.ts` is the whole
selection contract:

    reviewSupportsResearch -> type is "suggestion" or "missing-page"
    reviewResearchTopic    -> title, else the first line of the description
    selectedResearchReviews-> unresolved AND selected AND supports research
                              AND not already in flight

This port keeps improved-wiki's 🔴 gate intact by splitting the click in two:
the tool SELECTS and previews; the human authorizes with --apply; the actual
search/synthesis/write still runs through the deep-research.md flow one topic
at a time. The script never touches the network and never writes a wiki page.

Stdlib unittest only.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import batch_research_reviews as brr  # noqa: E402


def _write_review(
    wiki: Path,
    rtype: str,
    title: str,
    *,
    created: str = "2026-08-02",
    resolved: bool = False,
    resolved_reason: str = "",
    queries: list[str] | None = None,
    body: str = "Body text explaining the gap.",
    name: str | None = None,
) -> Path:
    d = wiki / "REVIEW" / rtype
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "type: review",
        f"review_type: {rtype}",
        f'title: "{title}"',
        f"created: {created}",
        f"resolved: {'true' if resolved else 'false'}",
    ]
    if queries:
        lines.append("search_queries:")
        lines.extend(f'  - "{q}"' for q in queries)
    if resolved_reason:
        lines.append(f'resolved_reason: "{resolved_reason}"')
    lines += ["---", "", f"# {title}", "", body, ""]
    path = d / (name or f"{rtype}-{abs(hash(title)) % 99999}-{created}.md")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class TestSupportsResearch(unittest.TestCase):
    """NashSU reviewSupportsResearch — only two types are research-eligible."""

    def test_suggestion_and_missing_page_are_eligible(self):
        self.assertTrue(brr.review_supports_research("suggestion"))
        self.assertTrue(brr.review_supports_research("missing-page"))

    def test_other_types_are_not(self):
        for rtype in ("contradiction", "duplicate", "confirm", "unknown"):
            self.assertFalse(brr.review_supports_research(rtype), rtype)


class TestResearchTopic(unittest.TestCase):
    """NashSU reviewResearchTopic + process-reviews.md's prefix stripping."""

    def test_title_is_the_topic(self):
        self.assertEqual(
            brr.research_topic("Sidelobe blanking in modern radar", "body"),
            "Sidelobe blanking in modern radar")

    def test_empty_title_falls_back_to_first_body_line(self):
        self.assertEqual(
            brr.research_topic("", "\n\nFirst real line\nSecond line"),
            "First real line")

    def test_type_marker_and_missing_page_prefix_are_stripped(self):
        self.assertEqual(
            brr.research_topic("[stale] Missing page: Doppler beam sharpening",
                               "body"),
            "Doppler beam sharpening")

    def test_wikilink_target_is_unwrapped(self):
        self.assertEqual(
            brr.research_topic("缺失页面: [[concepts/脉冲压缩]]", "body"),
            "脉冲压缩")

    def test_topic_is_not_truncated_like_a_filename(self):
        # derive_review_topic caps at 40 chars and hyphenates for filenames.
        # A research topic is a search string; it must stay a readable phrase.
        long_title = ("Adaptive digital beamforming for airborne early warning "
                      "radar under mainlobe jamming")
        self.assertEqual(brr.research_topic(long_title, "b"), long_title)


class TestSeedQueries(unittest.TestCase):
    """deep-research.md §2.2: pass search_queries verbatim, else [topic]."""

    def test_frontmatter_queries_are_used_verbatim(self):
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d) / "wiki"
            _write_review(wiki, "suggestion", "T",
                          queries=["first query", "second query"])
            item = brr.select_research_items(wiki)[0]
            self.assertEqual(item["queries"], ["first query", "second query"])

    def test_missing_queries_fall_back_to_the_topic(self):
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d) / "wiki"
            _write_review(wiki, "missing-page", "Pulse compression")
            item = brr.select_research_items(wiki)[0]
            self.assertEqual(item["queries"], ["Pulse compression"])


class TestSelection(unittest.TestCase):
    def _wiki(self, d: str) -> Path:
        wiki = Path(d) / "wiki"
        _write_review(wiki, "suggestion", "Eligible suggestion")
        _write_review(wiki, "missing-page", "Eligible gap")
        _write_review(wiki, "contradiction", "Not researchable")
        _write_review(wiki, "duplicate", "Also not researchable")
        _write_review(wiki, "suggestion", "Already resolved", resolved=True,
                      resolved_reason="Skip")
        return wiki

    def test_only_pending_eligible_types_are_selected(self):
        with tempfile.TemporaryDirectory() as d:
            titles = {i["topic"] for i in brr.select_research_items(self._wiki(d))}
        self.assertEqual(titles, {"Eligible suggestion", "Eligible gap"})

    def test_type_filter_composes(self):
        with tempfile.TemporaryDirectory() as d:
            items = brr.select_research_items(self._wiki(d),
                                              types={"missing-page"})
        self.assertEqual([i["topic"] for i in items], ["Eligible gap"])

    def test_ineligible_type_filter_selects_nothing(self):
        # Asking for contradictions must not silently widen to everything.
        with tempfile.TemporaryDirectory() as d:
            items = brr.select_research_items(self._wiki(d),
                                              types={"contradiction"})
        self.assertEqual(items, [])

    def test_title_and_date_filters_compose(self):
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d) / "wiki"
            _write_review(wiki, "suggestion", "Old topic", created="2026-07-01")
            _write_review(wiki, "suggestion", "New topic", created="2026-08-20")
            old = brr.select_research_items(wiki, created_before="2026-08-01")
            hit = brr.select_research_items(wiki, title_contains="new")
        self.assertEqual([i["topic"] for i in old], ["Old topic"])
        self.assertEqual([i["topic"] for i in hit], ["New topic"])

    def test_limit_caps_the_worklist(self):
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d) / "wiki"
            for n in range(5):
                _write_review(wiki, "suggestion", f"Topic {n}")
            self.assertEqual(len(brr.select_research_items(wiki, limit=2)), 2)

    def test_selection_is_deterministic(self):
        with tempfile.TemporaryDirectory() as d:
            wiki = self._wiki(d)
            self.assertEqual(brr.select_research_items(wiki),
                             brr.select_research_items(wiki))


class TestRerun(unittest.TestCase):
    """NashSU 0.6.10: rerun completed or failed research.

    Failed research leaves the item PENDING (the saved path is the success
    boundary), so it is already in the default set. Completed research
    resolved the item with a ``Research saved:`` reason — only --rerun
    reaches those.
    """

    def _wiki(self, d: str) -> Path:
        wiki = Path(d) / "wiki"
        _write_review(wiki, "suggestion", "Never researched")
        _write_review(
            wiki, "suggestion", "Completed research", resolved=True,
            resolved_reason="Research saved: wiki/queries/research-x.md")
        _write_review(wiki, "suggestion", "Resolved by hand", resolved=True,
                      resolved_reason="Skip")
        return wiki

    def test_default_excludes_completed_research(self):
        with tempfile.TemporaryDirectory() as d:
            topics = {i["topic"] for i in brr.select_research_items(self._wiki(d))}
        self.assertEqual(topics, {"Never researched"})

    def test_rerun_includes_completed_research_only(self):
        with tempfile.TemporaryDirectory() as d:
            topics = {i["topic"]
                      for i in brr.select_research_items(self._wiki(d), rerun=True)}
        # "Resolved by hand" was a human Skip, not a research result — rerunning
        # it would overturn a decision the user already made.
        self.assertEqual(topics, {"Never researched", "Completed research"})

    def test_rerun_flags_the_prior_page(self):
        with tempfile.TemporaryDirectory() as d:
            items = brr.select_research_items(self._wiki(d), rerun=True)
            done = next(i for i in items if i["topic"] == "Completed research")
        self.assertEqual(done["previous_page"], "wiki/queries/research-x.md")


class TestCli(unittest.TestCase):
    def _wiki(self, d: str) -> Path:
        wiki = Path(d) / "wiki"
        _write_review(wiki, "suggestion", "Eligible suggestion",
                      queries=["q one", "q two"])
        return wiki

    def test_preview_writes_nothing_and_reports_the_count(self):
        with tempfile.TemporaryDirectory() as d:
            wiki = self._wiki(d)
            rc = brr.main_with_args(["--project", str(wiki.parent)])
        self.assertEqual(rc, 0)
        self.assertFalse((wiki.parent / ".llm-wiki" / "research-batch.json").exists())

    def test_apply_writes_the_confirmed_worklist(self):
        with tempfile.TemporaryDirectory() as d:
            wiki = self._wiki(d)
            rc = brr.main_with_args(["--project", str(wiki.parent), "--apply"])
            plan_path = wiki.parent / ".llm-wiki" / "research-batch.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        self.assertEqual(len(plan["items"]), 1)
        self.assertEqual(plan["items"][0]["queries"], ["q one", "q two"])
        self.assertEqual(plan["source_mode"], "web")

    def test_source_mode_is_passed_through(self):
        with tempfile.TemporaryDirectory() as d:
            wiki = self._wiki(d)
            brr.main_with_args(["--project", str(wiki.parent), "--apply",
                                "--source-mode", "both"])
            plan = json.loads(
                (wiki.parent / ".llm-wiki" / "research-batch.json")
                .read_text(encoding="utf-8"))
        self.assertEqual(plan["source_mode"], "both")

    def test_empty_selection_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d) / "wiki"
            (wiki / "REVIEW").mkdir(parents=True)
            self.assertEqual(
                brr.main_with_args(["--project", str(wiki.parent)]), 0)


if __name__ == "__main__":
    unittest.main()
