#!/usr/bin/env python3
"""_frontmatter_array.py — frontmatter array-field parse / write / union.

Reads use the shared YAML frontmatter parser. Writes patch only the selected
array field so unrelated frontmatter formatting and body bytes survive.

Public API mirrors sources-merge.ts:
  - parse_frontmatter_array(content, field)        -> list[str]
  - write_frontmatter_array(content, field, vals)  -> str
  - merge_array_fields_into_content(new, existing, fields) -> str
  - merge_lists(existing, incoming)                -> list[str]
  - normalize_block_arrays(content)                -> str
"""
from __future__ import annotations

import re

__all__ = [
    "parse_frontmatter_array",
    "write_frontmatter_array",
    "merge_array_fields_into_content",
    "merge_lists",
    "normalize_block_arrays",
]

# Frontmatter block: leading `---` ... `---`. DOTALL so the body spans lines.
# `\r?\n` (not bare `\n`) — aligned with _frontmatter.py so CRLF pages don't
# silently no-op through every array operation here.
_FM_RE = re.compile(r"^---\r?\n(.*?)\r?\n---", re.DOTALL)
_FM_REPLACE_RE = re.compile(r"^(---\r?\n)(.*?)(\r?\n---)", re.DOTALL)


def _escape(name: str) -> str:
    return re.escape(name)


def parse_frontmatter_array(content: str, field_name: str) -> list[str]:
    """Read an array through the shared YAML reader; malformed values are empty."""
    from _frontmatter import parse_frontmatter
    value = parse_frontmatter(content)[0].get(field_name)
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value
            if isinstance(item, (str, int, float)) and str(item).strip()]


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

    # Match the whole inline-array line. `.*` (greedy to the last `]` on the
    # line) is intentional: a `[^\]]*` class can't span a `]` that appears
    # inside a quoted item (e.g. `related: ["[[a]]", "[[b]]"]`), which would
    # corrupt the rewrite by consuming only up to the first inner `]`.
    inline_re = re.compile(rf"^{escaped}:\s*\[.*\]\s*$", re.MULTILINE)
    if inline_re.search(fm_body):
        rewritten = inline_re.sub(lambda _m: new_line, fm_body, count=1)
        return f"{open_delim}{rewritten}{close_delim}{rest}"

    block_re = re.compile(
        rf"^{escaped}:\s*\n((?:[ \t]+-\s+.+\n?)+)",
        re.MULTILINE,
    )
    m_block = block_re.search(fm_body)
    if m_block:
        # The block regex's trailing `\n?` swallows the newline that separates
        # the last list item from the NEXT frontmatter field; the inline
        # replacement carries no trailing newline, so without restoring it the
        # following field collapses onto the same line (corrupt YAML). Restore it
        # only when the matched span actually ended in a newline (i.e. a field
        # follows; when this is the last field, close_delim supplies the break).
        tail = "\n" if m_block.group(0).endswith("\n") else ""
        rewritten = block_re.sub(lambda _m: new_line + tail, fm_body, count=1)
        return f"{open_delim}{rewritten}{close_delim}{rest}"

    # Field absent — append at end of frontmatter.
    rewritten = f"{fm_body}\n{new_line}"
    return f"{open_delim}{rewritten}{close_delim}{rest}"


def merge_lists(existing: list[str], incoming: list[str], *, case_sensitive: bool = False) -> list[str]:
    """Stable union; source paths require case-sensitive identity."""
    seen: set[str] = set()
    out: list[str] = []
    for s in list(existing) + list(incoming):
        key = s if case_sensitive else s.lower()
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
    if not re.match(r"^---\r?\n", existing_content):
        return new_content

    result = new_content
    changed = False
    for field in fields:
        old_values = parse_frontmatter_array(existing_content, field)
        if len(old_values) == 0:
            continue  # field absent in existing → nothing to preserve
        new_values = parse_frontmatter_array(result, field)
        merged = merge_lists(old_values, new_values, case_sensitive=field == "sources")
        if len(merged) == len(new_values) and all(
            s == new_values[i] for i, s in enumerate(merged)
        ):
            continue  # no-op for this field
        result = write_frontmatter_array(result, field, merged)
        changed = True
    return result if changed else new_content


# Array fields subject to block→inline normalization (== _frontmatter.UNION_FIELDS;
# not imported to keep this module dependency-free).
_NORMALIZE_FIELDS = ("tags", "related", "sources")


def normalize_block_arrays(content: str) -> str:
    """Rewrite block-style frontmatter arrays (``related:\\n  - a``) for
    tags/related/sources into the inline form (``related: ["a"]``).

    Compatibility formatting helper; parsing supports both forms directly.
    Content without frontmatter, or with all-inline arrays, passes through
    unchanged.
    """
    fm_match = _FM_RE.match(content)
    if not fm_match:
        return content
    for field in _NORMALIZE_FIELDS:
        block_re = re.compile(
            rf"^{_escape(field)}:\s*\n(?:[ \t]+-\s+.+\n?)+",
            re.MULTILINE,
        )
        fm_match = _FM_RE.match(content)
        if not fm_match or not block_re.search(fm_match.group(1)):
            continue
        values = parse_frontmatter_array(content, field)
        content = write_frontmatter_array(content, field, values)
    return content
