#!/usr/bin/env python3
"""Batch auto-fix broken [[wikilinks]] in wiki pages.

Scans all content pages for [[wikilinks]] whose targets don't exist,
then applies the appropriate fix:

  MATCH_FOUND   target exists under a kebab-case conversion → replace with correct slug
  NO_MATCH      target doesn't exist anywhere → remove [[]], keep link text

Usage:
  python3 fix_broken_wikilinks.py --wiki-root <path>            # dry-run
  python3 fix_broken_wikilinks.py --wiki-root <path> --apply   # apply
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

WIKI_PAGE_RE = re.compile(r'\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]')


def kebabify(name: str) -> str:
    """Human-readable name → kebab-case slug."""
    return name.lower().strip().replace(" ", "-")


def build_slug_index(wiki_dir: Path) -> dict[str, str]:
    """{lowercase_rel_path: rel_path} for all content pages."""
    idx: dict[str, str] = {}
    content_dirs = {"sources", "concepts", "entities", "queries",
                    "comparisons", "synthesis", "findings", "thesis"}
    for sub in content_dirs:
        d = wiki_dir / sub
        if not d.exists():
            continue
        for md in d.rglob("*.md"):
            rel = str(md.relative_to(wiki_dir).with_suffix(""))
            idx[rel.lower()] = rel
    return idx


def try_resolve(target: str, slug_index: dict[str, str]) -> str | None:
    """Try to resolve a broken wikilink target to an existing page."""
    clean = target.strip()

    # Normalize: strip known bad prefixes/suffixes
    for bad_prefix in ("wiki/", "./"):
        if clean.lower().startswith(bad_prefix):
            clean = clean[len(bad_prefix):]
    if clean.lower().endswith(".md"):
        clean = clean[:-3]

    # 1. Exact lowercase match
    if clean.lower() in slug_index:
        return slug_index[clean.lower()]

    # 2. Kebab-case the last segment, try with each content prefix
    stem = clean.split("/")[-1] if "/" in clean else clean
    kebab = kebabify(stem)
    for prefix in ("concepts", "entities", "comparisons", "sources",
                   "queries", "synthesis", "findings", "thesis"):
        key = f"{prefix}/{kebab}".lower()
        if key in slug_index:
            return slug_index[key]

    # 3. Case-insensitive filename match (slow but thorough)
    for key, val in slug_index.items():
        if key.endswith(f"/{kebab}"):
            return val

    return None


def scan_broken_links(wiki_dir: Path, slug_index: dict[str, str]) -> dict[str, list[tuple[str, str | None]]]:
    """Scan all content pages for broken wikilinks.

    Returns {page_rel_path: [(broken_target, resolved_target_or_None), ...]}
    """
    broken: dict[str, list[tuple[str, str | None]]] = {}
    content_dirs = {"sources", "concepts", "entities", "queries",
                    "comparisons", "synthesis", "findings", "thesis"}

    for sub in content_dirs:
        d = wiki_dir / sub
        if not d.exists():
            continue
        for md_file in d.rglob("*.md"):
            text = md_file.read_text(encoding="utf-8")
            page_rel = str(md_file.relative_to(wiki_dir).with_suffix(""))
            page_broken: list[tuple[str, str | None]] = []

            for m in WIKI_PAGE_RE.finditer(text):
                target = m.group(1).strip()
                # Check if already valid
                if target.lower() in slug_index:
                    continue
                resolved = try_resolve(target, slug_index)
                if resolved and resolved != target:
                    page_broken.append((target, resolved))
                else:
                    page_broken.append((target, None))

            if page_broken:
                broken[page_rel] = page_broken

    return broken


def main():
    parser = argparse.ArgumentParser(description="Batch fix broken wikilinks")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--wiki-root", required=True)
    args = parser.parse_args()

    wiki_root = Path(args.wiki_root).expanduser()
    wiki_dir = wiki_root / "wiki"
    if not wiki_dir.exists():
        print(f"ERROR: wiki/ not found at {wiki_dir}")
        return 1

    print(f"[fix] Indexing pages ...")
    slug_index = build_slug_index(wiki_dir)
    print(f"[fix] {len(slug_index)} content pages indexed")

    print(f"[fix] Scanning for broken wikilinks ...")
    broken = scan_broken_links(wiki_dir, slug_index)

    total_fixed = 0
    total_removed = 0
    pages_changed = 0

    for page_rel, links in sorted(broken.items()):
        page_file = wiki_dir / f"{page_rel}.md"
        original = page_file.read_text(encoding="utf-8")
        fixed_text = original
        fix_count = 0
        remove_count = 0

        for target, resolved in links:
            # Build a regex that matches this specific wikilink occurrence
            escaped = re.escape(target)
            pattern = rf'\[\[{escaped}(?:\|[^\]]+?)?\]\]'

            if resolved:
                replacement = f"[[{resolved}]]"
                new_text = re.sub(pattern, replacement, fixed_text, count=1)
                if new_text != fixed_text:
                    fixed_text = new_text
                    fix_count += 1
            else:
                # Remove [[]] but keep text
                def _unwrap(m):
                    pipe = m.group(1)
                    return pipe if pipe else target
                pattern2 = rf'\[\[{escaped}(?:\|([^\]]+?))?\]\]'
                new_text = re.sub(pattern2, _unwrap, fixed_text, count=1)
                if new_text != fixed_text:
                    fixed_text = new_text
                    remove_count += 1

        if fixed_text != original:
            pages_changed += 1
            total_fixed += fix_count
            total_removed += remove_count
            print(f"[fix] {page_rel}: {fix_count} fixed, {remove_count} removed")
            if args.apply:
                tmp = page_file.with_suffix(page_file.suffix + ".tmp")
                tmp.write_text(fixed_text, encoding="utf-8")
                tmp.rename(page_file)

    print(f"\n[fix] Summary: {pages_changed} pages, "
          f"{total_fixed} fixed, {total_removed} removed")
    if not args.apply:
        print("[fix] DRY RUN — use --apply to write changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
