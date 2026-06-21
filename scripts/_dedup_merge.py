#!/usr/bin/env python3
"""_dedup_merge.py — deterministic phase-1 dedup engine.

Merges wiki pages that share the same ``title:`` frontmatter (case-insensitive).
Same-title pages are the same concept; variant slugs (-zh language suffix,
macOS " 2" collisions, case variants, parenthesized variants, underscore slugs)
are produced by non-deterministic LLM path generation and are redundant.

This is the deterministic complement to ``_dedup`` (NashSU's LLM semantic
dedup). It needs no LLM and runs in seconds. ``cross_source_dedup.py`` runs phase 1
(this module) before phase 2 (LLM semantic) so obvious variant duplicates are
cheaply removed before spending LLM calls on synonym / cross-language pairs.

Public entry point: ``run(wiki_root, apply=True, scope=...)`` → report dict.
"""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_TITLE_RE = re.compile(r'^title:\s*["\']?(.+?)["\']?\s*$', re.MULTILINE)
_TAGS_RE = re.compile(r'^tags:\s*\[(.*)\]\s*$', re.MULTILINE)
_RELATED_RE = re.compile(r'^related:\s*\[(.*)\]\s*$', re.MULTILINE)
_SOURCES_RE = re.compile(r'^sources:\s*\[(.*)\]\s*$', re.MULTILINE)
_CREATED_RE = re.compile(r'^created:\s*(\S+)\s*$', re.MULTILINE)
_UPDATED_RE = re.compile(r'^updated:\s*(\S+)\s*$', re.MULTILINE)
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")
_QSTR_RE = re.compile(r'"([^"]*)"')

LANG_SUFFIX_RE = re.compile(r"-(zh|en|ja|ko|fr|de|es|ru|ar)$")
DOMAIN_SUFFIXES = {
    "emc", "pcb", "rf", "emi", "mcu", "fpga", "asic", "soc", "dsp",
    "adc", "dac", "pll", "ddr", "pcie", "usb", "analog", "power",
}


def _parse_list(raw: str) -> list[str]:
    if not raw or raw.strip() == "[]":
        return []
    items = _QSTR_RE.findall(raw)
    if items:
        return items
    return [x.strip().strip("'\"") for x in raw.split(",") if x.strip()]


def _dump_list(items: list[str]) -> str:
    seen: list[str] = []
    s: set[str] = set()
    for it in items:
        k = it.lower()
        if k not in s:
            s.add(k)
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
    folder: str
    slug: str
    title: str
    raw_fm: str
    body: str
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


def canonical_score(page: Page) -> tuple:
    """Higher is more canonical. max() picks highest."""
    slug = page.slug
    score = 0.0
    if LANG_SUFFIX_RE.search(slug):          score -= 1000
    if re.search(r"\s\d+$", slug):           score -= 1000
    if "(" in slug or ")" in slug:           score -= 500
    if " " in slug:                          score -= 100
    if slug != slug.lower():                 score -= 200
    if "_" in slug:                          score -= 30
    parts = slug.split("-")
    if len(parts) > 1 and parts[-1].lower() in DOMAIN_SUFFIXES:
        score -= 50
    score -= min(len(slug), 80) * 0.1
    score += page.size * 0.001
    return (score, page.size)


def pick_canonical(pages: list[Page]) -> Page:
    return max(pages, key=canonical_score)


def _norm_link(s: str) -> str:
    s = s.strip()
    s = re.sub(r"\.md$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^wiki:", "", s, flags=re.IGNORECASE)
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


def write_dedup_audit_record(wiki: Path, discarded: Page, kept_short_name: str) -> None:
    """Log what merge_group() is about to throw away before its file is unlinked.

    merge_group() keeps only the richest page's body verbatim — same-title
    pages aren't guaranteed to be content supersets of each other, so this is
    a recovery record (human-reviewable), not a guarantee the merge was
    lossless.
    """
    audit_dir = wiki / "REVIEW" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    safe_slug = re.sub(r"[^\w-]", "-", discarded.slug)[:60]
    out = audit_dir / f"{time.strftime('%Y-%m-%d')}-dedup-merge-{safe_slug}.md"
    out.write_text(
        "---\n"
        "type: audit\n"
        "source: dedup-merge\n"
        f"date: {time.strftime('%Y-%m-%d')}\n"
        "---\n\n"
        f"## Discarded duplicate: {discarded.short_name}\n\n"
        f"Merged into: [[{kept_short_name}]]\n\n"
        f"Title: {discarded.title}\n\n"
        "Body content discarded by phase-1 dedup (richest-page-wins — not "
        "guaranteed lossless; review before assuming nothing of value was lost):\n\n"
        "```markdown\n"
        f"{discarded.body}\n"
        "```\n",
        encoding="utf-8",
    )


def serialize(page: Page) -> str:
    fm = page.raw_fm
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


def build_redirect_index(redirects: dict[str, str]) -> dict[str, str]:
    index: dict[str, str] = {}
    for old, new in redirects.items():
        old_slug = old.split("/", 1)[1]
        new_slug = new.split("/", 1)[1]
        for f in ("concepts", "entities"):
            index[f"{f}/{old_slug.lower()}"] = new
        index[old_slug.lower()] = new_slug
    return index


def rewrite_links_in_text(text: str, index: dict[str, str]) -> tuple[str, int]:
    n = [0]

    def repl(m: re.Match) -> str:
        tgt, disp = m.group(1), m.group(2)
        key = _norm_link(tgt)
        if key in index:
            n[0] += 1
            new = index[key]
            return f"[[{new}|{disp}]]" if disp else f"[[{new}]]"
        return m.group(0)
    text = _WIKILINK_RE.sub(repl, text)

    def rel_repl(m: re.Match) -> str:
        items = _parse_list(m.group(1))
        out = []
        for it in items:
            key = _norm_link(it)
            if key in index:
                out.append(index[key]); n[0] += 1
            else:
                out.append(it)
        return f"related: {_dump_list(out)}"
    text = _RELATED_RE.sub(rel_repl, text)
    return text, n[0]


def run(wiki_root: Path, *, apply: bool = True,
        scope: list[str] | None = None) -> dict:
    """Run deterministic phase-1 dedup. I/O only when apply=True."""
    wiki = wiki_root / "wiki"
    scope = scope or ["concepts", "entities"]

    by_title: dict[tuple[str, str], list[Page]] = defaultdict(list)
    for folder in scope:
        d = wiki / folder
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            page = load_page(p, folder)
            if page:
                by_title[(folder, page.title.strip().lower())].append(page)

    groups = [g for g in by_title.values() if len(g) > 1]
    redundant = sum(len(g) - 1 for g in groups)

    redirects: dict[str, str] = {}
    discarded_pages: dict[str, Page] = {}
    merged_canonicals: list[Page] = []
    for g in groups:
        canon = pick_canonical(g)
        merged = merge_group(list(g), canon)
        merged_canonicals.append(merged)
        for p in g:
            if p.short_name != canon.short_name:
                redirects[p.short_name] = canon.short_name
                discarded_pages[p.short_name] = p

    report: dict = {
        "groups": len(groups),
        "redundant": redundant,
        "deleted": 0,
        "rewrites": 0,
        "files_touched": 0,
        "apply": apply,
        "samples": [
            {"keep": pick_canonical(g).short_name,
             "merge": [p.slug for p in g if p.short_name != pick_canonical(g).short_name]}
            for g in groups[:20]
        ],
    }

    if not apply or not groups:
        return report

    for page in merged_canonicals:
        page.path.write_text(serialize(page), encoding="utf-8")

    index = build_redirect_index(redirects)
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

    deleted = 0
    for old_short, kept_short in redirects.items():
        folder, slug = old_short.split("/", 1)
        target = wiki / folder / f"{slug}.md"
        if target.exists():
            discarded = discarded_pages.get(old_short)
            if discarded is not None:
                write_dedup_audit_record(wiki, discarded, kept_short)
            target.unlink()
            deleted += 1

    report["deleted"] = deleted
    report["rewrites"] = total_rewrites
    report["files_touched"] = files_touched
    return report
