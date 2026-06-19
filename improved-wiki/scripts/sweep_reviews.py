#!/usr/bin/env python3
"""
sweep_reviews.py — Auto-resolve review items satisfied by subsequent ingests.

NashSU v0.4.25 parity for sweep-reviews.ts: scans pending review items,
applies rule-based matching (missing-page now exists, duplicate resolved),
and reports which items can be auto-resolved vs. need human attention.

Usage:
  python3 sweep_reviews.py --project <wiki-root>           # dry-run (report only)
  python3 sweep_reviews.py --project <wiki-root> --apply   # auto-resolve + update files
  python3 sweep_reviews.py --project <wiki-root> --json    # machine-readable output
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _find_runtime_dir(wiki_root: Path) -> Path:
    """Detect runtime directory (NashSU-aligned .llm-wiki/ or legacy wiki/)."""
    candidates = [
        wiki_root / ".llm-wiki",
        wiki_root / ".iwiki-runtime",
        wiki_root / "wiki",
    ]
    for d in candidates:
        if d.exists() and (d / "ingest-cache.json").exists():
            return d
        if d.exists() and (d / "embed-cache.json").exists():
            return d
    # Fallback: use .llm-wiki
    runtime = wiki_root / ".llm-wiki"
    runtime.mkdir(parents=True, exist_ok=True)
    return runtime


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse YAML-like frontmatter from markdown text."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    fm_text = text[3:end].strip()
    result: dict[str, Any] = {}
    for line in fm_text.split("\n"):
        line = line.strip()
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            # Handle lists: [a, b, c]
            if val.startswith("[") and val.endswith("]"):
                val = [v.strip().strip('"').strip("'") for v in val[1:-1].split(",") if v.strip()]
            result[key] = val
    return result


def _build_wiki_index(wiki_dir: Path) -> dict[str, dict]:
    """Scan wiki/ for all pages: {slug: {path, title, mtime, type}}."""
    index: dict[str, dict] = {}
    for sub in ["sources", "concepts", "entities", "queries", "comparisons", "findings", "synthesis", "thesis"]:
        d = wiki_dir / sub
        if not d.exists():
            continue
        for f in d.rglob("*.md"):
            try:
                content = f.read_text(encoding="utf-8")
            except Exception:
                continue
            fm = _parse_frontmatter(content)
            slug = f.stem
            rel = str(f.relative_to(wiki_dir))
            index[slug] = {
                "path": rel,
                "title": fm.get("title", slug),
                "type": fm.get("type", sub.rstrip("s")),
                "mtime": f.stat().st_mtime,
                "domain": fm.get("domain", "general"),
            }
            # Also index by lowercase slug for fuzzy matching
            index[slug.lower()] = index[slug]
            # Index by title
            title_key = str(fm.get("title", "")).lower()
            if title_key and title_key not in index:
                index[f"title:{title_key}"] = index[slug]
    return index


def _scan_reviews(wiki_dir: Path) -> list[dict]:
    """Scan wiki/REVIEW/ for unresolved items."""
    review_dir = wiki_dir / "REVIEW"
    if not review_dir.exists():
        return []
    items: list[dict] = []
    for f in review_dir.rglob("*.md"):
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        fm = _parse_frontmatter(content)
        resolved = fm.get("resolved", "false")
        if str(resolved).lower() in ("true", "yes", "1"):
            continue  # already resolved
        items.append({
            "file": str(f.relative_to(wiki_dir)),
            "path": f,
            "type": fm.get("type", "confirm"),
            "title": fm.get("title", f.stem),
            "affected_pages": fm.get("affected_pages", []) if isinstance(fm.get("affected_pages"), list) else [],
            "created": fm.get("created", ""),
            "frontmatter": fm,
        })
    return items


def _slugify(title: str) -> str:
    """Convert a title to a plausible wiki slug. Strips .md extension and path prefix."""
    slug = title.lower().replace(" ", "-").replace("/", "-").replace("(", "").replace(")", "")
    # Strip .md if present (e.g. "concepts/phase-margin.md" → "concepts-phase-margin")
    if slug.endswith("-md"):
        slug = slug[:-3]
    slug = slug.strip("-.")
    return slug if slug else title.lower()


def _find_matching_page(target: str, index: dict[str, dict]) -> tuple[str | None, str]:
    """Find a page in the wiki index matching the target (title or slug).
    Returns (slug, reason) or (None, "").
    """
    # Clean target: strip .md, extract filename stem from path
    clean = target.lower().strip().strip("[]")
    if clean.endswith(".md"):
        clean = clean[:-3]
    if "/" in clean:
        clean = clean.rsplit("/", 1)[-1]  # "concepts/phase-margin" → "phase-margin"

    # Direct slug match
    slug = _slugify(clean)
    if not slug:
        return None, ""
    if slug in index:
        return slug, f"slug match: {slug}"
    if clean in index:
        return clean, f"exact match: {clean}"
    # Title match
    title_key = f"title:{target.lower()}"
    if title_key in index:
        entry = index[title_key]
        stem = entry.get("path", "").split("/")[-1].replace(".md", "")
        return stem, f"title match: {target}"
    # Partial slug match
    for key, entry in index.items():
        if key.startswith("title:"):
            continue
        entry_title = str(entry.get("title", "")).lower()
        entry_path = str(entry.get("path", "")).lower()
        if clean and (clean in entry_title or entry_title in clean or clean in entry_path):
            return key, f"partial match: {key} ≈ {target}"
    return None, ""


def _check_duplicate_pages(review: dict, index: dict[str, dict]) -> bool:
    """Check if a duplicate review is resolved (one page deleted/merged)."""
    affected = review.get("affected_pages", [])
    if not isinstance(affected, list):
        affected = [str(affected)]
    existing = 0
    for page_ref in affected:
        page_ref = str(page_ref).strip().strip("[]")
        slug = _slugify(page_ref)
        if slug in index:
            existing += 1
    # If at least one of the duplicate pages no longer exists → resolved
    return existing < len(affected) and existing > 0


def _check_missing_page(review: dict, index: dict[str, dict]) -> tuple[bool, str]:
    """Check if a missing-page review is now satisfied."""
    title = review.get("title", "")
    affected = review.get("affected_pages", [])
    if not isinstance(affected, list):
        affected = [str(affected)]

    # Check affected pages
    for page_ref in affected:
        page_ref = str(page_ref).strip().strip("[]")
        slug, reason = _find_matching_page(page_ref, index)
        if slug:
            return True, f"affected page found: {slug} ({reason})"

    # Check if title text appears as a page
    slug, reason = _find_matching_page(title, index)
    if slug:
        return True, f"title match: {slug} ({reason})"

    # Check body text for referenced page names
    body = review.get("frontmatter", {}).get("body", "")
    wikilinks = re.findall(r'\[\[([^\]]+)\]\]', str(body))
    for link in wikilinks:
        link = link.split("|")[0].strip()
        slug = _slugify(link)
        if slug in index:
            return True, f"wikilink now exists: {slug}"

    return False, ""


def _check_affected_pages_updated(review: dict, index: dict[str, dict]) -> tuple[bool, list[str]]:
    """Check if affected pages have been updated since review was created."""
    created_str = review.get("created", "")
    if not created_str:
        return False, []
    try:
        # Parse date in YYYY-MM-DD format
        created_date = datetime.strptime(created_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        created_ts = created_date.timestamp()
    except ValueError:
        return False, []

    affected = review.get("affected_pages", [])
    if not isinstance(affected, list):
        affected = [str(affected)]

    updated: list[str] = []
    for page_ref in affected:
        page_ref = str(page_ref).strip().strip("[]")
        slug, _ = _find_matching_page(page_ref, index)
        if slug and slug in index:
            entry = index[slug]
            if entry.get("mtime", 0) > created_ts:
                updated.append(slug)

    return len(updated) > 0, updated


def _resolve_review(review: dict, reason: str, dry_run: bool = True) -> bool:
    """Mark a review item as resolved by updating its frontmatter."""
    if dry_run:
        return True

    path = review["path"]
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        print(f"  ✘ Cannot read {path}")
        return False

    # Update frontmatter
    today = time.strftime("%Y-%m-%d")
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            fm_text = content[3:end]
            body = content[end + 4:]
            # Replace resolved: false → resolved: true
            fm_lines = fm_text.split("\n")
            new_lines = []
            has_resolved = False
            has_resolved_at = False
            for line in fm_lines:
                if line.strip().startswith("resolved:"):
                    new_lines.append(line.replace("false", "true").replace("False", "true"))
                    has_resolved = True
                elif line.strip().startswith("resolved_at:"):
                    new_lines.append(f"resolved_at: {today}")
                    has_resolved_at = True
                elif line.strip().startswith("resolved_reason:"):
                    new_lines.append(f'resolved_reason: "{reason}"')
                else:
                    new_lines.append(line)
            if not has_resolved:
                new_lines.append(f"resolved: true")
            if not has_resolved_at:
                new_lines.append(f"resolved_at: {today}")
            # Add resolved_reason if not present
            has_reason = any(l.strip().startswith("resolved_reason:") for l in new_lines)
            if not has_reason:
                new_lines.append(f'resolved_reason: "{reason}"')
            new_content = "---\n" + "\n".join(new_lines) + "\n---" + body
            try:
                path.write_text(new_content, encoding="utf-8")
                return True
            except Exception:
                return False
    return False


def sweep_reviews(wiki_root: Path, dry_run: bool = True) -> dict:
    """Main sweep logic. Returns results dict."""
    wiki_dir = wiki_root / "wiki"
    if not wiki_dir.exists():
        return {"error": f"wiki/ not found in {wiki_root}"}

    print(f"=== Review Sweep: {wiki_root.name} ===")
    print(f"Mode: {'dry-run (report only)' if dry_run else 'apply (will modify files)'}")

    # Step 1: Build wiki index
    print("\n[1/3] Building wiki index...")
    index = _build_wiki_index(wiki_dir)
    print(f"  Indexed {len(index)} page references ({len(set(v['path'] for v in index.values()))} unique pages)")

    # Step 2: Scan pending reviews
    print("\n[2/3] Scanning pending reviews...")
    reviews = _scan_reviews(wiki_dir)
    print(f"  Found {len(reviews)} unresolved review items")

    if not reviews:
        print("\n✓ No pending reviews — nothing to sweep.")
        return {"total": 0, "resolved": 0, "pending": 0, "details": []}

    # Step 3: Rule-based matching
    print(f"\n[3/3] Applying rule-based matching...")
    resolved: list[dict] = []
    pending: list[dict] = []
    by_type: dict[str, int] = {}
    by_type_pending: dict[str, int] = {}

    for review in reviews:
        rtype = review.get("type", "unknown")
        by_type[rtype] = by_type.get(rtype, 0) + 1
        should_resolve = False
        reason = ""

        if rtype == "missing-page":
            found, match_reason = _check_missing_page(review, index)
            if found:
                should_resolve = True
                reason = f"missing page now exists: {match_reason}"

        elif rtype == "duplicate":
            if _check_duplicate_pages(review, index):
                should_resolve = True
                reason = "duplicate page no longer exists (merged/deleted)"

        elif rtype in ("contradiction", "suggestion"):
            # Check if affected pages were updated since review
            updated, pages = _check_affected_pages_updated(review, index)
            if updated:
                should_resolve = True
                reason = f"affected pages updated since review: {', '.join(pages)}"
            # Also check for missing-page pattern in suggestion body
            elif _check_missing_page(review, index)[0]:
                should_resolve = True
                reason = f"referenced page now exists: {_check_missing_page(review, index)[1]}"

        # Generic check for any review: see if affected pages now exist
        if not should_resolve:
            found, match_reason = _check_missing_page(review, index)
            if found:
                should_resolve = True
                reason = f"referenced page created: {match_reason}"

        if should_resolve:
            if not dry_run:
                success = _resolve_review(review, reason, dry_run=False)
                if success:
                    resolved.append({"title": review["title"], "reason": reason, "file": review["file"]})
                    print(f"  ✅ {review['title'][:60]}")
                    print(f"     → {reason}")
                else:
                    pending.append(review)
            else:
                resolved.append({"title": review["title"], "reason": reason, "file": review["file"]})
                print(f"  ✅ [DRY RUN] {review['title'][:60]}")
                print(f"     → {reason}")
        else:
            pending.append(review)
            by_type_pending[rtype] = by_type_pending.get(rtype, 0) + 1

    # Report
    print(f"\n{'='*50}")
    print(f"Results: {len(review)} scanned, {len(resolved)} auto-resolved, {len(pending)} pending")
    if resolved:
        print(f"\n✅ Auto-resolved ({len(resolved)}):")
        for r in resolved:
            print(f"  - {r['title'][:80]}")
            print(f"    {r['reason']}")
    if pending:
        print(f"\n⚠️  Still pending ({len(pending)}):")
        by_type_pending_counts: dict[str, int] = {}
        for p in pending:
            t = p.get("type", "unknown")
            by_type_pending_counts[t] = by_type_pending_counts.get(t, 0) + 1
        for t, n in sorted(by_type_pending_counts.items()):
            print(f"  {t}: {n} items")

    return {
        "total": len(reviews),
        "resolved": len(resolved),
        "pending": len(pending),
        "details": {
            "resolved": [{"title": r["title"], "reason": r["reason"]} for r in resolved],
            "pending_types": by_type_pending,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="sweep_reviews.py — Auto-resolve wiki review items (NashSU parity)"
    )
    parser.add_argument("--project", required=True, help="Path to wiki project root")
    parser.add_argument("--apply", action="store_true", help="Actually resolve (default: dry-run)")
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    args = parser.parse_args()

    wiki_root = Path(args.project).expanduser().resolve()
    if not wiki_root.exists():
        print(f"Error: project not found: {wiki_root}", file=sys.stderr)
        return 1

    result = sweep_reviews(wiki_root, dry_run=not args.apply)

    if args.json:
        print("\n--- JSON ---")
        print(json.dumps(result, ensure_ascii=False, indent=2))

    if "error" in result:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
