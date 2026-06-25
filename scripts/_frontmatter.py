#!/usr/bin/env python3
"""
_frontmatter.py — Unified frontmatter parse/merge/write utility.

Aligns with NashSU's frontmatter.ts + page-merge.ts v0.4.25.
Three-layer merge: array union → LLM body merge → locked fields.

Usage:
    from _frontmatter import parse_frontmatter, merge_page_content, write_frontmatter

    # Parse
    fm, body = parse_frontmatter(content)

    # Merge (three-layer: array union + LLM body merge + locked fields)
    merged = merge_page_content(new_content, existing_content, merger_fn, opts)

    # Write
    new_text = write_frontmatter({"type": "concept", "title": "Foo", ...}, body)
"""
from __future__ import annotations

import re
import time
from typing import Callable, Optional

# ── Parse ────────────────────────────────────────────────────────────────────

_FM_RE = re.compile(r"^---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)")
_LEADING_FENCE_RE = re.compile(r"^[ \t]*```(?:yaml|md|markdown)?[ \t]*\r?\n")


def _strip_leading_code_fence(content: str) -> str:
    """Read-time fallback: if the whole doc is wrapped in a leading
    ```yaml/```md/```markdown fence, strip the opening fence (and a matching
    trailing fence) so frontmatter can be parsed. Old, already-written corrupt
    files get cleaned up this way; write-time sanitize prevents new ones."""
    open_m = _LEADING_FENCE_RE.match(content)
    if not open_m:
        return content
    after_open = content[open_m.end():]
    close_m = re.search(r"\r?\n[ \t]*```[ \t]*\r?\n?\s*$", after_open)
    if close_m:
        return after_open[: close_m.start()]
    return after_open


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter and body. Returns ({}, body) if no frontmatter.

    Read-time fallback: strips a leading ```yaml/```md/```markdown wrapper so
    legacy corrupt pages still parse (write-time sanitize prevents new ones).
    Fence detection is line-anchored (NashSU frontmatter.ts parity) so a
    mid-body ``---`` or stray ``---`` inside a line can't be mistaken for the
    closing fence.
    """
    if not content.startswith("---"):
        content = _strip_leading_code_fence(content)
    m = _FM_RE.match(content)
    if not m:
        return {}, content
    fm_text = m.group(1).strip()
    body = content[m.end():].lstrip("\n")

    fm = {}
    for line in fm_text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m2 = re.match(r'^([\w_-]+):\s*(.*)', line)
        if m2:
            key, val = m2.group(1), m2.group(2).strip()
            # Array values: [a, b, c]
            if val.startswith("[") and val.endswith("]"):
                inner = val[1:-1].strip()
                if inner:
                    fm[key] = [v.strip().strip("'\"") for v in inner.split(",")]
                else:
                    fm[key] = []
            # Quoted strings
            elif val.startswith('"') and val.endswith('"'):
                fm[key] = val[1:-1]
            elif val.startswith("'") and val.endswith("'"):
                fm[key] = val[1:-1]
            else:
                fm[key] = val
    return fm, body


def _quote_value(value) -> str:
    """Double-quote a YAML scalar/list-item with backslash + quote escaping."""
    s = str(value)
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# Chars that make a YAML scalar unsafe to emit bare. A leading char from the
# first set, any char from the second set anywhere, a space, or empty → quote.
_QUOTE_START_CHARS = set("[{}&*#!|>?-\"'%@`")
_QUOTE_ANY_CHARS = set(":#[]{}\"'")


def _needs_quoting(value: str) -> bool:
    if value == "":
        return True
    if value[0] in _QUOTE_START_CHARS:
        return True
    if " " in value:
        return True
    return any(ch in _QUOTE_ANY_CHARS for ch in value)


def write_frontmatter(fm: dict, body: str) -> str:
    """Serialize frontmatter dict + body to markdown string.

    YAML-safe quoting (MEDIUM-2 fix): list items are always quoted (parity
    with _frontmatter_array._quote_inline_array_value, avoids the
    ``related: [[[a]]]`` triple-bracket corruption); scalars are quoted when
    they contain a YAML-special char or would otherwise parse ambiguously.
    """
    lines = ["---"]
    for key, value in fm.items():
        if isinstance(value, list):
            items = ", ".join(_quote_value(v) for v in value)
            lines.append(f"{key}: [{items}]")
        elif isinstance(value, str) and _needs_quoting(value):
            lines.append(f"{key}: {_quote_value(value)}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + body


# ── Merge ────────────────────────────────────────────────────────────────────

# Fields whose values are UNIONED (not replaced) across re-ingests
UNION_FIELDS = ("sources", "tags", "related")

# Fields whose EXISTING values survive re-ingests unchanged
LOCKED_FIELDS = ("type", "title", "created")

# Body shrink threshold: reject LLM merge if result < 70% of max(old, new)
BODY_SHRINK_THRESHOLD = 0.7


def strip_embedded_images_section(body: str) -> str:
    """Remove the auto-injected ``## Embedded Images`` section from a page body.

    Stage 3.2 appends this section (a table of potentially hundreds of image
    links) to source pages AFTER 3.1 writes them. On re-ingest, the existing
    source page carries this section (often 50K+ chars for a 457-image book)
    while the new (Stage 2.6) body does not. Leaving it in the merge body:
      - inflates ``old_body`` so the merge length threshold (~0.7 * max) jumps
        to tens of KB, while the LLM merge prompt truncates each body to 3K —
        so the LLM output is always "below threshold" and the no-fallback
        policy pauses the ingest (bug 2026-06-25, Hansen source page).
      - prevents the "bodies identical" fast path from firing on a same-book
        re-ingest (the semantic body is unchanged; only the images section
        differs).

    Stripping it before comparison/threshold makes same-book re-ingests hit
    the identical-body fast path (no LLM merge), and 3.2 re-injects images
    afterward regardless. The section is an artifact, not author content.
    """
    marker = "## Embedded Images"
    idx = body.find(marker)
    if idx == -1:
        return body
    return body[:idx].rstrip()


def union_arrays(new_fm: dict, existing_fm: dict) -> dict:
    """Union array fields from both frontmatters. Keeps all other fields from new_fm."""
    merged = dict(new_fm)
    for field in UNION_FIELDS:
        new_vals = new_fm.get(field, [])
        old_vals = existing_fm.get(field, [])
        if not isinstance(new_vals, list):
            new_vals = [new_vals] if new_vals else []
        if not isinstance(old_vals, list):
            old_vals = [old_vals] if old_vals else []
        seen = set()
        union = []
        for v in old_vals + new_vals:
            key = str(v).lower().strip('"').strip("'")
            if key not in seen:
                seen.add(key)
                union.append(v)
        merged[field] = union
    return merged


def merge_array_fields_into_content(new_content: str, existing_content: str) -> str:
    """Union frontmatter array fields (sources/tags/related) from both contents.

    Delegates to _frontmatter_array (NashSU sources-merge.ts port) which
    handles BOTH inline ``[a, b]`` and block-style ``  - a`` arrays. The old
    in-house parser only understood inline form, so a block-style
    ``related:`` from an existing page was silently dropped during re-ingest
    merge. Returns new_content unchanged when existing has no frontmatter.
    """
    from _frontmatter_array import merge_array_fields_into_content as _robust
    return _robust(new_content, existing_content, list(UNION_FIELDS))


def lock_fields(content: str, reference_fm: dict) -> str:
    """Force LOCKED_FIELDS back to reference values."""
    fm, body = parse_frontmatter(content)
    for field in LOCKED_FIELDS:
        if field in reference_fm and reference_fm[field]:
            fm[field] = reference_fm[field]
    return write_frontmatter(fm, body)


def merge_page_content(
    new_content: str,
    existing_content: str | None,
    merger_fn: Optional[Callable] = None,
    *,
    page_path: str = "",
    source_file: str = "",
    backup_fn: Optional[Callable] = None,
) -> str:
    """Three-layer merge matching NashSU page-merge.ts.

    Layer 1: Union frontmatter array fields (always, zero-cost).
    Layer 2: If bodies differ, call merger_fn (LLM) to produce unified body.
    Layer 3: Lock type/title/created to existing values.

    Fallback: if LLM fails or body shrinks below threshold, return
    array-merged-only result with backup.
    """
    # Fast path 1: brand-new page
    if not existing_content:
        return new_content

    # Fast path 2: byte-identical
    if new_content == existing_content:
        return existing_content

    # Layer 1: union array fields
    array_merged = merge_array_fields_into_content(new_content, existing_content)

    # Fast path 3: bodies identical (only frontmatter arrays differed)
    # Strip the auto-injected ## Embedded Images section first: it is an
    # artifact appended by Stage 3.2 (not author content) and re-injected
    # after this merge. Without stripping, a same-book re-ingest never hits
    # this fast path (the existing page has the section, the new does not)
    # and falls through to an LLM merge whose truncated output fails the
    # inflated length threshold (bug 2026-06-25).
    old_body = strip_embedded_images_section(parse_frontmatter(existing_content)[1])
    new_body = strip_embedded_images_section(parse_frontmatter(array_merged)[1])
    if old_body.strip() == new_body.strip():
        return array_merged

    # Fast path 4: bodies identical after stripping [[wikilink]] markup.
    # On a conversation-mode mid-flight resume, the write phase re-runs with the
    # original (pre-enrichment) body while the existing page has been mutated by
    # wikilink enrichment (which only adds [[...]] links).  Stripping wikilink
    # markup from both bodies collapses this enrichment-only difference, avoiding
    # a spurious LLM page-merge round-trip for every already-written page.
    import re as _re
    _wikilink_re = _re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
    def _strip_wikilinks(text: str) -> str:
        return _wikilink_re.sub(lambda m: m.group(2) or m.group(1), text)
    if _strip_wikilinks(old_body).strip() == _strip_wikilinks(new_body).strip():
        # Keep the existing (enriched) body; only adopt frontmatter array unions.
        return write_frontmatter(parse_frontmatter(array_merged)[0], old_body)

    # Fast path 5: idempotent re-merge. If the existing page's `sources:`
    # already includes every source in the new content, this collision was
    # already merged in a prior run — the new body is already incorporated
    # into the existing page. Returning the existing body (with unioned
    # arrays) breaks the re-merge loop: write_phase has no per-file marker
    # (only an all-or-nothing write_phase marker), so a mid-flight crash
    # makes it re-write every file; the now-merged existing content changes
    # the merge prompt hash, the conversation cache misses, and the LLM is
    # asked to re-merge an already-merged page — forever (bug 2026-06-25).
    def _src_set(fm: dict) -> set:
        v = fm.get("sources")
        if not v:
            return set()
        if isinstance(v, str):
            return {v}
        if isinstance(v, list):
            return {str(s) for s in v}
        return set()
    if _src_set(parse_frontmatter(existing_content)[0]).issuperset(
        _src_set(parse_frontmatter(new_content)[0])
    ) and _src_set(parse_frontmatter(new_content)[0]):
        # Keep the existing (already-merged) body; union frontmatter arrays.
        return merge_array_fields_into_content(existing_content, new_content)

    # Layer 2: LLM merge (if merger provided)
    if merger_fn:
        try:
            llm_output = merger_fn(existing_content, array_merged, source_file)
        except Exception as e:
            # No fallback: LLM merge failure means the main path is not working.
            # Pause rather than silently degrading to array-merge-only (which
            # drops the existing body) — policy 2026-06-24.
            if backup_fn:
                try:
                    backup_fn(existing_content)
                except Exception:
                    pass
            raise RuntimeError(
                f"LLM page-merge failed for {page_path} ({type(e).__name__}: {e}). "
                f"No fallback — fix the LLM provider and re-run."
            ) from e

        # Sanity 1: must have frontmatter
        llm_fm, llm_body = parse_frontmatter(llm_output)
        if not llm_fm:
            if backup_fn:
                try:
                    backup_fn(existing_content)
                except Exception:
                    pass
            raise RuntimeError(
                f"LLM page-merge output for {page_path} has no frontmatter — "
                f"rejecting. No fallback, no silent array-merge degradation."
            )

        # Sanity 2: body length
        old_len = len(old_body)
        new_len = len(new_body)
        llm_len = len(llm_body)
        threshold = max(old_len, new_len) * BODY_SHRINK_THRESHOLD
        if llm_len < threshold:
            if backup_fn:
                try:
                    backup_fn(existing_content)
                except Exception:
                    pass
            raise RuntimeError(
                f"LLM page-merge for {page_path} produced {llm_len} chars, below "
                f"threshold {threshold:.0f} — rejecting. No fallback, no silent "
                f"array-merge degradation."
            )

        # Layer 3: lock fields + re-union arrays
        old_fm = parse_frontmatter(existing_content)[0]
        final = lock_fields(llm_output, old_fm)
        final = merge_array_fields_into_content(final, array_merged)
        # Update timestamp
        today = time.strftime("%Y-%m-%d")
        fm, body = parse_frontmatter(final)
        fm["updated"] = today
        return write_frontmatter(fm, body)

    # No merger — return array-merged only
    return array_merged
