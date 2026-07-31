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

In conversation-mode handoffs, an agent can accidentally write a LaTeX
command through an escaping layer that interprets ``\\f``, ``\\r``, or ``\\t``
as C0 characters.  The repair below is deliberately limited to math spans so
ordinary body indentation remains byte-for-byte unchanged.
"""
from __future__ import annotations

import re

__all__ = ["sanitize_ingested_file_content"]


# ── (1) Strip an outer code fence wrapping the whole document ────────────────
# The opener tolerates a BOM and blank lines before the fence, and an
# upper/mixed-case info string (```YAML), matching NashSU's regex exactly
# (ingest-sanitize.ts:93-95). Without that tolerance a page whose fence was
# preceded by one blank line kept its ``` first line forever: the frontmatter
# then failed to parse, so type/graph/index all degraded to `other`, and
# wiki-lint --fix stacked a placeholder frontmatter block on top of the fence
# rather than removing it.
_OUTER_OPEN_RE = re.compile(
    r"^(?:﻿)?(?:[ \t]*\r?\n)*[ \t]*```(?:yaml|md|markdown)?[ \t]*\r?\n",
    re.IGNORECASE,
)
_OUTER_CLOSE_RE = re.compile(r"\r?\n[ \t]*```[ \t]*\r?\n?\s*$")
# Models often close the fence right after the frontmatter and continue with an
# unfenced body. Only strip that shape when the fenced part is exactly one
# complete `---` block (ingest-sanitize.ts:107-111).
_OUTER_FRONTMATTER_ONLY_RE = re.compile(
    r"^(---[ \t]*\r?\n[\s\S]*?^---[ \t]*\r?\n)[ \t]*```[ \t]*(?:\r?\n|$)",
    re.MULTILINE,
)


def _strip_outer_code_fence(content: str) -> str:
    """Remove a leading ```yaml/```md/```markdown/``` fence + its matching
    closing fence when it wraps the whole document (or just its frontmatter)."""
    open_m = _OUTER_OPEN_RE.match(content)
    if not open_m:
        return content
    after_open = content[open_m.end():]
    close_m = _OUTER_CLOSE_RE.search(after_open)
    if close_m:
        return after_open[: close_m.start()]
    fm_only = _OUTER_FRONTMATTER_ONLY_RE.match(after_open)
    if not fm_only:
        return content
    return fm_only.group(1) + after_open[fm_only.end():]


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


# Only repair controls which are the standard one-character escapes for a
# literal LaTeX command prefix.  ``read_text()`` normalizes CRLF line endings,
# so a carriage return that survives inside a math span is not a line ending.
_MATH_SPAN_RE = re.compile(
    r"\$\$.*?\$\$|(?<!\$)\$(?!\$)[^$\n]+\$(?!\$)",
    re.DOTALL,
)
_LATEX_CONTROL_ESCAPES = {
    "\x07": r"\a",  # \alpha, \angle, \approx
    "\x08": r"\b",  # \beta, \begin
    "\x0b": r"\v",  # \vec
    "\x0c": r"\f",  # \frac
    "\r": r"\r",    # \rho, \right, \mathrm
    "\t": r"\t",    # \theta, \tag, \text
}
_LATEX_COMMANDS = (
    "alpha", "angle", "approx", "begin", "beta", "cdot", "circ", "cos",
    "Delta", "dfrac", "end", "epsilon", "eta", "exp", "frac", "gamma",
    "ge", "hat", "infty", "lambda", "left", "le", "log", "max", "min",
    "mu", "neq", "Omega", "operatorname", "partial", "Phi", "pi", "pm",
    "propto", "rho", "right", "Sigma", "sin", "sqrt", "sum", "tag",
    "text", "theta", "times", "underline", "vec", "widehat", "omega",
)
_MISSING_LATEX_COMMAND_RE = re.compile(
    r"(?<!\\)\b(" + "|".join(_LATEX_COMMANDS) + r")\b",
)


def _repair_latex_control_escapes(content: str) -> str:
    """Restore literal LaTeX backslashes swallowed as C0 escapes.

    A result file may contain ``form-feed + 'rac'`` instead of ``\\frac`` or
    ``tab + 'ag'`` instead of ``\\tag``.  Restricting the repair to ``$``/``$$``
    spans keeps legitimate tabs in Markdown prose intact and makes the
    transformation idempotent.
    """
    def _repair_span(match: re.Match[str]) -> str:
        repaired = "".join(_LATEX_CONTROL_ESCAPES.get(ch, ch) for ch in match.group(0))
        # Some handoff writers drop a backslash rather than converting it to a
        # control byte (for example hat or sqrt). These are only command names
        # inside math, and the negative lookbehind avoids double-prefixing
        # commands which were already intact.
        return _MISSING_LATEX_COMMAND_RE.sub(
            lambda command: "\\" + command.group(1), repaired)

    return _MATH_SPAN_RE.sub(_repair_span, content)


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
    cleaned = _repair_latex_control_escapes(cleaned)
    return cleaned
