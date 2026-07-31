#!/usr/bin/env python3
"""rebuild_index.py — deterministic, LLM-free full rebuild of wiki/index.md.

Scans the on-disk page inventory and writes NashSU 0.6.6's application-owned
index format: pages grouped by frontmatter type, full wiki-relative link
targets, display-title aliases, and title-sorted entries. Full paths preserve
unambiguous links when different page types share a filename stem.

Usage:
  python3 rebuild_index.py                                    # dry-run preview (diff)
  python3 rebuild_index.py --apply                             # write index.md
  python3 rebuild_index.py --apply --wiki-root /path/to/wiki
"""
from __future__ import annotations

import argparse
import difflib
import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _paths import atomic_write
from _stage_3_write import rebuild_index_deterministic


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="Write index.md (default: dry-run diff preview).")
    parser.add_argument("--wiki-root", type=Path, default=None,
                        help="Wiki dir (default: <project>/wiki).")
    parser.add_argument("--project-root", type=Path, default=None,
                        help="Project root (default: $IMPROVED_WIKI_ROOT or cwd).")
    args = parser.parse_args()

    project_root = args.project_root or Path(
        os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd()))
    wiki_dir = args.wiki_root or (project_root / "wiki")
    if not wiki_dir.is_dir():
        print(f"ERROR: wiki/ not found at {wiki_dir}", file=sys.stderr)
        return 2

    new_index = rebuild_index_deterministic(wiki_dir)
    index_path = wiki_dir / "index.md"
    current_index = index_path.read_text(encoding="utf-8") if index_path.exists() else ""

    if new_index == current_index:
        print("[rebuild-index] index.md already matches the on-disk inventory — no change")
        return 0

    diff = "".join(difflib.unified_diff(
        current_index.splitlines(keepends=True),
        new_index.splitlines(keepends=True),
        fromfile="index.md (current)", tofile="index.md (rebuilt)"))

    if not args.apply:
        print(diff or "(current index.md is empty)")
        print("[rebuild-index] DRY-RUN — pass --apply to write")
        return 0

    atomic_write(index_path, new_index)
    print(diff)
    print(f"[rebuild-index] wrote {index_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
