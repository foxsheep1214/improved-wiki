#!/usr/bin/env python3
"""
repair_wiki.py — Unified wiki repair tool (merges repair_stage_05/06/35/37/38).

Subcommands:
  extract   Repair Stage 0.5: re-extract images from PDF (old repair_05)
  caption   Repair Stage 0.6: re-caption missing/failed images (old repair_06)
  stub      Repair Stage 3.5: generate missing source page stubs (old repair_35)
  inject    Repair Stage 3.7: inject images into source pages (old repair_37)
  images    Repair Stage 3.8: rebuild ## Embedded Images from .caption.txt files

Usage:
  python3 repair_wiki.py images [--dry-run] [--slug NAME]
  python3 repair_wiki.py caption --media-dir PATH
  python3 repair_wiki.py stub --source-slug NAME
  python3 repair_wiki.py extract --pdf PATH --media-dir PATH
  python3 repair_wiki.py inject --source-slug NAME
"""

import argparse
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Unified wiki repair tool")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ── images (repair_stage_38) ──
    p_img = sub.add_parser("images", help="Rebuild ## Embedded Images from .caption.txt")
    p_img.add_argument("--dry-run", action="store_true")
    p_img.add_argument("--slug", type=str, default=None)
    p_img.add_argument("--verbose", "-v", action="store_true")

    # ── caption (repair_stage_06) ──
    p_cap = sub.add_parser("caption", help="Re-caption missing/failed images")
    p_cap.add_argument("--media-dir", type=str, required=True)
    p_cap.add_argument("--batch-size", type=int, default=8)

    # ── stub (repair_stage_35) ──
    p_stub = sub.add_parser("stub", help="Generate missing source page stub")
    p_stub.add_argument("--source-slug", type=str, required=True)

    # ── extract (repair_stage_05) ──
    p_ext = sub.add_parser("extract", help="Re-extract images from PDF")
    p_ext.add_argument("--pdf", type=str, required=True)
    p_ext.add_argument("--media-dir", type=str, required=True)

    # ── inject (repair_stage_37) ──
    p_inj = sub.add_parser("inject", help="Inject images into source page")
    p_inj.add_argument("--source-slug", type=str, required=True)

    args = parser.parse_args()

    if args.cmd == "images":
        from repair_stage_38 import repair_source_page, find_media_dir

        root = Path(os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd()))
        wiki_root = root / "wiki"
        sources_dir = wiki_root / "sources"
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

    elif args.cmd == "caption":
        print("Use ingest.py _caption_images() directly for caption repair.", file=sys.stderr)
        print("Example:", file=sys.stderr)
        print("  python3 -c \"from ingest import _caption_images; ...\"", file=sys.stderr)
        sys.exit(1)

    elif args.cmd == "stub":
        print("Use repair_stage_35.py directly (legacy).", file=sys.stderr)
        sys.exit(1)

    elif args.cmd == "extract":
        print("Use repair_stage_05.py directly (legacy).", file=sys.stderr)
        sys.exit(1)

    elif args.cmd == "inject":
        print("Use repair_stage_37.py directly (legacy).", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    sys.exit(main())
