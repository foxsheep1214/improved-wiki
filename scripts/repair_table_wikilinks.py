#!/usr/bin/env python3
"""Repair wikilink alias separators that split Markdown table cells."""
from __future__ import annotations

import argparse
from pathlib import Path

from _paths import atomic_write
from _wikilinks import escape_markdown_table_wikilink_aliases


def _wiki_dir(root: Path) -> Path:
    root = root.expanduser().resolve()
    candidate = root / "wiki"
    if candidate.is_dir():
        return candidate
    if root.name == "wiki" and root.is_dir():
        return root
    raise ValueError(f"No wiki/ directory under {root}")


def scan(root: Path, *, apply: bool = False) -> tuple[list[tuple[Path, int]], int]:
    wiki_dir = _wiki_dir(root)
    changed: list[tuple[Path, int]] = []
    total = 0
    for path in sorted(wiki_dir.rglob("*.md")):
        content = path.read_text(encoding="utf-8")
        repaired, count = escape_markdown_table_wikilink_aliases(content)
        if not count:
            continue
        changed.append((path, count))
        total += count
        if apply:
            atomic_write(path, repaired)
    return changed, total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "project_roots",
        nargs="+",
        type=Path,
        help="Wiki project root(s), or their wiki/ directories",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the repairs (default: dry-run)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Return exit 1 when a dry-run finds malformed table links",
    )
    args = parser.parse_args()

    findings = 0
    for root in args.project_roots:
        changed, total = scan(root, apply=args.apply)
        findings += total
        action = "repaired" if args.apply else "would repair"
        print(
            f"[table-wikilinks] {root}: {action} {total} alias pipe(s) "
            f"across {len(changed)} file(s)"
        )
        for path, count in changed:
            print(f"  {path}: {count}")
    if args.check and findings and not args.apply:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
