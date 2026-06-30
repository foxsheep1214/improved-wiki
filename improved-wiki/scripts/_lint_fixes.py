#!/usr/bin/env python3
"""_lint_fixes.py — auto-fixes for structural lint findings.

Faithful port of NashSU ``src/lib/lint-fixes.ts``. Three fixes that
``_lint_suggest.run_structural_lint`` surfaces suggestions for but the old
improved-wiki never applied:

  - append_wikilink          — add ``- [[target]]`` under ``## Related``
                               (for orphan / no-outlinks suggestions).
  - rewrite_wikilink_target  — rewrite a broken ``[[broken]]`` link to its
                               suggested target, preserving any ``|alias``.
  - ensure_broken_link_stub  — create a ``type: query`` stub page for a broken
                               link target that has no suggestion, so the link
                               resolves instead of dangling.

Pure string/path logic — no LLM, no I/O except ``ensure_broken_link_stub``
which writes the stub file. ``make_query_slug`` is a port of NashSU
``wiki-filename.ts:makeQuerySlug`` (NFKC + Unicode-aware, keeps CJK).
"""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

__all__ = [
    "make_query_slug",
    "lint_link_target",
    "has_wikilink_to_target",
    "append_wikilink",
    "rewrite_wikilink_target",
    "stub_relative_path_from_broken_target",
    "stub_title_from_broken_target",
    "ensure_broken_link_stub",
    "normalize_wiki_ref_key",
    "build_deleted_keys",
    "extract_frontmatter_title",
    "clean_index_listing",
    "strip_deleted_wikilinks",
]


# ── slug (port of wiki-filename.ts:makeQuerySlug) ────────────────────────────

# Strip everything that is NOT a Unicode letter, Unicode digit, or ASCII hyphen.
# NashSU makeQuerySlug uses /[^\p{L}\p{N}-]/gu which STRIPS underscores; Python's
# \w would KEEP '_' (it includes the connector-punctuation class), so we must not
# use \w here. We approximate \p{L}\p{N} with str.isalnum() per-character below,
# because Python's `re` has no \p{…} property escapes.
def _is_slug_char(ch: str) -> bool:
    return ch == "-" or ch.isalnum()


def make_query_slug(title: str) -> str:
    """Unicode-aware kebab slug. Keeps letters/digits across all scripts
    (Latin, CJK, Cyrillic …) plus ASCII hyphen. Underscores are stripped
    (matching NashSU ``/[^\\p{L}\\p{N}-]/gu``). NFKC-normalized, lowercased,
    whitespace→hyphen, runs collapsed, trimmed, truncated to 50 chars (by
    codepoint). Falls back to ``"query"`` when nothing usable remains.
    """
    slug = unicodedata.normalize("NFKC", title).strip()
    slug = re.sub(r"\s+", "-", slug)
    slug = "".join(ch for ch in slug if _is_slug_char(ch))
    slug = re.sub(r"-+", "-", slug).strip("-").lower()
    truncated = slug[:50]
    return truncated if truncated else "query"


# ── link target normalization ────────────────────────────────────────────────

def lint_link_target(target: str) -> str:
    """Normalize a wikilink target to a wiki-relative slug form (port of
    lint-fixes.ts:lintLinkTarget). Strips a leading ``wiki/`` and a trailing
    ``.md``, trims whitespace. Also strips surrounding quotes that leak from
    YAML-formatted related fields (e.g. [[concepts/foo"]] or [["concepts/foo"]])."""
    t = target.replace("\\", "/")
    t = re.sub(r"^wiki/", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\.md$", "", t, flags=re.IGNORECASE)
    return t.strip().strip('"').strip("'")


def _normalized_link_target(target: str) -> str:
    return lint_link_target(target).lower()


_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]")
_WIKILINK_WITH_ALIAS_RE = re.compile(r"\[\[([^\]|]+?)(\|[^\]]+?)?\]\]")


def has_wikilink_to_target(content: str, target: str) -> bool:
    """True if ``content`` already contains a ``[[target]]`` (or
    ``[[target|alias]]``) link to ``target`` (case-insensitive)."""
    normalized = _normalized_link_target(target)
    return any(
        _normalized_link_target(m.group(1)) == normalized
        for m in _WIKILINK_RE.finditer(content)
    )


# ── fix 1: append a wikilink under ## Related ────────────────────────────────

_RELATED_HEADING_RE = re.compile(r"^##\s+Related\s*$", re.IGNORECASE | re.MULTILINE)


def append_wikilink(content: str, target: str) -> str:
    """Append ``- [[target]]`` under a ``## Related`` heading. Creates the
    heading if absent. No-op (returns content unchanged) if a link to target
    already exists."""
    link_target = lint_link_target(target)
    if has_wikilink_to_target(content, link_target):
        return content
    link_line = f"- [[{link_target}]]"
    m = _RELATED_HEADING_RE.search(content)
    if m:
        insert_at = m.end()
        return f"{content[:insert_at]}\n{link_line}{content[insert_at:]}"
    return f"{content.rstrip()}\n\n## Related\n{link_line}\n"


# ── fix 2: rewrite a broken link target ──────────────────────────────────────

def rewrite_wikilink_target(
    content: str,
    broken_target: str,
    suggested_target: str,
) -> str:
    """Rewrite every ``[[broken]]`` (or ``[[broken|alias]]``) link to
    ``[[suggested]]`` (preserving alias). Other links are untouched."""
    broken = _normalized_link_target(broken_target)
    replacement = lint_link_target(suggested_target)

    def _sub(m: re.Match) -> str:
        raw_target = m.group(1)
        alias = m.group(2)
        if _normalized_link_target(raw_target) != broken:
            return m.group(0)
        return f"[[{replacement}{alias}]]" if alias is not None else f"[[{replacement}]]"

    return _WIKILINK_WITH_ALIAS_RE.sub(_sub, content)


# ── fix 3: stub page for an unresolvable broken link ─────────────────────────

def stub_relative_path_from_broken_target(broken_target: str) -> str:
    """Wiki-relative path (``queries/<slug>.md`` or nested) for a stub page
    that would satisfy ``[[broken_target]]``."""
    normalized = lint_link_target(broken_target)
    parts = [make_query_slug(p) for p in normalized.split("/") if p]
    if len(parts) > 1:
        rel = "/".join(parts)
    else:
        rel = f"queries/{parts[0] if parts else 'missing-page'}"
    return f"{rel}.md"


def stub_title_from_broken_target(broken_target: str) -> str:
    name = os.path.basename(lint_link_target(broken_target))
    return re.sub(r"[-_]+", " ", name).strip() or "Missing Page"


def ensure_broken_link_stub(
    project_path: str | Path,
    broken_target: str,
) -> tuple[Path, str, bool]:
    """Create a ``type: query`` stub page for ``broken_target`` if it doesn't
    exist. Returns ``(full_path, relative_path, created)``."""
    relative_path = stub_relative_path_from_broken_target(broken_target)
    full_path = Path(project_path) / "wiki" / relative_path
    if full_path.exists():
        return full_path, relative_path, False
    full_path.parent.mkdir(parents=True, exist_ok=True)
    title = stub_title_from_broken_target(broken_target)
    # UTC date — NashSU ensureBrokenLinkStub uses new Date().toISOString().slice(0,10),
    # i.e. UTC, not local. time.strftime() would use local time and drift across
    # timezones, so we read UTC explicitly.
    from datetime import datetime, timezone
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    safe_title = title.replace('"', '\\"')
    content = (
        "---\n"
        "type: query\n"
        f'title: "{safe_title}"\n'
        f"created: {date}\n"
        f"updated: {date}\n"
        "tags: [stub, lint]\n"
        "related: []\n"
        "sources: []\n"
        "---\n\n"
        f"# {title}\n\n"
        "Created by Wiki Lint as a placeholder for a missing wikilink target.\n"
    )
    full_path.write_text(content, encoding="utf-8")
    return full_path, relative_path, True


# ── cascade-delete cleanup helpers (port of wiki-cleanup.ts) ─────────────────
# Pure string-level helpers used by wiki-lint-fix.py's --delete-orphans cascade.
# Faithful ports of NashSU src/lib/wiki-cleanup.ts:
#   normalizeWikiRefKey, buildDeletedKeys, extractFrontmatterTitle,
#   cleanIndexListing, stripDeletedWikilinks.

_TITLE_RE = re.compile(r"^title:\s*[\"']?(.+?)[\"']?\s*$", re.MULTILINE)
# `- [[Target]] description` / `* [[T|D]]` — the primary wikilink of a list item.
_INDEX_ENTRY_RE = re.compile(r"^\s*[-*]\s*\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]")
# [[target]] or [[target|display]] anywhere in body prose.
_WIKILINK_BODY_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+))?\]\]")
_REFKEY_STRIP_RE = re.compile(r"[\s\-_]+")


def normalize_wiki_ref_key(s: str) -> str:
    """Canonicalise a wiki ref so lookups ignore case and the
    space/hyphen/underscore boundary (port of wiki-cleanup.ts:normalizeWikiRefKey).
    Strips path prefixes and a trailing ``.md``. E.g. ``KV Cache``,
    ``kv-cache``, ``kv_cache`` and ``wiki/concepts/kv-cache.md`` all collapse
    to ``kvcache``."""
    normalized = s.strip().replace("\\", "/")
    leaf = normalized.split("/")[-1] if normalized else normalized
    without_md = leaf[:-3] if leaf.lower().endswith(".md") else leaf
    return _REFKEY_STRIP_RE.sub("", without_md.lower())


def build_deleted_keys(infos):
    """Build a set of normalized keys for a batch of (slug, title) deletions —
    BOTH slug-form and title-form (port of wiki-cleanup.ts:buildDeletedKeys).
    ``infos`` is an iterable of ``(slug, title)`` tuples."""
    keys = set()
    for slug, title in infos:
        if slug:
            keys.add(normalize_wiki_ref_key(slug))
        if title:
            keys.add(normalize_wiki_ref_key(title))
    return keys


def extract_frontmatter_title(content: str) -> str:
    """Extract the ``title:`` value from YAML-ish frontmatter, tolerating
    optional single/double quotes (port of wiki-cleanup.ts:extractFrontmatterTitle).
    Returns ``""`` when no title line is found."""
    m = _TITLE_RE.search(content)
    return m.group(1).strip() if m else ""


def clean_index_listing(text: str, deleted_keys) -> str:
    """Drop list-item lines from an index-style file when their primary
    wikilink targets a deleted page (port of wiki-cleanup.ts:cleanIndexListing).
    Anchored to wikilink structure, not substring matching."""
    if not deleted_keys:
        return text

    def _keep(line: str) -> bool:
        m = _INDEX_ENTRY_RE.match(line)
        if not m:
            return True
        return normalize_wiki_ref_key(m.group(1).strip()) not in deleted_keys

    return "\n".join(line for line in text.split("\n") if _keep(line))


def strip_deleted_wikilinks(text: str, deleted_keys) -> str:
    """Replace wikilinks pointing to deleted pages with plain text, leaving
    links to surviving pages alone (port of wiki-cleanup.ts:stripDeletedWikilinks).
    ``[[deleted]]`` → ``deleted``; ``[[deleted|display]]`` → ``display``;
    ``[[kept]]`` unchanged."""
    if not deleted_keys:
        return text

    def _sub(m):
        target = m.group(1)
        display = m.group(2)
        if normalize_wiki_ref_key(target.strip()) not in deleted_keys:
            return m.group(0)
        return display if display is not None else target

    return _WIKILINK_BODY_RE.sub(_sub, text)
