#!/usr/bin/env python3
"""review_fix_guard.py — code-side guard for review-fix batches (audit A8/M8).

M8 (audit 2026-07-02): a 13:31 review-fix batch silently cleared ``related:``
on 4 pages (2 queries + 2 comparisons) that were NOT declared in the review
item's ``affected_pages``. No script applies review fixes to wiki pages —
``sweep_reviews.py`` only flips ``resolved:`` on the REVIEW page itself, and
``wiki-lint-fix.py`` applies lint.json suggestions, not review items — so the
writer is the conversation agent editing files directly. This guard gives that
conversational path an enforceable checkpoint: before (or after) editing pages
for a review item, verify every touched page is declared in the item's
``affected_pages``. The review page itself is always an allowed target
(marking it resolved is part of the fix).

``affected_pages`` declares PAGES, not fields, so the guard enforces the
page-level half of A8 ("restrict edits to fields/pages declared in
affected_pages"); field-level restraint stays a prompt-side rule for the agent.

Usage:
  python3 review_fix_guard.py --review wiki/REVIEW/confirm/2026-07-02-x.md \\
      wiki/queries/foo.md wiki/comparisons/bar.md

Exit codes: 0 = every target declared; 2 = violation (undeclared targets are
listed on stderr); 1 = bad invocation (missing/unreadable review file).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _frontmatter import parse_frontmatter  # noqa: E402


def normalize_page_ref(ref: str) -> str:
    """Normalize a page reference to a wiki-relative, extension-less, lowercase
    key: ``wiki/concepts/Foo.md`` / ``[[concepts/foo]]`` / ``concepts/foo``
    all collapse to ``concepts/foo``. Anything before a ``wiki/`` path segment
    (absolute paths, project prefixes) is stripped."""
    t = str(ref).strip().strip("[]").strip("'\"").replace("\\", "/")
    m = re.search(r"(?:^|/)wiki/(.+)$", t)
    if m:
        t = m.group(1)
    t = re.sub(r"\.md$", "", t, flags=re.IGNORECASE)
    return t.strip("/").lower()


def allowed_pages_from_review(review_text: str) -> set:
    """The set of normalized page keys a review item declares in its
    ``affected_pages`` frontmatter. Missing/malformed field → empty set
    (i.e. NO wiki-page edit is in scope for that item)."""
    fm, _ = parse_frontmatter(review_text)
    affected = fm.get("affected_pages", [])
    if isinstance(affected, str):
        affected = [affected]
    if not isinstance(affected, list):
        return set()
    return {normalize_page_ref(p) for p in affected if str(p).strip()}


def check_review_fix_targets(review_path: Path, targets: list) -> list:
    """Return the list of targets NOT declared by the review item (violations).

    The review page itself is always allowed. Raises OSError if the review
    file cannot be read — a guard that cannot see the declaration must not
    pass anything.
    """
    allowed = allowed_pages_from_review(review_path.read_text(encoding="utf-8"))
    review_key = normalize_page_ref(str(review_path))
    violations = []
    for target in targets:
        key = normalize_page_ref(str(target))
        if key == review_key:
            continue
        if key not in allowed:
            violations.append(str(target))
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a review-fix batch only touches pages declared in "
                    "the review item's affected_pages (audit A8/M8 guard)."
    )
    parser.add_argument("--review", required=True,
                        help="Path to the review item .md (wiki/REVIEW/...)")
    parser.add_argument("targets", nargs="+",
                        help="Pages the fix batch edits (paths or slugs)")
    args = parser.parse_args()

    review_path = Path(args.review).expanduser()
    if not review_path.is_file():
        print(f"Error: review file not found: {review_path}", file=sys.stderr)
        return 1
    try:
        violations = check_review_fix_targets(review_path, args.targets)
    except OSError as e:
        print(f"Error: cannot read review file: {e}", file=sys.stderr)
        return 1

    if violations:
        print("[review-fix-guard] VIOLATION — targets NOT declared in "
              f"affected_pages of {review_path.name}:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        print("Edit only declared pages, or fix the review item's "
              "affected_pages first.", file=sys.stderr)
        return 2

    print(f"[review-fix-guard] OK — {len(args.targets)} target(s) all declared "
          f"in {review_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
