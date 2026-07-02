#!/usr/bin/env python3
"""enrich_wikilinks_retroactive.py — retroactive, SAFE wikilink enrichment.

Audit point ③: the wiki is link-sparse (~51% of pages have no outbound
[[wikilink]]; 7868 orphans). The per-ingest LLM enrichment only links NEW
pages, so existing pages stay isolated. Bulk auto-linking via token-overlap
"related" suggestions is RISKY (wrong edges pollute the graph worse than
sparsity), so this tool does only DETERMINISTIC, CORRECT link backfills:

  1. Source-link backfill (default): every page with a `sources:` frontmatter
     field gets a body wikilink to each of its source pages
     (`[[sources/<slug>]]`) if not already linked. 5953 no-outlink pages on
     HardwareWiki have a `sources:` field → this alone cuts no-outlinks from
     6367 to ~680, with zero guessing.
  2. Broken-link auto-fix (--fix-broken): applies the suggestion engine's
     HIGH-confidence (≥0.74) broken-link corrections (typo → closest page).
     O(n²) over the wiki — slow on large wikis; skip unless needed.

No token-overlap "related" links are auto-added — those stay in wiki-lint.sh's
--fix-links for human-reviewed runs.

Usage:
  IMPROVED_WIKI_ROOT=/path python3 enrich_wikilinks_retroactive.py            # dry-run
  IMPROVED_WIKI_ROOT=/path python3 enrich_wikilinks_retroactive.py --apply
  IMPROVED_WIKI_ROOT=/path python3 enrich_wikilinks_retroactive.py --apply --fix-broken
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))


from _frontmatter import WIKILINK_RE as _WIKILINK_RE  # noqa: E402
from _frontmatter_array import parse_frontmatter_array  # noqa: E402
from _paths import iter_wiki_pages, WIKI_ARTIFACT_DIRS  # noqa: E402
from pathlib import PurePosixPath  # noqa: E402


def source_slug_from_raw(raw_path: str) -> str:
    """raw/Datasheet/.../X.pdf → sources/Datasheet/.../X  (mirrors raw/ layout).

    Extension handling matches the canonical mapping
    (_core.source_slug_from_raw_path / NashSU source-identity.ts): the LAST
    extension is stripped whatever it is. The old local copy only recognized
    .pdf/.pptx/.docx, so any other extension produced a slug that diverged
    from the canonical source-page path.
    """
    p = raw_path.strip().strip('"').strip("'")
    if p.startswith("raw/"):
        p = p[4:]
    try:
        p = str(PurePosixPath(p).with_suffix(""))
    except ValueError:
        pass
    return f"sources/{p}"


def backfill_source_links(content: str) -> tuple[str, int]:
    """Ensure the page body links to every source in its `sources:` field.

    Returns (new_content, n_links_added). Idempotent. Appends a `## Sources`
    section (or extends an existing one) with `[[sources/<slug>]]` for each
    missing source.
    """
    m = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not m:
        return content, 0
    fm, body = m.group(1), content[m.end():]
    # Shared parser — handles BOTH inline [a, b] and block-style arrays (the
    # old local regex only understood the inline form).
    raw_paths = parse_frontmatter_array(content, "sources")
    if not raw_paths:
        return content, 0

    linked = set()
    for wl in _WIKILINK_RE.finditer(body):
        tgt = wl.group(1).strip().replace(".md", "")
        linked.add(tgt.lower())
        linked.add(tgt.split("/")[-1].lower())

    missing = []
    for rp in raw_paths:
        slug = source_slug_from_raw(rp)
        if slug.lower() in linked or slug.split("/")[-1].lower() in linked:
            continue
        missing.append(slug)
    if not missing:
        return content, 0

    addition_lines = "".join(f"- [[{s}]]\n" for s in missing)
    src_section = re.search(r"(^|\n)(## Sources\s*\n)", body)
    if src_section:
        insert_at = src_section.end()
        new_body = body[:insert_at] + addition_lines + body[insert_at:]
    else:
        new_body = body.rstrip() + "\n\n## Sources\n\n" + addition_lines
    return f"---\n{fm}\n---{new_body}", len(missing)


def scan_wiki(wiki_dir: Path):
    # Write-guard: never backfill source links INTO an aggregate file.
    # Artifact dirs come from the shared _paths.WIKI_ARTIFACT_DIRS.
    for rel, content in iter_wiki_pages(
        wiki_dir,
        anchor_files=("index.md", "log.md", "overview.md", "schema.md"),
    ):
        new_content, n = backfill_source_links(content)
        if n:
            yield wiki_dir / rel, new_content, n


def fix_broken_links(wiki_dir: Path, apply: bool):
    """Apply high-confidence broken-link suggestions (≥0.74) across the wiki.

    O(n²) — slow on large wikis. Returns (n_fixed_pages, n_fixed_links).
    """
    from _lint_suggest import run_structural_lint
    # Scan universe = NashSU {index, log}: overview/schema stay valid link
    # targets so real [[overview]] links aren't mis-flagged as broken. The
    # engine exempts aggregates from findings, so overview/schema source
    # pages never get rewritten here.
    pages = list(iter_wiki_pages(wiki_dir, anchor_files=("index.md", "log.md")))
    findings = run_structural_lint(pages, with_suggestions=True)
    broken = [f for f in findings if f["type"] == "broken-link" and f.get("suggested_target")]
    pages_by_rel = {p[0]: wiki_dir / p[0] for p in pages}
    fixed_pages = 0
    fixed_links = 0
    by_page: dict[str, list[dict]] = {}
    for f in broken:
        by_page.setdefault(f["page"], []).append(f)
    for page_rel, finds in by_page.items():
        p = pages_by_rel.get(page_rel)
        if not p or not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        changed = False
        for f in finds:
            broken_tgt = f["broken_target"]
            sug = f["suggested_target"].replace(".md", "")
            pat = re.compile(r"\[\[" + re.escape(broken_tgt) + r"(\|[^\]]+)?\]\]")
            new_text, n = pat.subn(lambda mm: f"[[{sug}{mm.group(1) or ''}]]", text)
            if n:
                text = new_text
                changed = True
                fixed_links += n
        if changed and apply:
            p.write_text(text, encoding="utf-8")
            fixed_pages += 1
    return fixed_pages, fixed_links


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--project", default=None)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--fix-broken", action="store_true",
                    help="also auto-fix high-confidence broken links (O(n²), slow)")
    args = ap.parse_args()
    root = Path(args.project or os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd()))
    wiki = root / "wiki"
    if not wiki.is_dir():
        print(f"ERROR: wiki/ not found under {root}", file=sys.stderr)
        return 2

    mode = "apply" if args.apply else "dry-run"
    pages_changed = 0
    links_added = 0
    for path, new_content, n in scan_wiki(wiki):
        pages_changed += 1
        links_added += n
        if pages_changed <= 10:
            print(f"  {path.relative_to(root)} (+{n} source link)")
        if args.apply:
            path.write_text(new_content, encoding="utf-8")
    if pages_changed > 10:
        print(f"  ... and {pages_changed - 10} more")
    print(f"\n[source-link backfill · {mode}] {pages_changed} page(s), +{links_added} link(s)")

    if args.fix_broken:
        print(f"\n[broken-link fix · {mode}] scanning (O(n²), may take minutes)...")
        fp, fl = fix_broken_links(wiki, apply=args.apply)
        print(f"[broken-link fix · {mode}] {fp} page(s), {fl} link(s) corrected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
