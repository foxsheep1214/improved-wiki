#!/usr/bin/env python3
"""normalize_page_types.py — normalize page `type` frontmatter across a wiki.

Problem: ~190 pages use an entity ROLE as the page TYPE
(`type: person/organization/system/standard/model/device`), conflating the
two axes. This breaks schema routing (type↔directory), makes type statistics
meaningless, and corrupts the Graph command's type-affinity signal.

This migrates role-as-type → `type: entity` + `role: <role>`, and fixes the
`entities` typo. The canonical page-type vocabulary is:
  source, concept, entity, query, comparison, synthesis (findings/thesis/
  methodology are sub-types of synthesis). Entity roles live in a separate
  `role:` field: person, organization, system, standard, model, device, ...

Idempotent: a page already `type: entity` (with or without role) is untouched.

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

# Entity roles that were mistakenly used as page `type`.
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
    new_role = None
    if old_type in ROLE_TYPES:
        new_type = "entity"
        new_role = old_type
        changes.append(f"type: {old_type} → type: entity + role: {old_type}")
    elif old_type in TYPE_ALIASES:
        new_type = TYPE_ALIASES[old_type]
        changes.append(f"type: {old_type} → type: {new_type} (typo)")
    else:
        return content, []  # already canonical (or unknown type — leave alone)

    lines = fm.split("\n")
    out: list[str] = []
    type_written = False
    role_written = False
    has_role = any(re.match(r"^role:\s*\S+", ln) for ln in lines)
    for ln in lines:
        if re.match(r"^type:\s*\S+", ln):
            out.append(f"type: {new_type}")
            type_written = True
            if new_role and not has_role and not role_written:
                out.append(f"role: {new_role}")
                role_written = True
            continue
        if new_role and has_role and re.match(r"^role:\s*\S+", ln) and not role_written:
            role_written = True  # keep existing role, don't clobber
        out.append(ln)
    if not type_written:
        return content, []

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
