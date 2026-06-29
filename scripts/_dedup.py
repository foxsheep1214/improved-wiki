#!/usr/bin/env python3
"""_dedup.py — duplicate-entity / -concept detection and merge.

Faithful port of NashSU `src/lib/dedup.ts` (verified against v0.5.3).

Problem: across re-ingests the LLM names the same topic differently
(`paos` vs `聚磷菌`, `dpao` vs `dpaos`, `vfa` vs `volatile-fatty-acids`).
Each becomes a separate page even though they're the same entity. The
page-merge layer only catches EXACT slug collisions; this module catches the
soft-collision case via an LLM-driven self-check.

Three independently testable stages:
  1. extract_entity_summary  — pure data (slug, title, description, tags).
  2. detect_duplicate_groups — LLM identifies same-topic slug groups (LLM
     call injected, so unit tests don't hit a model).
  3. merge_duplicate_group   — LLM body merge + deterministic frontmatter
     union + cross-reference rewrite + backup snapshot. Compute only; the
     CALLER performs the filesystem writes/deletes.

The LLM call is a plain callable `(system_prompt, user_message) -> str`.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date

from _frontmatter import parse_frontmatter, BODY_SHRINK_THRESHOLD
from _frontmatter_array import (
    merge_array_fields_into_content,
    parse_frontmatter_array,
    write_frontmatter_array,
)

__all__ = [
    "EntitySummary",
    "MergeResult",
    "extract_entity_summary",
    "detect_duplicate_groups",
    "parse_detector_response",
    "merge_duplicate_group",
    "rewrite_cross_references",
    "rewrite_index_md",
    "DETECTOR_SYSTEM_PROMPT",
    "MERGER_SYSTEM_PROMPT",
]


@dataclass
class EntitySummary:
    slug: str
    path: str
    type: str
    title: str
    tags: list[str] = field(default_factory=list)
    description: str | None = None


@dataclass
class MergeResult:
    canonical_content: str
    canonical_path: str
    rewrites: list[dict]      # [{"path", "new_content"}]
    pages_to_delete: list[str]
    backup: list[dict]        # [{"path", "content"}]


# ──────────────────────────────────────────────────────────────────
# Stage 1: extract summaries (no LLM)
# ──────────────────────────────────────────────────────────────────

def extract_entity_summary(path_relative_to_project: str, content: str) -> EntitySummary | None:
    """Build an EntitySummary from a page's path + content. Returns None when
    the page has no frontmatter."""
    frontmatter, body = parse_frontmatter(content)
    if not frontmatter:
        return None
    page_type = _string_field(frontmatter.get("type")) or "unknown"
    title = _string_field(frontmatter.get("title")) or _slug_from_path(path_relative_to_project)
    description = _string_field(frontmatter.get("description")) or _first_body_paragraph(body)
    tags = _array_field(frontmatter.get("tags"))
    return EntitySummary(
        slug=_slug_from_path(path_relative_to_project),
        path=path_relative_to_project,
        type=page_type,
        title=title,
        tags=tags,
        description=_truncate(description, 200) if description else None,
    )


def _slug_from_path(path: str) -> str:
    base = path.split("/")[-1] if "/" in path else path
    return re.sub(r"\.md$", "", base)


def _string_field(v) -> str | None:
    if isinstance(v, str) and v.strip() != "":
        return v.strip()
    return None


def _array_field(v) -> list[str]:
    if not isinstance(v, list):
        return []
    return [x.strip() for x in v if isinstance(x, str) and x.strip() != ""]


def _first_body_paragraph(body: str) -> str | None:
    for raw in body.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if line.startswith("|"):  # table — too noisy
            continue
        return line
    return None


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


# ──────────────────────────────────────────────────────────────────
# Stage 2: LLM-driven duplicate detection
# ──────────────────────────────────────────────────────────────────

DETECTOR_SYSTEM_PROMPT = """You are a wiki maintenance assistant. You will receive a list of entity / concept pages from a wiki. Identify groups of slugs that likely refer to the same underlying topic under different names — for example:

- Same name in two languages (English vs Chinese, etc.)
- Plural vs singular form (e.g. "dpao" vs "dpaos")
- Abbreviation vs full form (e.g. "vfa" vs "volatile-fatty-acids")
- Synonyms in the same language
- The same proper noun spelled differently

Output ONLY valid JSON. No prose, no markdown fences, no explanation outside the JSON. The schema is:

{
  "groups": [
    {
      "slugs": ["slug-a", "slug-b"],
      "reason": "Both refer to X; first is English, second is Chinese.",
      "confidence": "high"
    }
  ]
}

Rules:
- Only include groups of 2 or more slugs from the input list.
- "high" = clearly the same entity, only naming differs.
- "medium" = likely the same but context-dependent.
- "low" = uncertain; user should review carefully.
- Never invent slugs that aren't in the input.
- If no duplicates exist, output {"groups": []}.
- Pages of different `type` (e.g. an entity and a concept) usually should NOT be grouped — only group across types when they're unambiguously the same thing."""


def detect_duplicate_groups(
    summaries: list[EntitySummary],
    llm_call,
    *,
    not_duplicates: list[list[str]] | None = None,
) -> list[dict]:
    """Run the LLM duplicate-detector. Filters out groups whose slugs aren't in
    the input and groups already on the not-duplicates whitelist. Returns
    [{"slugs", "reason", "confidence"}]."""
    if len(summaries) < 2:
        return []

    user_message = _build_detector_user_message(summaries)
    response = llm_call(DETECTOR_SYSTEM_PROMPT, user_message)
    parsed = parse_detector_response(response)

    valid_slugs = {s.slug for s in summaries}
    not_dup_set = {_normalize_group_key(g) for g in (not_duplicates or [])}

    out: list[dict] = []
    for g in parsed:
        slugs = [s for s in g["slugs"] if s in valid_slugs]
        if len(slugs) < 2:
            continue
        if _normalize_group_key(slugs) in not_dup_set:
            continue
        out.append({**g, "slugs": slugs})
    return out


def _build_detector_user_message(summaries: list[EntitySummary]) -> str:
    lines = []
    for s in summaries:
        tag_part = f" [{', '.join(s.tags)}]" if s.tags else ""
        desc_part = f" — {s.description}" if s.description else ""
        title = json.dumps(s.title, ensure_ascii=False)
        lines.append(f"- type={s.type}, slug={s.slug}, title={title}{tag_part}{desc_part}")
    body = "\n".join(lines)
    return (
        f"## Wiki pages to scan ({len(summaries)} entries)\n\n"
        f"{body}\n\nReturn duplicate groups as JSON only."
    )


def parse_detector_response(raw: str) -> list[dict]:
    """Tolerant JSON extraction: strips code fences / preamble, pulls the first
    balanced {...} block. Returns [] on any failure."""
    json_text = _extract_first_json_object(raw)
    if not json_text:
        return []
    try:
        parsed = json.loads(json_text)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, dict):
        return []
    groups_raw = parsed.get("groups")
    if not isinstance(groups_raw, list):
        return []

    out: list[dict] = []
    for g in groups_raw:
        if not isinstance(g, dict):
            continue
        raw_slugs = g.get("slugs")
        slugs = [s for s in raw_slugs if isinstance(s, str)] if isinstance(raw_slugs, list) else []
        if len(slugs) < 2:
            continue
        reason = g.get("reason") if isinstance(g.get("reason"), str) else ""
        confidence = g.get("confidence")
        if confidence not in ("high", "medium"):
            confidence = "low"
        out.append({"slugs": slugs, "reason": reason, "confidence": confidence})
    return out


def _extract_first_json_object(text: str) -> str | None:
    """Extract the first balanced {...} substring, ignoring braces inside
    strings and escaped characters."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _normalize_group_key(slugs: list[str]) -> str:
    return ",".join(sorted(s.lower() for s in slugs))


# ──────────────────────────────────────────────────────────────────
# Stage 3: merge a confirmed duplicate group
# ──────────────────────────────────────────────────────────────────

MERGER_SYSTEM_PROMPT = """You are a wiki maintenance assistant. You will be given several wiki pages that all describe the same entity or concept under different names. Merge them into a single coherent wiki page.

Output the COMPLETE merged file (frontmatter + body). The first character of your response MUST be "-" (the opening of "---"). No preamble, no explanation outside the file.

Rules:
- Preserve every distinct factual claim from every input page.
- Eliminate redundancy (don't say the same thing twice across sections).
- Reorganize sections so the structure is logical for the unified topic, not a concatenation of inputs.
- Use [[wikilink]] syntax in the body where the inputs did.
- Frontmatter: keep the standard fields (type, title, created, updated, tags, related, sources). The caller will overwrite sources / tags / related / updated with deterministic unions afterward — your job is to produce a sensible body and reasonable frontmatter shape.
- Pick the most descriptive title. If the inputs use different languages, prefer the language that matches the majority of the body content."""

FIELDS_TO_UNION = ["sources", "tags", "related"]


def _validate_merge_output(llm_output: str, group: list[dict]) -> None:
    """Reject an LLM merge that would corrupt the canonical page.

    Two checks (mirror ``_frontmatter.merge_page_content`` sanity gates):
      1. Output must parse as a frontmatter-bearing wiki page — a body-only
         or "I can't merge that" response would leave the canonical page
         with no type/title/sources.
      2. Merged body must be >= BODY_SHRINK_THRESHOLD (0.7) of the longest
         input body — catches lazy summaries / truncation that drop claims
         from the pages about to be deleted.
    Raises ValueError on failure; never falls back silently.
    """
    fm, body = parse_frontmatter(llm_output)
    if not fm:
        raise ValueError(
            "LLM merge output has no frontmatter — rejecting (no fallback). "
            "Fix the LLM provider and re-run; the canonical page was NOT overwritten."
        )
    input_bodies = [len(parse_frontmatter(p["content"])[1]) for p in group]
    max_input = max(input_bodies) if input_bodies else 0
    threshold = max_input * BODY_SHRINK_THRESHOLD
    if len(body.strip()) < threshold:
        raise ValueError(
            f"LLM merge body {len(body)} chars < {threshold:.0f} threshold "
            f"(max input body {max_input}) — rejecting (likely truncation / "
            f"lazy summary). No fallback; canonical page NOT overwritten."
        )


def merge_duplicate_group(
    group: list[dict],
    canonical_slug: str,
    other_wiki_pages: list[dict],
    llm_call,
    *,
    today=None,
) -> MergeResult:
    """Compute everything needed to merge a confirmed duplicate group.

    group:            [{"slug", "path", "content"}] — >= 2 pages.
    canonical_slug:   slug to keep (must be in group).
    other_wiki_pages: [{"path", "content"}] — every other wiki page, for
                      cross-reference rewriting.
    Returns a MergeResult; the CALLER writes canonical_content + each rewrite,
    deletes pages_to_delete, and persists backup before writing.
    """
    canonical = next((p for p in group if p["slug"] == canonical_slug), None)
    if canonical is None:
        names = ", ".join(p["slug"] for p in group)
        raise ValueError(f'canonicalSlug "{canonical_slug}" is not in the group: {names}')
    if len(group) < 2:
        raise ValueError("merge_duplicate_group requires at least 2 pages in the group")

    # 1. LLM body merge.
    user_message = _build_merger_user_message(group)
    llm_output = llm_call(MERGER_SYSTEM_PROMPT, user_message)

    # 1b. Sanity-check the LLM output before it becomes the canonical page.
    #    No-fallback policy (matches _frontmatter.merge_page_content): a
    #    garbage merge — no frontmatter, or a body that shed most of the
    #    input — must NOT be written, or the canonical page is corrupted and
    #    the merged-away pages are already deleted. Raise so the caller can
    #    fix the LLM and re-run instead of silently losing data.
    _validate_merge_output(llm_output, group)

    # 2. Frontmatter union (deterministic post-processing of LLM output).
    merged = llm_output
    for page in group:
        merged = merge_array_fields_into_content(merged, page["content"], list(FIELDS_TO_UNION))

    # 3. Stamp updated to today.
    today_str = (today() if callable(today) else today) or _default_today()
    merged = _set_frontmatter_scalar(merged, "updated", today_str)

    # 4. Cross-reference rewrites across every other wiki page.
    slug_redirects: dict[str, str] = {}
    for page in group:
        if page["slug"] != canonical_slug:
            slug_redirects[page["slug"]] = canonical_slug
    rewrites: list[dict] = []
    for page in other_wiki_pages:
        rewritten = rewrite_cross_references(page["content"], slug_redirects)
        if rewritten != page["content"]:
            rewrites.append({"path": page["path"], "new_content": rewritten})

    # 5. Backup: every touched file's PRE-merge content.
    backup: list[dict] = [{"path": p["path"], "content": p["content"]} for p in group]
    for r in rewrites:
        orig = next((p for p in other_wiki_pages if p["path"] == r["path"]), None)
        if orig:
            backup.append({"path": orig["path"], "content": orig["content"]})

    # 6. Pages to delete: every group member except the canonical.
    pages_to_delete = [p["path"] for p in group if p["slug"] != canonical_slug]

    return MergeResult(
        canonical_content=merged,
        canonical_path=canonical["path"],
        rewrites=rewrites,
        pages_to_delete=pages_to_delete,
        backup=backup,
    )


def _build_merger_user_message(group: list[dict]) -> str:
    sections = []
    for i, p in enumerate(group):
        sections.append("\n".join([f"## Page {i + 1} (slug: {p['slug']})", "", p["content"], ""]))
    return "\n".join([
        f"These {len(group)} wiki pages have been confirmed by the user to describe the same topic.",
        f'Merge them into a single coherent page (the canonical slug will be "{group[0]["slug"]}" or whichever the caller chose).',
        "",
        "\n---\n\n".join(sections),
        "",
        "Now output the merged file. First character must be `-`.",
    ])


def rewrite_cross_references(content: str, slug_redirects: dict[str, str]) -> str:
    """Rewrite [[old-slug]] / [[old-slug|alias]] wikilinks and the `related:`
    field to point at the canonical slug. Dedups `related` after rewrite."""
    out = content

    # 1. Wikilinks in the body — both [[slug]] and [[slug|alias]].
    for old_slug, new_slug in slug_redirects.items():
        escaped = re.escape(old_slug)
        pattern = re.compile(rf"\[\[{escaped}(\|[^\]]+)?\]\]")
        out = pattern.sub(lambda m, ns=new_slug: f"[[{ns}{m.group(1) or ''}]]", out)

    # 2. & 3. `related` field — re-parse and rewrite.
    existing = parse_frontmatter_array(out, "related")
    if existing:
        rewritten = [slug_redirects.get(s, s) for s in existing]
        seen: set[str] = set()
        unique: list[str] = []
        for s in rewritten:
            k = s.lower()
            if k in seen:
                continue
            seen.add(k)
            unique.append(s)
        if len(unique) != len(existing) or any(s != existing[i] for i, s in enumerate(unique)):
            out = write_frontmatter_array(out, "related", unique)

    return out


def _set_frontmatter_scalar(content: str, field_name: str, value: str) -> str:
    m = re.match(r"^(---\n)(.*?)(\n---)", content, re.DOTALL)
    if not m:
        return content
    open_d, body, close_d = m.group(1), m.group(2), m.group(3)
    rest = content[m.end():]
    escaped = re.escape(field_name)
    new_line = f"{field_name}: {value}"
    # Match the scalar line; negative lookahead skips array fields (`name: [...]`).
    line_re = re.compile(rf"^{escaped}:\s*(?!\[)([^\n]*)", re.MULTILINE)
    if line_re.search(body):
        rewritten = line_re.sub(lambda _m: new_line, body, count=1)
        return f"{open_d}{rewritten}{close_d}{rest}"
    return f"{open_d}{body}\n{new_line}{close_d}{rest}"


def _default_today() -> str:
    return date.today().isoformat()


# ──────────────────────────────────────────────────────────────────
# Index rewriter — wiki/index.md-specific
# ──────────────────────────────────────────────────────────────────

def rewrite_index_md(content: str, removed_slugs: set[str]) -> str:
    """Conservatively remove whole lines from index.md that link to a
    merged-away slug (markdown link, wikilink, or bare `slug.md`)."""
    if not removed_slugs:
        return content
    out = [line for line in content.split("\n") if not _line_refers_to_slug(line, removed_slugs)]
    return "\n".join(out)


def _line_refers_to_slug(line: str, slugs: set[str]) -> bool:
    for slug in slugs:
        escaped = re.escape(slug)
        if re.search(rf"\[\[{escaped}(\|[^\]]*)?\]\]", line):  # wikilink
            return True
        if re.search(rf"\(([^)]*/)?{escaped}\.md\)", line):    # markdown link
            return True
        if re.search(rf"\b{escaped}\.md\b", line):             # bare slug.md
            return True
    return False
