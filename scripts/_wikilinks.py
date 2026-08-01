"""Shared wikilink parsing and Markdown-table escaping helpers.

Obsidian-style aliases use ``[[target|display]]`` in prose, but Markdown
tables also treat ``|`` as a cell delimiter.  Inside a table cell the alias
separator must therefore be escaped as ``[[target\\|display]]``.  Readers must
accept both spellings while writers must make table rows safe and idempotent.
"""
from __future__ import annotations

import re


# group(1) = target; group(2) = display text or None.  The optional backslash
# belongs to Markdown's table escape, not to the target.
WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\\?\|([^\]]+))?\]\]")

_WIKILINK_SPAN_RE = re.compile(r"\[\[([^\[\]\r\n]+)\]\]")
_CODE_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*"
    r"(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
_UNESCAPED_PIPE_RE = re.compile(r"(?<!\\)\|")


def split_wikilink_inner(inner: str) -> tuple[str, str | None, str]:
    """Split the text inside ``[[...]]`` into target, alias, and separator.

    ``separator`` is ``"|"``, ``r"\\|"``, or ``""``.  An odd run of
    backslashes immediately before the first pipe means that the final
    backslash is Markdown's table escape and is not part of the target.
    """
    for index, char in enumerate(inner):
        if char != "|":
            continue
        slash_count = 0
        cursor = index - 1
        while cursor >= 0 and inner[cursor] == "\\":
            slash_count += 1
            cursor -= 1
        if slash_count % 2:
            return inner[:index - 1], inner[index + 1:], r"\|"
        return inner[:index], inner[index + 1:], "|"
    return inner, None, ""


def escape_wikilink_alias_pipes(text: str) -> tuple[str, int]:
    """Escape unescaped pipes inside every complete wikilink in ``text``."""
    count = 0

    def _escape(match: re.Match) -> str:
        nonlocal count
        inner, replacements = _UNESCAPED_PIPE_RE.subn(
            r"\\|", match.group(1)
        )
        count += replacements
        return f"[[{inner}]]"

    return _WIKILINK_SPAN_RE.sub(_escape, text), count


def _table_line_indexes(lines: list[str]) -> set[int]:
    """Return line indexes belonging to Markdown tables outside code fences."""
    eligible = [True] * len(lines)
    in_fence = False
    fence_char = ""
    fence_len = 0
    for index, line in enumerate(lines):
        match = _CODE_FENCE_RE.match(line)
        if match:
            marker = match.group(1)
            eligible[index] = False
            if not in_fence:
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
            elif (
                marker[0] == fence_char
                and len(marker) >= fence_len
                and line.strip() == marker
            ):
                in_fence = False
            continue
        if in_fence:
            eligible[index] = False

    table_lines: set[int] = set()
    for separator_index, line in enumerate(lines):
        if not eligible[separator_index] or not _TABLE_SEPARATOR_RE.match(line):
            continue
        header_index = separator_index - 1
        if (
            header_index < 0
            or not eligible[header_index]
            or not _UNESCAPED_PIPE_RE.search(lines[header_index])
        ):
            continue
        table_lines.update((header_index, separator_index))
        row_index = separator_index + 1
        while (
            row_index < len(lines)
            and eligible[row_index]
            and lines[row_index].strip()
            and _UNESCAPED_PIPE_RE.search(lines[row_index])
        ):
            table_lines.add(row_index)
            row_index += 1
    return table_lines


def escape_markdown_table_wikilink_aliases(text: str) -> tuple[str, int]:
    """Escape wikilink alias pipes only on real Markdown-table rows.

    A table is recognized by its standard ``|---|---|`` delimiter row.  This
    supports tables with or without outer pipes, ignores fenced code examples,
    and leaves prose links byte-identical.  Re-running is a no-op.
    """
    lines = text.split("\n")
    count = 0
    for index in sorted(_table_line_indexes(lines)):
        lines[index], escaped = escape_wikilink_alias_pipes(lines[index])
        count += escaped
    return "\n".join(lines), count
