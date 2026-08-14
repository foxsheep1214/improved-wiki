"""Review write side — index/log parity with NashSU handleResolve."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _review_write as rw  # noqa: E402
import review_actions as ra  # noqa: E402

_NOW = datetime(2026, 8, 11, 14, 30, 52, tzinfo=timezone.utc)


def _item(**over) -> dict:
    base = {
        "title": "Gate Charge Losses",
        "description": "Switching loss tracks gate charge.",
        "review_type": "suggestion",
    }
    base.update(over)
    return base


class TestIndexEntry(unittest.TestCase):
    def test_header_capitalizes_only_the_first_character(self):
        self.assertEqual(rw.index_section_header("concepts"), "## Concepts")
        self.assertEqual(rw.index_section_header("queries"), "## Queries")

    def test_entry_drops_the_md_suffix_from_the_link_target(self):
        self.assertEqual(
            rw.index_entry_line("concepts", "gate-charge-2026-08-11.md", "Gate"),
            "- [[concepts/gate-charge-2026-08-11|Gate]]",
        )

    def test_entry_goes_directly_under_an_existing_section(self):
        index = "# Wiki Index\n\n## Concepts\n- [[concepts/old|Old]]\n"
        out = rw.insert_index_entry(index, "concepts", "new-page.md", "New")
        self.assertEqual(out.splitlines()[2:5], [
            "## Concepts",
            "- [[concepts/new-page|New]]",
            "- [[concepts/old|Old]]",
        ])

    def test_missing_section_is_appended(self):
        out = rw.insert_index_entry(
            "# Wiki Index\n", "entities", "acme.md", "Acme")
        self.assertEqual(
            out, "# Wiki Index\n\n## Entities\n- [[entities/acme|Acme]]\n")

    def test_prefix_named_section_does_not_swallow_the_entry(self):
        # NashSU tests with includes() but inserts with a \n-anchored regex, so
        # this shape drops the entry silently. Both steps here agree.
        index = "# Wiki Index\n\n## Conceptsx\n- [[concepts/x|X]]\n"
        out = rw.insert_index_entry(index, "concepts", "real.md", "Real")
        self.assertIn("## Concepts\n- [[concepts/real|Real]]", out)
        self.assertIn("## Conceptsx", out)


class TestLogEntry(unittest.TestCase):
    def test_appends_one_dated_line_without_growing_blank_lines(self):
        out = rw.append_log_entry("# Wiki Log\n\n\n", "2026-08-11", "Did a thing")
        self.assertEqual(out, "# Wiki Log\n- 2026-08-11: Did a thing\n")


class TestWriteCreatedPages(unittest.TestCase):
    def _run(self, root: Path, item: dict, action: str = "Create Page"):
        decision = ra.route_review_action(
            item, action, has_search_source=True, now=_NOW)
        (root / "wiki").mkdir(parents=True, exist_ok=True)
        return decision, rw.write_created_pages(root, item, decision["drafts"])

    def test_page_index_and_log_all_land(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decision, created = self._run(root, _item())

            self.assertEqual(len(created), 1)
            page = root / created[0]["path"]
            self.assertTrue(page.is_file())
            body = page.read_text(encoding="utf-8")
            self.assertIn("type: query", body)
            self.assertIn('title: "Gate Charge Losses"', body)
            self.assertIn("related: []", body)
            self.assertIn("# Gate Charge Losses", body)
            self.assertIn("Switching loss tracks gate charge.", body)

            index = (root / "wiki" / "index.md").read_text(encoding="utf-8")
            self.assertIn("## Queries", index)
            self.assertIn(created[0]["file_name"].removesuffix(".md"), index)

            log = (root / "wiki" / "log.md").read_text(encoding="utf-8")
            self.assertIn("Created 1 page from review:", log)
            self.assertIn(f"`{created[0]['file_name']}`", log)

    def test_resolve_reason_points_at_a_file_that_now_exists(self):
        # The audit trail must be usable: the path in resolved_reason has to
        # resolve to the page that was actually written.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decision, _created = self._run(root, _item())
            claimed = decision["resolve_reason"].removeprefix("Created: ")
            self.assertTrue((root / claimed).is_file(), claimed)

    def test_multiple_drafts_pluralize_and_index_each(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(review_type="missing-page",
                         title="Missing pages: Alpha, Beta")
            decision, created = self._run(root, item)

            self.assertEqual(len(created), 2)
            self.assertEqual(decision["resolve_reason"], "Created 2 pages")
            index = (root / "wiki" / "index.md").read_text(encoding="utf-8")
            for page in created:
                self.assertIn(page["file_name"].removesuffix(".md"), index)
                self.assertTrue((root / page["path"]).is_file())
            log = (root / "wiki" / "log.md").read_text(encoding="utf-8")
            self.assertIn("Created 2 pages from review:", log)

    def test_existing_aggregates_are_extended_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "wiki").mkdir()
            (root / "wiki" / "index.md").write_text(
                "# Wiki Index\n\n## Concepts\n- [[concepts/keep|Keep]]\n",
                encoding="utf-8")
            (root / "wiki" / "log.md").write_text(
                "# Wiki Log\n- 2026-01-01: earlier entry\n", encoding="utf-8")

            self._run(root, _item())

            index = (root / "wiki" / "index.md").read_text(encoding="utf-8")
            log = (root / "wiki" / "log.md").read_text(encoding="utf-8")
            self.assertIn("- [[concepts/keep|Keep]]", index)
            self.assertIn("2026-01-01: earlier entry", log)


class TestWriteSavedQueryPage(unittest.TestCase):
    def test_save_writes_page_index_and_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "wiki").mkdir()
            saved = rw.write_saved_query_page(
                root, "Chat Answer", "Body text.\n", _NOW)

            body = (root / saved["path"]).read_text(encoding="utf-8")
            self.assertIn("type: query", body)
            self.assertIn("Body text.", body)
            # save: writes no `related` key — NashSU review-view.tsx:98.
            self.assertNotIn("related:", body)

            index = (root / "wiki" / "index.md").read_text(encoding="utf-8")
            self.assertIn("## Queries", index)
            log = (root / "wiki" / "log.md").read_text(encoding="utf-8")
            self.assertIn(f"Saved query page `{saved['file_name']}`", log)

    def test_quotes_in_a_title_are_escaped_in_frontmatter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "wiki").mkdir()
            saved = rw.write_saved_query_page(
                root, 'The "Fast" Path', "Body.\n", _NOW)
            body = (root / saved["path"]).read_text(encoding="utf-8")
            self.assertIn(r'title: "The \"Fast\" Path"', body)


class TestFilenameDateConvention(unittest.TestCase):
    def test_filename_is_utc_while_created_is_the_local_calendar_date(self):
        from _wiki_filename import make_query_file_name

        file_name, created = make_query_file_name(
            "Late Night",
            datetime(2026, 8, 3, 16, 30, tzinfo=timezone.utc),
            local_timezone=timezone(timedelta(hours=8)),
        )
        self.assertEqual(file_name, "late-night-2026-08-03-163000.md")
        self.assertEqual(created, "2026-08-04")


if __name__ == "__main__":
    unittest.main()
