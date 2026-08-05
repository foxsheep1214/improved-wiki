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

Dismiss deviates from NashSU deliberately. `dismissItem` drops the item from
an in-memory store; improved-wiki keeps every review file on disk as an audit
trail (process-reviews.md: "Resolved review files stay on disk — never delete
them"), so a dismissal is recorded as a resolution carrying a distinct reason.

Usage:
    # preview what the filter selects (no writes)
    batch_resolve_reviews.py --project <wiki-root> --type suggestion \
        --created-before 2026-08-01
    # act on it
    batch_resolve_reviews.py --project <wiki-root> --type suggestion \
        --created-before 2026-08-01 --reason "Superseded by later ingest" --apply
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from sweep_reviews import _resolve_review  # noqa: E402

BULK_RESOLVE_REASON = "Bulk resolved"
BULK_DISMISS_REASON = "Dismissed (bulk)"

_DATE_IN_NAME = re.compile(r"(\d{8})(?=\.md$|-[0-9a-f]{4}\.md$)")


def _frontmatter_value(text: str, key: str) -> str:
    m = re.search(rf"^{re.escape(key)}:\s*(.*)$", text, re.M)
    return m.group(1).strip().strip('"').strip("'") if m else ""


def _is_pending(text: str) -> bool:
    return _frontmatter_value(text, "resolved").lower() != "true"


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
) -> list[Path]:
    """Pending review files matching the human-supplied filter, sorted.

    Only unresolved items are ever returned — an already-resolved file is never
    re-touched, so re-running a filter is idempotent.
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
        if not _is_pending(text):
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
    """Record a dismissal. Unlike NashSU's dismissItem the file is kept."""
    return _apply(paths, reason, dry_run)


def main() -> int:
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
    ap.add_argument("--reason", help="resolved_reason to record")
    ap.add_argument("--apply", action="store_true",
                    help="actually write; without it this only previews")
    ap.add_argument("--limit", type=int, help="cap the number acted on")
    args = ap.parse_args()

    wiki_dir = Path(args.project).expanduser().resolve() / "wiki"
    items = select_items(
        wiki_dir,
        types=set(args.types) if args.types else None,
        created_before=args.created_before,
        title_contains=args.title_contains,
    )
    if args.limit is not None:
        items = items[:args.limit]

    if not items:
        print("No pending review items match this filter.")
        return 0

    reason = args.reason or (
        BULK_DISMISS_REASON if args.dismiss else BULK_RESOLVE_REASON)
    verb = "dismiss" if args.dismiss else "resolve"
    dry_run = not args.apply

    print(f"{len(items)} pending item(s) selected to {verb}:")
    for path in items[:15]:
        print(f"  - {path.parent.name}/{path.name}")
    if len(items) > 15:
        print(f"  ... and {len(items) - 15} more")
    print(f'reason: "{reason}"')

    if dry_run:
        print("\nPREVIEW ONLY — nothing was written. Re-run with --apply "
              "to act on exactly this set.")
        return 0

    fn = bulk_dismiss if args.dismiss else bulk_resolve
    done = fn(items, reason=reason, dry_run=False)
    print(f"\n✓ {verb}d {done}/{len(items)} item(s); files kept on disk.")
    return 0 if done == len(items) else 1


if __name__ == "__main__":
    sys.exit(main())
