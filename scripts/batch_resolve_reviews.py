#!/usr/bin/env python3
"""Bulk resolve / dismiss pending review items — NashSU review-view parity.

NashSU 0.6.7's review panel is not per-item-only. `review-view.tsx` renders a
select-all checkbox and two bulk buttons over the pending queue:

    handleBatchResolve  -> for (id of selected) resolveItem(id, "Bulk resolved")
    handleBatchDismiss  -> for (id of selected) dismissItem(id)

improved-wiki had only the per-item path, which is stricter than NashSU: there,
one human decision can cover N items. Measured on RadarWiki that gap is 510
actionable items — 128 rounds of four-at-a-time questions versus one filtered
bulk action.

This is the CLI equivalent of ticking checkboxes and clicking the button, NOT
an automatic triage: the filter and the `--apply` are supplied by the human,
and without `--apply` the tool only previews. `sweep_reviews.py` remains the
automatic side (it clears items later ingests already satisfied); this tool is
the human side operating in bulk.

`--dismiss` matches `dismissItem` exactly: the file is deleted.

NashSU DOES persist review items — corrected 2026-08-05, an earlier note here
claimed otherwise after only checking for a `persist` middleware inside
review-store.ts. The persistence is external: `auto-save.ts` subscribes to the
store and debounce-writes `.llm-wiki/review.json` 1s after any change, and
`App.tsx` rehydrates it via `loadReviewItems` on project open. What makes
dismiss a deletion is not the absence of persistence but WHAT gets persisted:
`dismissItem` does `items.filter(i => i.id !== id)`, and auto-save then writes
that shorter array back — so the item is gone from the stored record too.
Deleting the file here reproduces exactly that net effect (user decision
2026-08-05; resolve/sweep still never delete).

Usage:
    # preview what the filter selects (no writes)
    batch_resolve_reviews.py --project <wiki-root> --type suggestion \
        --created-before 2026-08-01
    # act on it
    batch_resolve_reviews.py --project <wiki-root> --type suggestion \
        --created-before 2026-08-01 --reason "Superseded by later ingest" --apply
    # dismiss = delete; no --reason needed, --apply is still required
    batch_resolve_reviews.py --project <wiki-root> --type duplicate --dismiss --apply
    # clearResolved parity — shed the TRIAGED backlog (see the caveat below)
    batch_resolve_reviews.py --project <wiki-root> --clear-resolved
    batch_resolve_reviews.py --project <wiki-root> --clear-resolved --apply

`--clear-resolved` ports `clearResolved()` (review-store.ts, the button at
review-view.tsx:332): `items.filter(i => !i.resolved)`. It selects the resolved
set rather than the pending one, and deletes.

It costs more here than in NashSU, and the CLI says so before acting. A
resolved page on disk is not inert: it suppresses its own regeneration
(`_review_utils.is_resolved_review_file`, consulted by every writer) and lets
`sweep_reviews`' resolved-wins dedup kill a re-ingested twin. NashSU's store
keeps resolved items in memory for the same dedup and pays nothing to hold
them; deleting the files here gives that suppression up, so a cleared finding
can return as a fresh pending item on the next ingest or lint.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _review_utils import is_review_resolved  # noqa: E402
from sweep_reviews import _resolve_review  # noqa: E402

BULK_RESOLVE_REASON = "Bulk resolved"
BULK_DISMISS_REASON = "Dismissed (bulk)"

_DATE_IN_NAME = re.compile(r"(\d{8})(?=\.md$|-[0-9a-f]{4}\.md$)")


def _frontmatter_value(text: str, key: str) -> str:
    m = re.search(rf"^{re.escape(key)}:\s*(.*)$", text, re.M)
    return m.group(1).strip().strip('"').strip("'") if m else ""


def _is_pending(text: str) -> bool:
    return not is_review_resolved(text)


def _created_compact(path: Path, text: str) -> str:
    """YYYYMMDD for date filtering: frontmatter `created` first, else filename."""
    created = _frontmatter_value(text, "created")
    digits = re.sub(r"\D", "", created)
    if len(digits) >= 8:
        return digits[:8]
    m = _DATE_IN_NAME.search(path.name) or re.search(r"(\d{8})", path.name)
    return m.group(1) if m else ""


def select_items(
    wiki_dir: Path,
    *,
    types: set[str] | None = None,
    created_before: str | None = None,
    title_contains: str | None = None,
    resolved: bool = False,
) -> list[Path]:
    """Review files matching the human-supplied filter, sorted.

    Defaults to PENDING items: the resolve and dismiss verbs never re-touch an
    already-resolved file, so re-running a filter is idempotent.

    ``resolved=True`` selects the opposite set — the triaged backlog that
    ``clearResolved`` operates on. Nothing else changes; the type/date/title
    filters compose with either state.
    """
    review_dir = wiki_dir / "REVIEW"
    if not review_dir.is_dir():
        return []
    cutoff = re.sub(r"\D", "", created_before or "")[:8]
    needle = (title_contains or "").lower()

    hits: list[Path] = []
    for path in sorted(review_dir.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        if _is_pending(text) is resolved:
            continue
        rtype = _frontmatter_value(text, "review_type") or path.parent.name
        if types and rtype not in types:
            continue
        if cutoff:
            created = _created_compact(path, text)
            if not created or created >= cutoff:
                continue
        if needle and needle not in _frontmatter_value(text, "title").lower():
            continue
        hits.append(path)
    return hits


def _apply(paths, reason: str, dry_run: bool) -> int:
    done = 0
    for path in paths:
        if _resolve_review({"path": path}, reason, dry_run=dry_run):
            done += 1
        else:
            print(f"  x failed: {path}", file=sys.stderr)
    return done


def bulk_resolve(paths, reason: str = BULK_RESOLVE_REASON,
                 dry_run: bool = True) -> int:
    """Mark each item resolved, keeping the file (NashSU resolveItem parity)."""
    return _apply(paths, reason, dry_run)


def bulk_dismiss(paths, reason: str = BULK_DISMISS_REASON,
                 dry_run: bool = True) -> int:
    """Delete each item — NashSU dismissItem parity: the review is gone.

    ``reason`` exists for the CLI/tests to share ``_apply``'s signature; it has
    no on-disk effect once the file is removed.
    """
    del reason
    done = 0
    for path in paths:
        if dry_run:
            done += 1
            continue
        try:
            path.unlink()
            done += 1
        except OSError as exc:
            print(f"  x failed to delete {path}: {exc}", file=sys.stderr)
    return done


def bulk_clear_resolved(paths, dry_run: bool = True) -> int:
    """Delete every already-resolved item — NashSU ``clearResolved`` parity.

    ``review-store.ts`` is ``items.filter(i => !i.resolved)``; here each item is
    a file, so clearing is unlinking. Mechanically identical to
    ``bulk_dismiss``, kept as its own verb because it is a different decision:
    dismiss discards something you never triaged, clear discards the record of
    triage you already did.

    That record is not inert on this side. A resolved page suppresses its own
    regeneration (``_review_utils.is_resolved_review_file``) and lets sweep's
    resolved-wins dedup kill a re-ingested twin, neither of which NashSU's
    in-memory store needs. Callers are expected to say so before acting; the
    CLI does.
    """
    return bulk_dismiss(paths, dry_run=dry_run)


def main_with_args(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Bulk resolve/dismiss pending review items "
                    "(NashSU review-view batch parity).")
    ap.add_argument("--project", default=".", help="wiki root")
    ap.add_argument("--type", action="append", dest="types",
                    help="review_type to include; repeatable")
    ap.add_argument("--created-before", metavar="YYYY-MM-DD",
                    help="only items created strictly before this date")
    ap.add_argument("--title-contains", help="substring filter on title")
    ap.add_argument("--dismiss", action="store_true",
                    help="record a dismissal instead of a resolution")
    ap.add_argument("--clear-resolved", action="store_true",
                    help="delete ALREADY-RESOLVED items (NashSU clearResolved); "
                         "selects the resolved set instead of the pending one")
    ap.add_argument("--reason", help="resolved_reason to record")
    ap.add_argument("--apply", action="store_true",
                    help="actually write; without it this only previews")
    ap.add_argument("--limit", type=int, help="cap the number acted on")
    args = ap.parse_args(argv)

    if args.clear_resolved and args.dismiss:
        print("--clear-resolved and --dismiss select opposite sets "
              "(resolved vs pending) — pass only one.", file=sys.stderr)
        return 2

    wiki_dir = Path(args.project).expanduser().resolve() / "wiki"
    items = select_items(
        wiki_dir,
        types=set(args.types) if args.types else None,
        created_before=args.created_before,
        title_contains=args.title_contains,
        resolved=args.clear_resolved,
    )
    if args.limit is not None:
        items = items[:args.limit]

    state = "resolved" if args.clear_resolved else "pending"
    if not items:
        print(f"No {state} review items match this filter.")
        return 0

    reason = args.reason or BULK_RESOLVE_REASON
    verb = "delete" if (args.dismiss or args.clear_resolved) else "resolve"
    dry_run = not args.apply

    print(f"{len(items)} {state} item(s) selected to {verb}:")
    for path in items[:15]:
        print(f"  - {path.parent.name}/{path.name}")
    if len(items) > 15:
        print(f"  ... and {len(items) - 15} more")
    if args.clear_resolved:
        print("clear-resolved = delete: these triaged pages will be removed.")
        print("  Cost, which NashSU's in-memory store does not pay: a resolved "
              "page on disk suppresses its own regeneration and lets sweep's "
              "resolved-wins dedup kill a re-ingested twin. Once deleted, the "
              "same finding can come back as a fresh pending item.")
    elif args.dismiss:
        print("dismiss = delete: these files will be removed, not marked resolved.")
    else:
        print(f'reason: "{reason}"')

    if dry_run:
        print("\nPREVIEW ONLY — nothing was written. Re-run with --apply "
              "to act on exactly this set.")
        return 0

    if args.clear_resolved:
        done = bulk_clear_resolved(items, dry_run=False)
        tail = "deleted"
    else:
        fn = bulk_dismiss if args.dismiss else bulk_resolve
        done = fn(items, reason=reason, dry_run=False)
        tail = "deleted" if args.dismiss else "kept on disk, marked resolved"
    print(f"\n✓ {verb}d {done}/{len(items)} item(s); files {tail}.")
    return 0 if done == len(items) else 1


def main() -> int:
    return main_with_args()


if __name__ == "__main__":
    sys.exit(main())
