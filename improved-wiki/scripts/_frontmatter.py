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

def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter and body. Returns ({}, body) if no frontmatter."""
    if not content.startswith("---"):
        return {}, content
    fm_end = content.find("---", 3)
    if fm_end == -1:
        return {}, content
    fm_text = content[3:fm_end].strip()
    body = content[fm_end + 3:].lstrip("\n")

    fm = {}
    for line in fm_text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^([\w_-]+):\s*(.*)', line)
        if m:
            key, val = m.group(1), m.group(2).strip()
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


def write_frontmatter(fm: dict, body: str) -> str:
    """Serialize frontmatter dict + body to markdown string."""
    lines = ["---"]
    for key, value in fm.items():
        if isinstance(value, list):
            items = ", ".join(f'"{v}"' if " " in str(v) else str(v) for v in value)
            lines.append(f"{key}: [{items}]")
        elif isinstance(value, str) and (" " in value or ":" in value):
            lines.append(f'{key}: "{value}"')
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
    """Union frontmatter array fields from both contents. Returns merged content."""
    new_fm, new_body = parse_frontmatter(new_content)
    existing_fm, _ = parse_frontmatter(existing_content)
    merged_fm = union_arrays(new_fm, existing_fm)
    return write_frontmatter(merged_fm, new_body)


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
    old_body = parse_frontmatter(existing_content)[1]
    new_body = parse_frontmatter(array_merged)[1]
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
