#!/usr/bin/env python3
"""normalize_page_types.py — normalize page `type` frontmatter across a wiki.

Two idempotent migrations:
  1. role-as-type → `type: entity`. Legacy pages used an entity ROLE as the page
     TYPE (`type: person/organization/system/standard/model/device`), conflating
     the role and type axes. Collapse them to the flat NashSU type `entity`.
  2. strip the obsolete `role:` field. The skill no longer carries a separate
     entity `role:` axis — NashSU has no such field: entities are a flat
     `type: entity`, and finer distinctions (person vs organization vs …) come
     from schema-defined typed folders, not a frontmatter enum. Any leftover
     `role:` line is removed.
Also fixes the `entities` typo. The canonical page-type vocabulary is the NashSU
set: source, concept, entity, query, comparison, synthesis, finding, thesis,
methodology — plus any custom type a project declares in schema.md.

Idempotent: a page already `type: entity` with no `role:` line is untouched.

Usage:
  IMPROVED_WIKI_ROOT=/path python3 normalize_page_types.py --check
  IMPROVED_WIKI_ROOT=/path python3 normalize_page_types.py --fix
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Entity roles that legacy pages mistakenly used as the page `type`. Detected
# here only to collapse them back to `type: entity` (the role axis is removed).
ROLE_TYPES = {"person", "organization", "system", "standard", "model", "device"}
# Typos / aliases → canonical type.
TYPE_ALIASES = {"entities": "entity"}


def normalize_frontmatter(content: str) -> tuple[str, list[str]]:
    """Normalize the type/role fields in a page's frontmatter.

    Returns (new_content, changes) where changes is a list of human-readable
    descriptions (empty if no change). Idempotent.
    """
    m = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not m:
        return content, []
    fm = m.group(1)
    body = content[m.end():]
    changes: list[str] = []

    type_match = re.search(r"^type:\s*(\S+)\s*$", fm, re.MULTILINE)
    if not type_match:
        return content, []
    old_type = type_match.group(1).strip()

    new_type = old_type
    if old_type in ROLE_TYPES:
        new_type = "entity"
        changes.append(f"type: {old_type} → type: entity")
    elif old_type in TYPE_ALIASES:
        new_type = TYPE_ALIASES[old_type]
        changes.append(f"type: {old_type} → type: {new_type} (typo)")

    lines = fm.split("\n")
    if any(re.match(r"^role:\s*\S+", ln) for ln in lines):
        changes.append("removed obsolete role: field")

    if not changes:
        return content, []  # already canonical, no role line — leave alone

    out: list[str] = []
    for ln in lines:
        if re.match(r"^role:\s*\S+", ln):
            continue  # drop the obsolete entity role axis
        if re.match(r"^type:\s*\S+", ln):
            out.append(f"type: {new_type}")
            continue
        out.append(ln)

    new_fm = "\n".join(out)
    return f"---\n{new_fm}\n---{body}", changes


def scan_wiki(wiki_dir: Path):
    """Yield (path, new_content, changes) for each page needing normalization."""
    for path in sorted(wiki_dir.rglob("*.md")):
        rel = path.relative_to(wiki_dir)
        if rel.parts and rel.parts[0] in ("lint", "REVIEW", "media"):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        new_content, changes = normalize_frontmatter(content)
        if changes:
            yield path, new_content, changes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--project", default=None,
                    help="wiki project root (default: IMPROVED_WIKI_ROOT or cwd)")
    ap.add_argument("--check", action="store_true", help="report only, no writes")
    ap.add_argument("--fix", action="store_true", help="apply the migration")
    args = ap.parse_args()
    if not args.check and not args.fix:
        ap.error("pass --check or --fix")
    root = Path(args.project or os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd()))
    wiki = root / "wiki"
    if not wiki.is_dir():
        print(f"ERROR: wiki/ not found under {root}", file=sys.stderr)
        return 2
    changed = 0
    for path, new_content, changes in scan_wiki(wiki):
        print(f"  {path.relative_to(root)}")
        for c in changes:
            print(f"      {c}")
        if args.fix:
            path.write_text(new_content, encoding="utf-8")
        changed += 1
    mode = "would fix" if args.check else "fixed"
    print(f"\n[{mode}] {changed} page(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
