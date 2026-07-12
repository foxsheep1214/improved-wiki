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
  2. Broken-link auto-fix (--fix-broken): applies broken-link corrections at
     or above the shared headless auto-rewrite gate
     (_lint_suggest.BROKEN_LINK_AUTO_REWRITE_MIN_SCORE = 0.9, same as
     wiki-lint-fix.py); lower-scored suggestions are listed for manual review.
     O(n²) over the wiki — slow on large wikis; skip unless needed.
  3. Mention backlink (--mention-orphans): the deterministic core of NashSU
     enrich-wikilinks.ts — a page whose BODY literally mentions an orphan
     page's title (as plain text, not already inside a [[link]]) gets a
     [[wikilink]] to it, giving the orphan an inbound link. Unlike NashSU's
     LLM-suggested enrich, this is exact-title matching only (no LLM), which
     keeps it in the "deterministic, correct" tier of this tool. Scoped to
     orphans + guarded against generic short words to avoid mislinks.

No token-overlap "related" links are auto-added — those stay in wiki-lint.sh's
--fix-links for human-reviewed runs.

Usage:
  IMPROVED_WIKI_ROOT=/path python3 enrich_wikilinks_retroactive.py            # dry-run
  IMPROVED_WIKI_ROOT=/path python3 enrich_wikilinks_retroactive.py --apply
  IMPROVED_WIKI_ROOT=/path python3 enrich_wikilinks_retroactive.py --apply --fix-broken
  IMPROVED_WIKI_ROOT=/path python3 enrich_wikilinks_retroactive.py --apply --mention-orphans
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
from _paths import iter_wiki_pages, WIKI_ARTIFACT_DIRS, atomic_write  # noqa: E402
from _lint_suggest import BROKEN_LINK_AUTO_REWRITE_MIN_SCORE  # noqa: E402
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
    """Apply broken-link suggestions at or above the shared headless
    auto-rewrite gate (_lint_suggest.BROKEN_LINK_AUTO_REWRITE_MIN_SCORE, 0.9 —
    same threshold wiki-lint-fix.py enforces; the old local ≥0.74 let
    contains-tier/fuzzy matches rewrite unattended). Suggestions below the
    gate are PRINTED for manual handling, never rewritten.

    O(n²) — slow on large wikis. Returns (n_fixed_pages, n_fixed_links).
    """
    from _lint_suggest import run_structural_lint
    # Scan universe = NashSU {index, log}: overview/schema stay valid link
    # targets so real [[overview]] links aren't mis-flagged as broken. The
    # engine exempts aggregates from findings, so overview/schema source
    # pages never get rewritten here.
    pages = list(iter_wiki_pages(wiki_dir, anchor_files=("index.md", "log.md")))
    findings = run_structural_lint(pages, with_suggestions=True)
    suggested = [f for f in findings
                 if f["type"] == "broken-link" and f.get("suggested_target")]
    # Gate: a missing score (older engine output) is treated conservatively
    # as below-gate — no unattended rewrite.
    broken = [f for f in suggested
              if (f.get("suggested_score") or 0) >= BROKEN_LINK_AUTO_REWRITE_MIN_SCORE]
    below_gate = [f for f in suggested if f not in broken]
    if below_gate:
        print(f"  [fix-broken] {len(below_gate)} suggestion(s) below the "
              f"auto-rewrite gate ({BROKEN_LINK_AUTO_REWRITE_MIN_SCORE}) — "
              f"left for manual handling:")
        for f in below_gate:
            print(f"    {f['page']}: [[{f['broken_target']}]] → "
                  f"[[{f['suggested_target']}]] (score={f.get('suggested_score')})")
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
            atomic_write(p, text)
            fixed_pages += 1
    return fixed_pages, fixed_links


# Generic short words too ambiguous to trigger a mention backlink (a body
# mention of "ground"/"power" says nothing about relatedness to that page).
_MENTION_STOPWORDS = {
    "ground", "amp", "amps", "power", "current", "voltage", "noise", "gain",
    "filter", "switch", "load", "source", "diode", "transistor", "signal",
    "energy", "phase", "clock", "data", "logic", "pulse", "wave", "band",
}


def mention_backlink_orphans(wiki_dir: Path, apply: bool):
    """Give orphan pages an inbound link from any page whose BODY literally
    mentions their title. Deterministic core of NashSU enrich-wikilinks
    (exact-title match, no LLM). Returns (n_pages, n_links, n_orphans_solved).
    """
    from _lint_suggest import run_structural_lint
    from _lint_fixes import append_wikilink

    pages = list(iter_wiki_pages(wiki_dir, anchor_files=("index.md", "log.md")))
    content_map = {rel: content for rel, content in pages}
    # with_suggestions=False → O(n) orphan detection, skips the O(n²) suggester.
    findings = run_structural_lint(pages, with_suggestions=False)
    orphan_rels = [
        f["page"] for f in findings
        if f["type"] == "orphan" and f["page"].split("/")[0] in ("concepts", "entities")
    ]

    def title_of(text: str, stem: str) -> str:
        m = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', text, re.M)
        return (m.group(1).strip() if m and m.group(1).strip()
                else re.sub(r"[-_]+", " ", stem))

    # Eligible orphans → (slug, stem, compiled word-boundary pattern).
    targets = []
    for rel in orphan_rels:
        slug = rel[:-3] if rel.endswith(".md") else rel
        stem = slug.split("/")[-1]
        name = title_of(content_map.get(rel, ""), stem).strip()
        if len(name) < 6 or name.lower() in _MENTION_STOPWORDS:
            continue
        if " " not in name and len(name) < 8:   # skip bare short single words
            continue
        pat = re.compile(r"(?<![\w\[])" + re.escape(name) + r"(?![\w\]])", re.I)
        targets.append((slug, stem, pat))

    plan: dict[str, set] = {}
    for src_rel, src_text in content_map.items():
        # never append a link INTO an aggregate page (iter_wiki_pages already
        # drops index/log; guard overview/schema too).
        if src_rel.split("/")[-1] in ("overview.md", "schema.md"):
            continue
        src_slug = src_rel[:-3] if src_rel.endswith(".md") else src_rel
        body = src_text.split("\n---\n", 1)[-1] if src_text.startswith("---") else src_text
        for slug, stem, pat in targets:
            if src_slug == slug:
                continue
            if re.search(r"\[\[[^\]]*" + re.escape(stem) + r"[^\]]*\]\]", src_text, re.I):
                continue  # already links to this orphan
            if pat.search(body):
                plan.setdefault(src_rel, set()).add(slug)

    n_pages = n_links = 0
    for src_rel, slugs in sorted(plan.items()):
        content = content_map[src_rel]
        new = content
        added = 0
        for slug in sorted(slugs):
            n2 = append_wikilink(new, slug)
            if n2 != new:
                new = n2
                added += 1
        if new != content:
            n_pages += 1
            n_links += added
            if apply:
                atomic_write(wiki_dir / src_rel, new)
    solved = len({s for slugs in plan.values() for s in slugs})
    return n_pages, n_links, solved


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--project", default=None)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--fix-broken", action="store_true",
                    help="also auto-fix high-confidence broken links (O(n²), slow)")
    ap.add_argument("--mention-orphans", action="store_true",
                    help="give orphan pages an inbound link from any page whose body "
                         "literally mentions their title (deterministic enrich, no LLM)")
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
            atomic_write(path, new_content)
    if pages_changed > 10:
        print(f"  ... and {pages_changed - 10} more")
    print(f"\n[source-link backfill · {mode}] {pages_changed} page(s), +{links_added} link(s)")

    if args.fix_broken:
        print(f"\n[broken-link fix · {mode}] scanning (O(n²), may take minutes)...")
        fp, fl = fix_broken_links(wiki, apply=args.apply)
        print(f"[broken-link fix · {mode}] {fp} page(s), {fl} link(s) corrected")

    if args.mention_orphans:
        print(f"\n[mention backlink · {mode}] scanning for orphan title mentions...")
        mp, ml, ms = mention_backlink_orphans(wiki, apply=args.apply)
        print(f"[mention backlink · {mode}] {mp} page(s), +{ml} link(s), "
              f"{ms} orphan(s) given an inbound link")
    return 0


if __name__ == "__main__":
    sys.exit(main())
