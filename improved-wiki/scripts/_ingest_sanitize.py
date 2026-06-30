#!/usr/bin/env python3
"""_ingest_sanitize.py — clean up an LLM-generated wiki page body before write.

Faithful port of NashSU ``src/lib/ingest-sanitize.ts``.

NashSU's audit of one real corpus (67 entity pages) found 30/67 pages had
frontmatter that couldn't be parsed strictly. Four recurring shapes the model
emits — this module rewrites all four into the standard ``---\\n…\\n---\\n``
form before the page hits disk. Each pattern is anchored at the very start of
the document (or at top-level frontmatter scope) so a legitimate fenced code
block deep in the body, or a ``frontmatter:`` mention inside prose, is left
alone.

The read-time parser (``_frontmatter.parse_frontmatter``) keeps its own
fallback for the outer-fence case so already-written corrupt files still
render; sanitizing on write means newly-generated files never need that
fallback, and re-ingesting an old file once cleans it up permanently.

Public API:
  - sanitize_ingested_file_content(content) -> str
"""
from __future__ import annotations

import re

__all__ = ["sanitize_ingested_file_content"]


# ── (1) Strip an outer code fence wrapping the whole document ────────────────
_OUTER_OPEN_RE = re.compile(r"^[ \t]*```(?:yaml|md|markdown)?[ \t]*\r?\n")
_OUTER_CLOSE_RE = re.compile(r"\r?\n[ \t]*```[ \t]*\r?\n?\s*$")


def _strip_outer_code_fence(content: str) -> str:
    """Remove a leading ```yaml/```md/```markdown/``` fence + its matching
    closing fence when it wraps the whole document."""
    open_m = _OUTER_OPEN_RE.match(content)
    if not open_m:
        return content
    after_open = content[open_m.end():]
    close_m = _OUTER_CLOSE_RE.search(after_open)
    if not close_m:
        return content
    return after_open[: close_m.start()]


# ── (2) Strip a stray `frontmatter:` line prefixing the real `---` block ─────
_FRONTMATTER_KEY_RE = re.compile(
    r"^[ \t]*frontmatter\s*:\s*\r?\n(?=[ \t]*---\s*\r?\n)",
)


def _strip_frontmatter_key_prefix(content: str) -> str:
    m = _FRONTMATTER_KEY_RE.match(content)
    if not m:
        return content
    return content[m.end():]


# ── (2.5) Repair a missing opening frontmatter fence ────────────────────────
_FM_FIELD_FIRST_RE = re.compile(
    r"^(type|title|created|updated|tags|related|sources)\s*:",
    re.IGNORECASE,
)
_HEADING_RE = re.compile(r"^#{1,6}\s+")


def _add_missing_opening_frontmatter_fence(content: str) -> str:
    if re.match(r"^[ \t]*---\s*(\r?\n|$)", content):
        return content
    lines = content.split("\n")
    first_content_idx = -1
    for i, line in enumerate(lines):
        if line.strip():
            first_content_idx = i
            break
    if first_content_idx < 0:
        return content
    first = lines[first_content_idx].strip()
    if not _FM_FIELD_FIRST_RE.match(first):
        return content
    search_end = min(len(lines), first_content_idx + 30)
    for i in range(first_content_idx + 1, search_end):
        if lines[i].strip() == "---":
            return "---\n" + "\n".join(lines[first_content_idx:])
        if _HEADING_RE.match(lines[i].strip()):
            break
    return content


# ── (3) Repair `key: [[a]], [[b]]` lines inside the frontmatter block ───────
_FM_BLOCK_RE = re.compile(r"^---\s*\r?\n([\s\S]*?)\r?\n---\s*(\r?\n|$)")
_WIKILINK_LIST_LINE_RE = re.compile(
    r"^(\s*[A-Za-z_][\w-]*\s*:\s*)(\[\[[^\]]+\]\](?:\s*,\s*\[\[[^\]]+\]\])+)\s*$",
)


def _repair_wikilink_lists_in_frontmatter(content: str) -> str:
    m = _FM_BLOCK_RE.match(content)
    if not m:
        return content
    payload = m.group(1)

    def _repair_line(line: str) -> str:
        lm = _WIKILINK_LIST_LINE_RE.match(line)
        if not lm:
            return line
        items = [s.strip() for s in lm.group(2).split(",") if s.strip()]
        quoted = ", ".join(f'"{s}"' for s in items)
        return f"{lm.group(1)}[{quoted}]"

    repaired = "\n".join(_repair_line(line) for line in payload.split("\n"))
    # m.group(0) layout: <open_fence><payload><close_fence><trailing>.
    # Rebuild as open_fence + repaired payload + (close_fence + trailing + body).
    full = m.group(0)
    open_fence = full[: m.start(1) - m.start(0)]
    after_payload = full[m.end(1) - m.start(0):]
    return open_fence + repaired + after_payload + content[m.end(0):]


def sanitize_ingested_file_content(content: str) -> str:
    """Clean common LLM formatting errors before writing a wiki page to disk.

    Port of NashSU ``sanitizeIngestedFileContent``. Conservative: each pattern
    is anchored at document start / frontmatter scope, so body content is
    never touched.
    """
    cleaned = _strip_outer_code_fence(content)
    cleaned = _strip_frontmatter_key_prefix(cleaned)
    cleaned = _add_missing_opening_frontmatter_fence(cleaned)
    cleaned = _repair_wikilink_lists_in_frontmatter(cleaned)
    return cleaned
