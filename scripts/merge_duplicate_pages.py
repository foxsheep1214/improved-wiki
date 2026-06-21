#!/usr/bin/env python3
"""merge_duplicate_pages.py — merge wiki pages that share the same title.

Same-title pages are the same concept (the knowledge base names by title), so
variant slugs produced by non-deterministic LLM path generation are redundant.
This script is the deterministic complement to dedup_sweep.py: it handles
exact-title collisions (language suffixes like -zh, macOS " 2" collisions,
case variants, parenthesized variants) without any LLM call.

  1. Groups concept/entity pages by lowercased `title:` frontmatter.
  2. Picks a canonical slug per group (lowercase-kebab, no language suffix,
     no macOS " 2" collision, no parens, richest body on ties).
  3. Merges non-canonical pages into the canonical one:
       - frontmatter tags/related/sources  → union
       - created/updated                   → earliest/latest
       - body                              → richest (most bytes)
       - title                             → best-cased (the richest file's)
  4. Builds a redirect map (every non-canonical slug → canonical slug) and
     rewrites `[[wikilinks]]` + `related:` arrays across the whole wiki/.
  5. Deletes non-canonical files.

Usage:
  python3 merge_duplicate_pages.py <wiki_root>            # dry-run, print plan
  python3 merge_duplicate_pages.py <wiki_root> --apply    # execute
  python3 merge_duplicate_pages.py <wiki_root> --scope concepts   # limit
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# ── frontmatter / link parsing ──────────────────────────────────────────────

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_TITLE_RE = re.compile(r'^title:\s*["\']?(.+?)["\']?\s*$', re.MULTILINE)
_TAGS_RE = re.compile(r'^tags:\s*\[(.*)\]\s*$', re.MULTILINE)
_RELATED_RE = re.compile(r'^related:\s*\[(.*)\]\s*$', re.MULTILINE)
_SOURCES_RE = re.compile(r'^sources:\s*\[(.*)\]\s*$', re.MULTILINE)
_CREATED_RE = re.compile(r'^created:\s*(\S+)\s*$', re.MULTILINE)
_UPDATED_RE = re.compile(r'^updated:\s*(\S+)\s*$', re.MULTILINE)

_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")
_QSTR_RE = re.compile(r'"([^"]*)"')


def _parse_list(raw: str) -> list[str]:
    """Parse a YAML inline list value: [a, "b", c] → [a, b, c]."""
    if not raw or raw.strip() == "[]":
        return []
    items = _QSTR_RE.findall(raw)
    if items:
        return items
    return [x.strip().strip("'\"") for x in raw.split(",") if x.strip()]


def _dump_list(items: list[str]) -> str:
    """Serialize a list back to a quoted YAML inline list (dedup, order-preserving)."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for it in items:
        k = it.lower()
        if k not in seen_set:
            seen_set.add(k)
            seen.append(it)
    if not seen:
        return "[]"
    parts = []
    for it in seen:
        if re.search(r'[:\[\]\{\},#&\*!\|>\?@"\'`]', it) or " " in it:
            parts.append(json.dumps(it, ensure_ascii=False))
        else:
            parts.append(it)
    return "[" + ", ".join(parts) + "]"


@dataclass
class Page:
    path: Path
    folder: str            # "concepts" | "entities"
    slug: str              # filename without .md
    title: str
    raw_fm: str            # raw frontmatter block (between the --- lines)
    body: str              # content after frontmatter
    size: int
    tags: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    created: str = ""
    updated: str = ""

    @property
    def short_name(self) -> str:
        return f"{self.folder}/{self.slug}"


def load_page(path: Path, folder: str) -> Page | None:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    m = _FM_RE.match(text)
    if not m:
        return None
    fm, body = m.group(1), m.group(2)
    tm = _TITLE_RE.search(fm)
    if not tm:
        return None
    title = tm.group(1).strip()
    slug = path.stem
    def g(rx, default=""):
        mm = rx.search(fm)
        return mm.group(1).strip() if mm else default
    return Page(
        path=path, folder=folder, slug=slug, title=title,
        raw_fm=fm, body=body, size=len(text),
        tags=_parse_list(g(_TAGS_RE)), related=_parse_list(g(_RELATED_RE)),
        sources=_parse_list(g(_SOURCES_RE)), created=g(_CREATED_RE),
        updated=g(_UPDATED_RE),
    )


# ── canonical selection ────────────────────────────────────────────────────

_LANG_SUFFIX_RE = re.compile(r"-(zh|en|ja|ko|fr|de|es|ru|ar)$")
_DOMAIN_SUFFIXES = {
    "emc", "pcb", "rf", "emi", "mcu", "fpga", "asic", "soc", "dsp",
    "adc", "dac", "pll", "ddr", "pcie", "usb", "analog", "power",
}


def canonical_score(page: Page) -> tuple:
    """Higher is more canonical. Returns a sort key (max() picks highest)."""
    slug = page.slug
    score = 0.0
    if _LANG_SUFFIX_RE.search(slug):          score -= 1000   # -zh/-en dup
    if re.search(r"\s\d+$", slug):            score -= 1000   # macOS " 2"
    if "(" in slug or ")" in slug:            score -= 500    # parenthesized
    if " " in slug:                           score -= 100    # space in slug (collision-prone)
    if slug != slug.lower():                  score -= 200    # case variant
    if "_" in slug:                           score -= 30     # underscore slug (prefer hyphen)
    parts = slug.split("-")
    if len(parts) > 1 and parts[-1].lower() in _DOMAIN_SUFFIXES:
        score -= 50                                            # domain-disambiguator suffix
    score -= min(len(slug), 80) * 0.1                          # prefer shorter
    score += page.size * 0.001                                 # prefer richer body
    return (score, page.size)


def pick_canonical(pages: list[Page]) -> Page:
    return max(pages, key=canonical_score)


# ── merge ──────────────────────────────────────────────────────────────────

def _norm_link(s: str) -> str:
    s = s.strip()
    s = re.sub(r"\.md$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^wiki/", "", s, flags=re.IGNORECASE)
    return s.strip().lower()


def _dedup(items: list[str]) -> list[str]:
    seen: list[str] = []
    s: set[str] = set()
    for it in items:
        k = it.lower()
        if k not in s:
            s.add(k)
            seen.append(it)
    return seen


def merge_group(pages: list[Page], canonical: Page) -> Page:
    """Merge all pages into canonical (in place). Returns canonical."""
    all_tags: list[str] = []
    all_related: list[str] = []
    all_sources: list[str] = []
    richest = max(pages, key=lambda p: p.size)
    earliest = min((p.created for p in pages if p.created), default="")
    latest = max((p.updated for p in pages if p.updated), default="")

    sibling_keys = {_norm_link(x.short_name) for x in pages if x is not canonical}
    for p in pages:
        all_tags.extend(p.tags)
        all_sources.extend(p.sources)
        for r in p.related:
            # drop refs to non-canonical siblings (they'd be self-refs after merge)
            if _norm_link(r) in sibling_keys:
                continue
            all_related.append(r)

    canonical.tags = _dedup(all_tags)
    canonical.sources = _dedup(all_sources)
    canonical.related = _dedup(all_related)
    canonical.created = earliest or canonical.created
    canonical.updated = latest or canonical.updated
    canonical.body = richest.body
    canonical.title = richest.title
    return canonical


# ── serialization ──────────────────────────────────────────────────────────

def serialize(page: Page) -> str:
    """Rebuild frontmatter with merged fields, preserving unhandled fields."""
    fm = page.raw_fm
    # title: quote if it contains YAML-special chars
    if any(c in page.title for c in '":[]{}'):
        title_line = f'title: "{page.title}"'
    else:
        title_line = f"title: {page.title}"
    fm = _TITLE_RE.sub(lambda _m: title_line, fm, count=1)
    fm = _TAGS_RE.sub(f"tags: {_dump_list(page.tags)}", fm, count=1)
    fm = _RELATED_RE.sub(f"related: {_dump_list(page.related)}", fm, count=1)
    fm = _SOURCES_RE.sub(f"sources: {_dump_list(page.sources)}", fm, count=1)
    if page.created:
        fm = _CREATED_RE.sub(f"created: {page.created}", fm, count=1)
    if page.updated:
        fm = _UPDATED_RE.sub(f"updated: {page.updated}", fm, count=1)
    return f"---\n{fm}\n---\n{page.body}"


# ── wikilink rewrite ───────────────────────────────────────────────────────

def build_redirect_index(redirects: dict[str, str]) -> dict[str, str]:
    """redirects: {non_canon_short_name: canon_short_name}.
    Returns lookup keyed by every normalized form of the old slug."""
    index: dict[str, str] = {}
    for old, new in redirects.items():
        old_folder, old_slug = old.split("/", 1)
        new_folder, new_slug = new.split("/", 1)
        index[_norm_link(old)] = new            # full form concepts/磁畴-zh
        index[_norm_link(old_slug)] = new_slug  # basename form 磁畴-zh
    return index


def rewrite_links_in_text(text: str, index: dict[str, str]) -> tuple[str, int]:
    n = 0
    def repl(m: re.Match) -> str:
        nonlocal n
        target = m.group(1)
        display = m.group(2)
        norm = _norm_link(target)
        if norm in index:
            n += 1
            new_target = index[norm]
            return f"[[{new_target}|{display}]]" if display else f"[[{new_target}]]"
        return m.group(0)
    text = _WIKILINK_RE.sub(repl, text)

    def rel_repl(m: re.Match) -> str:
        nonlocal n
        items = _parse_list(m.group(1))
        new_items = []
        for it in items:
            norm = _norm_link(it)
            if norm in index:
                new_items.append(index[norm])
                n += 1
            else:
                new_items.append(it)
        return f"related: {_dump_list(new_items)}"
    text = _RELATED_RE.sub(rel_repl, text)
    return text, n


# ── main ───────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wiki_root")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--scope", nargs="*", default=["concepts", "entities"])
    args = ap.parse_args()
    wiki = Path(args.wiki_root) / "wiki"
    if not wiki.is_dir():
        print(f"ERROR: {wiki} not found", file=sys.stderr)
        return 2

    by_title: dict[tuple[str, str], list[Page]] = defaultdict(list)
    for folder in args.scope:
        d = wiki / folder
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            page = load_page(p, folder)
            if page:
                by_title[(folder, page.title.strip().lower())].append(page)

    groups = [g for g in by_title.values() if len(g) > 1]
    total_redundant = sum(len(g) - 1 for g in groups)
    print(f"=== {'APPLY' if args.apply else 'DRY RUN'} ===")
    print(f"groups: {len(groups)}  redundant files: {total_redundant}")

    redirects: dict[str, str] = {}
    merged_canonicals: list[Page] = []
    for g in groups:
        canon = pick_canonical(g)
        merged = merge_group(list(g), canon)
        merged_canonicals.append(merged)
        for p in g:
            if p.short_name != canon.short_name:
                redirects[p.short_name] = canon.short_name

    print(f"\n--- plan sample (first 25 groups) ---")
    for g in groups[:25]:
        canon = pick_canonical(g)
        others = [p.slug for p in g if p.short_name != canon.short_name]
        print(f"  keep {canon.short_name:55s}  ← {', '.join(others)}")
    print(f"  ... ({len(groups)} groups total, {total_redundant} files to delete)")

    if not args.apply:
        print("\n(dry-run; pass --apply to execute)")
        return 0

    for page in merged_canonicals:
        page.path.write_text(serialize(page), encoding="utf-8")
    print(f"\nwrote {len(merged_canonicals)} merged canonical pages")

    index = build_redirect_index(redirects)
    print(f"redirect index entries: {len(index)}")
    total_rewrites = 0
    files_touched = 0
    for md in wiki.rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8")
        except Exception:
            continue
        new_text, n = rewrite_links_in_text(text, index)
        if n > 0:
            md.write_text(new_text, encoding="utf-8")
            total_rewrites += n
            files_touched += 1
    print(f"rewrote {total_rewrites} link references across {files_touched} files")

    deleted = 0
    for old_short, _ in redirects.items():
        folder, slug = old_short.split("/", 1)
        target = wiki / folder / f"{slug}.md"
        if target.exists():
            target.unlink()
            deleted += 1
    print(f"deleted {deleted} non-canonical files")
    print("\n=== DONE ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
