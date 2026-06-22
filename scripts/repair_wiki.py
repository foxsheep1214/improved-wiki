#!/usr/bin/env python3
"""
repair_wiki.py — Rebuild ## Embedded Images sections from .caption.txt files.

Repairs Stage 3.2 (image injection): scans all source pages, reads
.caption.txt files from wiki/media/, and rebuilds the "## Embedded Images"
table.  Supports dry-run mode.

Usage:
  python3 repair_wiki.py [--dry-run] [--slug NAME] [--verbose]
"""

import argparse
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Rebuild ## Embedded Images in source pages")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--slug", type=str, default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    from repair_stage_38 import repair_source_page

    root = Path(os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd()))
    wiki_root = root / "wiki"
    sources_dir = wiki_root / "sources"
    if not sources_dir.is_dir():
        print(f"ERROR: wiki/sources/ not found in {root}", file=sys.stderr)
        return 1

    total_pages = 0
    total_changes = 0
    mode = "DRY-RUN" if args.dry_run else "repair"

    for page_path in sorted(sources_dir.rglob("*.md")):
        rel = page_path.relative_to(sources_dir)
        if args.slug and args.slug not in str(rel) and args.slug not in page_path.stem:
            continue
        changes = repair_source_page(page_path, wiki_root, dry_run=args.dry_run)
        if changes > 0:
            total_pages += 1
            total_changes += changes
            if args.verbose:
                print(f"  [{mode}] {rel}: {changes} updated")

    print(f"[{mode}] {total_pages} source pages, {total_changes} captions updated")
    if args.dry_run:
        print("[dry-run] No files modified. Remove --dry-run to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
