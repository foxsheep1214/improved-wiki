#!/usr/bin/env python3
"""review_fix_guard.py — guarded repair lifecycle for confirm reviews.

M8 (audit 2026-07-02): a review-fix batch silently edited pages that were not
declared in the Review item's ``affected_pages``. The original guard enforced
that page-level scope. The repair-first lifecycle adds a second invariant: a
Review cannot close merely because somebody selected Fix.

The durable flow is:

1. ``--snapshot`` verifies scope and records pre-edit hashes under a temporary
   path.
2. The agent edits only declared pages and performs issue-specific validation.
3. ``--finalize`` refuses to close the Review unless at least one declared
   page changed, the Review itself did not change underfoot, and the caller
   records what was verified.

The legacy no-mode invocation remains a read-only scope check.

Usage:
  python3 review_fix_guard.py --review wiki/REVIEW/confirm/item.md \
      wiki/concepts/foo.md

  python3 review_fix_guard.py --review wiki/REVIEW/confirm/item.md \
      --snapshot /tmp/codex-work/review-fix/state.json wiki/concepts/foo.md

  # edit and validate the page, then:
  python3 review_fix_guard.py --review wiki/REVIEW/confirm/item.md \
      --finalize /tmp/codex-work/review-fix/state.json \
      --verification "formula recalculated; structural lint passed"

Exit codes: 0 = success; 2 = scope/change/verification gate failed; 1 = bad
invocation or unreadable state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _frontmatter import parse_frontmatter  # noqa: E402
from _frontmatter_array import parse_frontmatter_array  # noqa: E402
from _paths import atomic_write  # noqa: E402
from _review_utils import is_review_resolved  # noqa: E402
from sweep_reviews import _resolve_review  # noqa: E402


_SNAPSHOT_VERSION = 1


def normalize_page_ref(ref: str) -> str:
    """Normalize a page ref to a lowercase wiki-relative extensionless key."""
    t = str(ref).strip().strip("[]").strip("'\"").replace("\\", "/")
    m = re.search(r"(?:^|/)wiki/(.+)$", t)
    if m:
        t = m.group(1)
    t = re.sub(r"\.md$", "", t, flags=re.IGNORECASE)
    return t.strip("/").lower()


def allowed_pages_from_review(review_text: str) -> set:
    """Return normalized pages declared by ``affected_pages``."""
    fm, _ = parse_frontmatter(review_text)
    affected = fm.get("affected_pages", [])
    if not isinstance(affected, list):
        # ``parse_frontmatter`` intentionally handles only inline arrays.
        # Reviews emitted by semantic lint may use the equally valid block
        # form, so fall back to the quote-aware array parser before rejecting
        # a declared target as out of scope.
        block_values = parse_frontmatter_array(review_text, "affected_pages")
        if block_values:
            affected = block_values
    if isinstance(affected, str) and affected.strip():
        affected = [affected]
    if not isinstance(affected, list):
        return set()
    return {normalize_page_ref(p) for p in affected if str(p).strip()}


def check_review_fix_targets(review_path: Path, targets: list) -> list:
    """Return targets not declared by the Review item.

    The Review page itself is always allowed. A guard that cannot read the
    declaration raises instead of passing edits through.
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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wiki_dir_for_review(review_path: Path) -> Path:
    for parent in review_path.resolve().parents:
        if parent.name == "REVIEW":
            return parent.parent
    raise ValueError("review path must be under wiki/REVIEW/")


def _target_path(review_path: Path, target: str) -> Path:
    raw = Path(str(target)).expanduser()
    wiki_dir = _wiki_dir_for_review(review_path).resolve()
    if raw.is_absolute():
        path = raw.resolve()
        try:
            path.relative_to(wiki_dir)
        except ValueError as exc:
            raise ValueError(f"target is outside wiki/: {target}") from exc
        return path
    key = normalize_page_ref(target)
    if not key:
        raise ValueError(f"invalid target page: {target}")
    return (wiki_dir / f"{key}.md").resolve()


def create_fix_snapshot(review_path: Path, targets: list) -> dict:
    """Capture the pending Review and declared page hashes before editing."""
    review_path = review_path.resolve()
    review_text = review_path.read_text(encoding="utf-8")
    if is_review_resolved(review_text):
        raise ValueError("review is already resolved")
    violations = check_review_fix_targets(review_path, targets)
    if violations:
        raise ValueError("targets not declared in affected_pages: "
                         + ", ".join(violations))

    records = []
    seen = set()
    review_key = normalize_page_ref(str(review_path))
    for target in targets:
        key = normalize_page_ref(target)
        if key == review_key or key in seen:
            continue
        path = _target_path(review_path, target)
        if not path.is_file():
            raise ValueError(f"target page does not exist: {target}")
        seen.add(key)
        records.append({
            "key": key,
            "path": str(path),
            "sha256": _sha256(path),
        })
    if not records:
        raise ValueError("at least one affected wiki page is required")
    return {
        "version": _SNAPSHOT_VERSION,
        "review": str(review_path),
        "review_sha256": _sha256(review_path),
        "targets": records,
    }


def write_fix_snapshot(snapshot_path: Path, snapshot: dict) -> None:
    snapshot_path = snapshot_path.expanduser()
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(snapshot_path, json.dumps(snapshot, ensure_ascii=False,
                                           indent=2) + "\n")


def load_fix_snapshot(snapshot_path: Path) -> dict:
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != _SNAPSHOT_VERSION:
        raise ValueError("unsupported or malformed review-fix snapshot")
    if not isinstance(data.get("targets"), list):
        raise ValueError("review-fix snapshot has no targets")
    return data


def validate_fix_changes(review_path: Path, snapshot: dict) -> list[dict]:
    """Return changed target records or raise when finalization is unsafe."""
    review_path = review_path.resolve()
    if str(review_path) != snapshot.get("review"):
        raise ValueError("snapshot belongs to a different review")
    if _sha256(review_path) != snapshot.get("review_sha256"):
        raise ValueError("review changed after snapshot; restart the repair")

    records = snapshot.get("targets") or []
    violations = check_review_fix_targets(
        review_path, [record.get("path", "") for record in records])
    if violations:
        raise ValueError("snapshot target is no longer declared: "
                         + ", ".join(violations))

    changed = []
    wiki_dir = _wiki_dir_for_review(review_path).resolve()
    for record in records:
        path = Path(record.get("path", "")).resolve()
        try:
            path.relative_to(wiki_dir)
        except ValueError as exc:
            raise ValueError(f"snapshot target is outside wiki/: {path}") from exc
        if normalize_page_ref(str(path)) != record.get("key"):
            raise ValueError(f"snapshot target/key mismatch: {path}")
        if not path.is_file():
            raise ValueError(f"target page missing at finalize: {path}")
        if _sha256(path) != record.get("sha256"):
            changed.append(record)
    if not changed:
        raise ValueError("no affected page changed; review remains pending")
    return changed


def finalize_fix(review_path: Path, snapshot_path: Path,
                 verification: str) -> str:
    """Close a Review only after a declared page changed and was verified."""
    verification = re.sub(r"\s+", " ", verification or "").strip()
    if not verification:
        raise ValueError("--verification is required before finalizing a fix")
    verification = verification.replace('"', "'")
    snapshot = load_fix_snapshot(snapshot_path)
    changed = validate_fix_changes(review_path, snapshot)
    pages = [f"wiki/{record['key']}.md" for record in changed]
    reason = f"Fixed: {', '.join(pages)}; Verified: {verification}"
    if not _resolve_review({"path": review_path.resolve()}, reason,
                           dry_run=False):
        raise OSError("failed to mark review resolved")
    try:
        snapshot_path.unlink()
    except OSError:
        pass
    return reason


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Guard and finalize a repair-first Review workflow.")
    parser.add_argument("--review", required=True,
                        help="Review item under wiki/REVIEW/")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--snapshot", metavar="STATE.json",
                      help="record pre-edit hashes in temporary state")
    mode.add_argument("--finalize", metavar="STATE.json",
                      help="verify changes and mark the Review fixed")
    parser.add_argument("--verification",
                        help="validation evidence recorded by --finalize")
    parser.add_argument("targets", nargs="*",
                        help="affected pages the repair will edit")
    args = parser.parse_args()

    review_path = Path(args.review).expanduser()
    if not review_path.is_file():
        print(f"Error: review file not found: {review_path}", file=sys.stderr)
        return 1
    try:
        if args.snapshot:
            if not args.targets:
                raise ValueError("--snapshot requires at least one target")
            state_path = Path(args.snapshot).expanduser()
            write_fix_snapshot(
                state_path, create_fix_snapshot(review_path, args.targets))
            print(f"[review-fix-guard] SNAPSHOT — {len(args.targets)} "
                  f"target(s) recorded in {state_path}")
            return 0
        if args.finalize:
            if args.targets:
                raise ValueError("--finalize reads targets from its snapshot")
            reason = finalize_fix(
                review_path, Path(args.finalize).expanduser(),
                args.verification or "")
            print(f"[review-fix-guard] RESOLVED — {reason}")
            return 0
        if not args.targets:
            raise ValueError("at least one target is required")
        violations = check_review_fix_targets(review_path, args.targets)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2 if isinstance(exc, ValueError) else 1

    if violations:
        print("[review-fix-guard] VIOLATION — targets NOT declared in "
              f"affected_pages of {review_path.name}:", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        return 2

    print(f"[review-fix-guard] OK — {len(args.targets)} target(s) all "
          f"declared in {review_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
