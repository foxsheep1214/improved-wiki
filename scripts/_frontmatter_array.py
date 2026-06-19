#!/usr/bin/env python3
"""_frontmatter_array.py — frontmatter array-field parse / write / union.

Faithful port of NashSU `src/lib/sources-merge.ts` (v0.4.25). The skill's
existing `_frontmatter.parse_frontmatter` only understands the INLINE array
form (`name: [a, b]`); this module also handles the BLOCK form
(`name:\n  - a\n  - b`), which the dedup subsystem (`_dedup.py`) and the
`related:` rewrite path depend on.

Public API mirrors sources-merge.ts:
  - parse_frontmatter_array(content, field)        -> list[str]
  - write_frontmatter_array(content, field, vals)  -> str
  - merge_array_fields_into_content(new, existing, fields) -> str
  - merge_lists(existing, incoming)                -> list[str]
  - parse_sources / write_sources / merge_sources_lists / merge_sources_into_content
"""
from __future__ import annotations

import re

__all__ = [
    "parse_frontmatter_array",
    "write_frontmatter_array",
    "merge_array_fields_into_content",
    "merge_lists",
    "parse_sources",
    "write_sources",
    "merge_sources_lists",
    "merge_sources_into_content",
]

# Frontmatter block: leading `---\n` ... `\n---`. DOTALL so the body spans lines.
_FM_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_FM_REPLACE_RE = re.compile(r"^(---\n)(.*?)(\n---)", re.DOTALL)


def _escape(name: str) -> str:
    return re.escape(name)


def parse_frontmatter_array(content: str, field_name: str) -> list[str]:
    """Extract a frontmatter array field. Handles inline (`name: [a, b]`) and
    block (`name:\n  - a`) forms; strips quotes. Returns [] when absent,
    malformed, or the content has no frontmatter.
    """
    fm_match = _FM_RE.match(content)
    if not fm_match:
        return []
    fm = fm_match.group(1)
    escaped = _escape(field_name)

    block_re = re.compile(
        rf"^{escaped}:\s*\n((?:[ \t]+-\s+.+\n?)+)",
        re.MULTILINE,
    )
    block = block_re.search(fm)
    if block:
        out: list[str] = []
        for line in block.group(1).split("\n"):
            m = re.match(r"^\s+-\s+[\"']?(.+?)[\"']?\s*$", line)
            if m and m.group(1):
                out.append(m.group(1).strip())
        return out

    inline_re = re.compile(rf"^{escaped}:\s*\[([^\]]*)\]", re.MULTILINE)
    inline = inline_re.search(fm)
    if not inline:
        return []
    body = inline.group(1).strip()
    if body == "":
        return []
    return _split_inline_array(body)


def _split_inline_array(body: str) -> list[str]:
    """Comma-split that respects single/double quotes (and backslash escapes
    inside double quotes), matching sources-merge.ts splitInlineArray."""
    out: list[str] = []
    current = ""
    quote: str | None = None
    escaped = False

    for ch in body:
        if escaped:
            current += ch
            escaped = False
            continue
        if quote == '"' and ch == "\\":
            escaped = True
            continue
        if ch in ('"', "'") and quote is None:
            quote = ch
            continue
        if quote == ch:
            quote = None
            continue
        if ch == "," and quote is None:
            value = current.strip()
            if value:
                out.append(value)
            current = ""
            continue
        current += ch

    value = current.strip()
    if value:
        out.append(value)
    return out


def _quote_inline_array_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_frontmatter_array(content: str, field_name: str, values: list[str]) -> str:
    """Rewrite (or insert) a frontmatter array field, preserving all other
    lines and field order. Always emits the inline form. Returns content
    unchanged when there is no frontmatter at all.
    """
    fm_match = _FM_REPLACE_RE.match(content)
    if not fm_match:
        return content

    open_delim, fm_body, close_delim = fm_match.group(1), fm_match.group(2), fm_match.group(3)
    escaped = _escape(field_name)
    serialized = ", ".join(_quote_inline_array_value(v) for v in values)
    new_line = f"{field_name}: [{serialized}]"
    rest = content[fm_match.end():]

    inline_re = re.compile(rf"^{escaped}:\s*\[[^\]]*\]", re.MULTILINE)
    if inline_re.search(fm_body):
        rewritten = inline_re.sub(lambda _m: new_line, fm_body, count=1)
        return f"{open_delim}{rewritten}{close_delim}{rest}"

    block_re = re.compile(
        rf"^{escaped}:\s*\n((?:[ \t]+-\s+.+\n?)+)",
        re.MULTILINE,
    )
    if block_re.search(fm_body):
        rewritten = block_re.sub(lambda _m: new_line, fm_body, count=1)
        return f"{open_delim}{rewritten}{close_delim}{rest}"

    # Field absent — append at end of frontmatter.
    rewritten = f"{fm_body}\n{new_line}"
    return f"{open_delim}{rewritten}{close_delim}{rest}"


def merge_lists(existing: list[str], incoming: list[str]) -> list[str]:
    """Union two lists, case-insensitive dedup, first-seen casing wins."""
    seen: set[str] = set()
    out: list[str] = []
    for s in list(existing) + list(incoming):
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def merge_array_fields_into_content(
    new_content: str,
    existing_content: str | None,
    fields: list[str],
) -> str:
    """For each named field, union the existing-on-disk value with the new
    value and rewrite new_content's frontmatter. Returns new_content verbatim
    when existing is empty / has no frontmatter / nothing changes.
    """
    if not existing_content:
        return new_content
    if not re.match(r"^---\n", existing_content):
        return new_content

    result = new_content
    changed = False
    for field in fields:
        old_values = parse_frontmatter_array(existing_content, field)
        if len(old_values) == 0:
            continue  # field absent in existing → nothing to preserve
        new_values = parse_frontmatter_array(result, field)
        merged = merge_lists(old_values, new_values)
        if len(merged) == len(new_values) and all(
            s == new_values[i] for i, s in enumerate(merged)
        ):
            continue  # no-op for this field
        result = write_frontmatter_array(result, field, merged)
        changed = True
    return result if changed else new_content


# ─── Backward-compatible single-field wrappers (sources-merge.ts parity) ───

def parse_sources(content: str) -> list[str]:
    return parse_frontmatter_array(content, "sources")


def write_sources(content: str, sources: list[str]) -> str:
    return write_frontmatter_array(content, "sources", sources)


def merge_sources_lists(existing: list[str], incoming: list[str]) -> list[str]:
    return merge_lists(existing, incoming)


def merge_sources_into_content(new_content: str, existing_content: str | None) -> str:
    return merge_array_fields_into_content(new_content, existing_content, ["sources"])
