"""Batch resolve/dismiss for the review queue — NashSU review-view parity.

NashSU 0.6.7's review panel is not per-item-only. `review-view.tsx` renders a
select-all checkbox plus two bulk buttons over the pending queue:

    handleBatchResolve  -> for (id of selected) resolveItem(id, "Bulk resolved")
    handleBatchDismiss  -> for (id of selected) dismissItem(id)

process-reviews.md previously claimed "every pending item gets an explicit
human decision", which is stricter than NashSU: there, one human decision can
cover N items. On RadarWiki that difference is 510 actionable items — 128
rounds of four-at-a-time questions versus one filtered bulk action.

The human still decides: this tool never picks a filter or fires on its own.
It is the CLI equivalent of ticking checkboxes and clicking the button.

Stdlib unittest only — no pytest, no network, no LLM calls.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import batch_resolve_reviews as br  # noqa: E402


def _item(root: Path, rtype: str, slug: str, *, resolved=False, title=None,
          created="2026-07-01") -> Path:
    d = root / "wiki" / "REVIEW" / rtype
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{rtype}-{slug}-{created.replace('-', '')}.md"
    p.write_text(
        "---\n"
        f"review_type: {rtype}\n"
        f'title: "{title or slug}"\n'
        f"created: {created}\n"
        f"resolved: {'true' if resolved else 'false'}\n"
        "resolved_at: null\n"
        "resolved_reason: null\n"
        "---\n\n"
        f"# {title or slug}\n\nBody.\n",
        encoding="utf-8")
    return p


class SelectsPendingOnly(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)
        self.pending = _item(self.root, "suggestion", "a")
        self.done = _item(self.root, "suggestion", "b", resolved=True)
        self.miss = _item(self.root, "missing-page", "c")

    def tearDown(self):
        self._t.cleanup()

    def test_resolved_items_are_never_reselected(self):
        sel = br.select_items(self.root / "wiki")
        names = {p.name for p in sel}
        self.assertIn(self.pending.name, names)
        self.assertIn(self.miss.name, names)
        self.assertNotIn(self.done.name, names)

    def test_type_filter(self):
        sel = br.select_items(self.root / "wiki", types={"missing-page"})
        self.assertEqual([p.name for p in sel], [self.miss.name])

    def test_created_before_filter(self):
        _item(self.root, "suggestion", "new", created="2026-08-04")
        old = br.select_items(self.root / "wiki", created_before="2026-08-01")
        self.assertTrue(all("20260804" not in p.name for p in old))
        self.assertTrue(any("20260701" in p.name for p in old))


class BulkResolveMatchesNashsu(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)
        self.a = _item(self.root, "suggestion", "a")
        self.b = _item(self.root, "suggestion", "b")

    def tearDown(self):
        self._t.cleanup()

    def test_dry_run_changes_nothing(self):
        before = self.a.read_text(encoding="utf-8")
        n = br.bulk_resolve([self.a, self.b], reason="Bulk resolved",
                            dry_run=True)
        self.assertEqual(n, 2)
        self.assertEqual(self.a.read_text(encoding="utf-8"), before)

    def test_apply_marks_resolved_and_keeps_the_file(self):
        n = br.bulk_resolve([self.a, self.b], reason="Bulk resolved",
                            dry_run=False)
        self.assertEqual(n, 2)
        for p in (self.a, self.b):
            self.assertTrue(p.is_file(), "resolved files stay on disk")
            t = p.read_text(encoding="utf-8")
            self.assertIn("resolved: true", t)
            self.assertIn('resolved_reason: "Bulk resolved"', t)
            self.assertNotIn("resolved_at: null", t)

    def test_reason_is_recorded_verbatim(self):
        br.bulk_resolve([self.a], reason="Superseded by later ingest",
                        dry_run=False)
        self.assertIn('resolved_reason: "Superseded by later ingest"',
                      self.a.read_text(encoding="utf-8"))


class DismissMatchesNashsuRemoval(unittest.TestCase):
    """NashSU's dismissItem drops the item from its store, and that removal
    reaches disk: `auto-save.ts` debounce-writes the store to
    `.llm-wiki/review.json`, so the shorter array replaces the stored record
    (App.tsx rehydrates it via loadReviewItems). Dismiss therefore means gone,
    not "kept but flagged" — deleting the file here reproduces that net effect.
    resolveItem parity (bulk_resolve) is the half that keeps the file.
    User decision 2026-08-05 — align with NashSU over the project's own prior
    "never delete a review file" convention for this verb."""

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)
        self.a = _item(self.root, "suggestion", "a")

    def tearDown(self):
        self._t.cleanup()

    def test_dismiss_deletes_the_file(self):
        n = br.bulk_dismiss([self.a], dry_run=False)
        self.assertEqual(n, 1)
        self.assertFalse(self.a.exists(),
                         "dismissed items must be gone, matching dismissItem")

    def test_dismiss_dry_run_deletes_nothing(self):
        n = br.bulk_dismiss([self.a], dry_run=True)
        self.assertEqual(n, 1)
        self.assertTrue(self.a.is_file())

    def test_resolve_still_keeps_the_file(self):
        """Only dismiss changed. resolveItem parity (bulk_resolve) still keeps
        the file — that half of the audit trail is untouched."""
        n = br.bulk_resolve([self.a], dry_run=False)
        self.assertEqual(n, 1)
        self.assertTrue(self.a.is_file())
        self.assertIn("resolved: true", self.a.read_text(encoding="utf-8"))


class ClearResolvedMatchesNashsu(unittest.TestCase):
    """NashSU `clearResolved()` — items.filter(i => !i.resolved).

    review-view.tsx:332 renders it as a button over the panel. It is the only
    NashSU verb that removes a triaged item, and improved-wiki had no
    equivalent, so the only way to shed a resolved backlog was `rm`.

    Here it costs more than it does in NashSU: a resolved page on disk also
    suppresses regeneration (is_resolved_review_file) and lets sweep's
    resolved-wins dedup kill a re-ingested twin. Deleting it gives that up,
    which is why the CLI states the consequence and still demands --apply.
    """

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.root = Path(self._t.name)
        self.wiki = self.root / "wiki"
        self.pending = _item(self.root, "suggestion", "still-open")
        self.done_a = _item(self.root, "suggestion", "triaged", resolved=True)
        self.done_b = _item(self.root, "confirm", "approved", resolved=True)

    def test_selects_resolved_items_only(self):
        sel = br.select_items(self.wiki, resolved=True)
        self.assertEqual({self.done_a.name, self.done_b.name},
                         {p.name for p in sel})

    def test_pending_selection_is_unchanged_by_the_new_flag(self):
        sel = br.select_items(self.wiki)
        self.assertEqual([self.pending.name], [p.name for p in sel])

    def test_filters_still_compose(self):
        sel = br.select_items(self.wiki, resolved=True, types={"confirm"})
        self.assertEqual([self.done_b.name], [p.name for p in sel])

    def test_apply_deletes_resolved_and_spares_pending(self):
        done = br.bulk_clear_resolved(
            br.select_items(self.wiki, resolved=True), dry_run=False)
        self.assertEqual(2, done)
        self.assertFalse(self.done_a.exists())
        self.assertFalse(self.done_b.exists())
        self.assertTrue(self.pending.exists())

    def test_dry_run_deletes_nothing(self):
        done = br.bulk_clear_resolved(
            br.select_items(self.wiki, resolved=True), dry_run=True)
        self.assertEqual(2, done)
        self.assertTrue(self.done_a.exists())
        self.assertTrue(self.done_b.exists())

    def test_helper_defaults_to_dry_run(self):
        br.bulk_clear_resolved(br.select_items(self.wiki, resolved=True))
        self.assertTrue(self.done_a.exists())

    def test_cli_without_apply_is_a_preview(self):
        rc = br.main_with_args(
            ["--project", str(self.root), "--clear-resolved"])
        self.assertEqual(0, rc)
        self.assertTrue(self.done_a.exists())

    def test_cli_apply_clears(self):
        rc = br.main_with_args(
            ["--project", str(self.root), "--clear-resolved", "--apply"])
        self.assertEqual(0, rc)
        self.assertFalse(self.done_a.exists())
        self.assertTrue(self.pending.exists())

    def test_clear_resolved_refuses_to_combine_with_dismiss(self):
        rc = br.main_with_args(
            ["--project", str(self.root), "--clear-resolved", "--dismiss",
             "--apply"])
        self.assertEqual(2, rc, "conflicting verbs must not silently pick one")
        self.assertTrue(self.done_a.exists())


class RequiresAnExplicitHumanTrigger(unittest.TestCase):
    def test_apply_is_not_the_default(self):
        import inspect
        # The CLI body lives in main_with_args; main() is the argv-less entry.
        src = inspect.getsource(br.main_with_args)
        self.assertIn("--apply", src)
        self.assertIn("dry_run", src)

    def test_bulk_helpers_default_to_dry_run(self):
        import inspect
        for fn in (br.bulk_resolve, br.bulk_dismiss, br.bulk_clear_resolved):
            sig = inspect.signature(fn)
            self.assertIs(sig.parameters["dry_run"].default, True,
                          f"{fn.__name__} must default to dry-run")


if __name__ == "__main__":
    unittest.main()
