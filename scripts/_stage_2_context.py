"""Shared consolidated Stage 2 context for NashSU-style generation.

NashSU 0.6.6 analyzes long sources chunk by chunk, then performs one final
generation call with the final rolling digest and the complete set of chunk
analyses.  improved-wiki additionally keeps bounded raw evidence from every
chunk so exact formulas, identifiers, and late-source details remain grounded.

The builder below is deterministic and hard-bounded by ``source_budget``.
Every chunk stays represented in both sections.  When the analyses do not all
fit, whole low-value FIELDS are dropped in a fixed priority order rather than
character-slicing each payload: an even split plus a balanced excerpt handed the
generation model 20-40 fragments cut through the middle of a JSON object
(measured at source_budget=104,000: 20 chunks needed 198,130 analysis chars and
received ~60,000).  Raw evidence takes the leftover budget instead of a fixed
share, so a short source keeps its full text and a long source spends the room
on cross-chunk analyses.  Whatever is dropped is stated in the context itself.
"""
from __future__ import annotations

import json


STAGE_2_CONTEXT_POLICY_VERSION = "nashsu-0.6.6-consolidated-v3"
_TRUNCATION_MARKER = "\n... [middle omitted to fit context budget] ...\n"

# Analysis detail levels, most detailed first. Each level names the fields it
# gives up; the builder picks the FIRST level whose rendered analyses fit.
# Ordering rationale: source_quotes duplicate raw evidence; connections are
# re-derived by Stage 2.3; formulas are separately fed verbatim to Stage 2.4 by
# _collect_formulas_block; key_details elaborate a definition that survives.
# Names, definitions, claims and schema-typed candidates are what generation
# selects pages from, so they are the last things to go.
_DETAIL_LEVELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("full", ()),
    ("no-source-quotes", ("source_quotes",)),
    ("no-connections", ("source_quotes", "connections_to_existing_wiki")),
    (
        "core-fields",
        ("source_quotes", "connections_to_existing_wiki", "formulas",
         "key_details"),
    ),
    (
        "names-claims-only",
        ("source_quotes", "connections_to_existing_wiki", "formulas",
         "key_details", "definition", "significance", "evidence", "rationale"),
    ),
    (
        "names-only",
        ("source_quotes", "connections_to_existing_wiki", "formulas",
         "key_details", "definition", "significance", "evidence", "rationale",
         "claims"),
    ),
)

# Share of the post-digest budget the analyses may claim. A single-chunk source
# is grounded primarily by its raw text; a multi-chunk source is grounded by the
# cross-chunk analyses. Either way the analyses only take what they need at the
# chosen detail level and the rest goes to raw evidence.
_ANALYSIS_SHARE_SHORT = 0.45
_ANALYSIS_SHARE_LONG = 0.85


def _balanced_excerpt(text: str, limit: int) -> str:
    """Return at most ``limit`` characters while preserving both ends."""
    text = str(text or "")
    limit = max(0, int(limit))
    if len(text) <= limit:
        return text
    if limit <= len(_TRUNCATION_MARKER):
        return text[:limit]
    payload = limit - len(_TRUNCATION_MARKER)
    head = (payload + 1) // 2
    tail = payload - head
    return text[:head] + _TRUNCATION_MARKER + (text[-tail:] if tail else "")


def _fit_json_scalar(text: str, limit: int) -> str:
    """Return a valid JSON scalar no longer than ``limit`` when possible."""
    limit = max(0, int(limit))
    if limit == 0:
        return ""
    if limit == 1:
        return "0"
    low, high = 0, len(text)
    best = '""'
    while low <= high:
        middle = (low + high) // 2
        candidate = json.dumps(text[:middle], ensure_ascii=False)
        if len(candidate) <= limit:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def _json_excerpt(serialized: str, limit: int) -> str:
    """Fit an existing JSON serialization without cutting its syntax.

    Normal analysis degradation removes whole fields before this helper is
    needed. This is the final safety net for an oversized minimum-detail
    analysis and for a large final digest: preserve balanced head/tail evidence
    inside a valid JSON object instead of ending inside a quoted value.
    """
    limit = max(0, int(limit))
    serialized = str(serialized or "")
    if len(serialized) <= limit:
        return serialized

    identity = {}
    try:
        parsed = json.loads(serialized)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        for key in ("chunk_index", "chunk_total"):
            if key in parsed:
                identity[key] = parsed[key]

    def _envelope(payload_chars: int) -> str:
        head_chars = (payload_chars + 1) // 2
        tail_chars = payload_chars - head_chars
        return json.dumps(
            {
                **identity,
                "_truncated": _TRUNCATION_MARKER.strip(),
                "head": serialized[:head_chars],
                "tail": (
                    serialized[-tail_chars:] if tail_chars else ""
                ),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )

    minimum = _envelope(0)
    if len(minimum) > limit:
        return _fit_json_scalar(_TRUNCATION_MARKER.strip(), limit)

    low, high = 0, len(serialized)
    best = minimum
    while low <= high:
        middle = (low + high) // 2
        candidate = _envelope(middle)
        if len(candidate) <= limit:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def _fit_section(title: str, body: str, budget: int) -> str:
    """Fit one titled section into an exact character budget."""
    budget = max(0, int(budget))
    if budget == 0:
        return ""
    prefix = title.rstrip() + "\n"
    if len(prefix) >= budget:
        return prefix[:budget]
    return prefix + _balanced_excerpt(body, budget - len(prefix))


def _fit_json_section(title: str, serialized: str, budget: int) -> str:
    """Fit one titled section while keeping the body valid JSON."""
    budget = max(0, int(budget))
    if budget == 0:
        return ""
    prefix = title.rstrip() + "\n"
    if len(prefix) >= budget:
        return prefix[:budget]
    return prefix + _json_excerpt(serialized, budget - len(prefix))


def _drop_item_fields(items, dropped: tuple[str, ...]) -> list:
    """Remove dropped per-item fields from one analysis list field."""
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            out.append(item)
            continue
        out.append({
            key: value for key, value in item.items()
            if key not in dropped
        })
    return out


def _project_analysis(analysis: dict, dropped: tuple[str, ...]) -> dict:
    """One chunk analysis with ``dropped`` fields removed, still an object.

    Internal ``_``-prefixed keys and ``updated_global_digest`` never belong in
    the shared context: the final rolled-up digest is rendered once above, so
    repeating each chunk's interim digest would spend the whole budget on it.
    """
    projected = {
        key: value
        for key, value in (analysis or {}).items()
        if (
            not str(key).startswith("_")
            and key != "updated_global_digest"
            and key not in dropped
        )
    }
    for field, value in list(projected.items()):
        if isinstance(value, list):
            projected[field] = _drop_item_fields(value, dropped)
    return projected


def _render_analysis_payloads(
    chunk_analyses: list[dict],
    dropped: tuple[str, ...],
) -> list[str]:
    return [
        json.dumps(
            _project_analysis(analysis, dropped),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        for analysis in chunk_analyses
    ]


def _chunk_labels(headings: list[str], total: int, compact: bool) -> list[str]:
    if compact:
        return [f"### {index}/{total}\n" for index in range(1, total + 1)]
    labels = []
    for index, heading in enumerate(headings, 1):
        suffix = f" — {heading.strip()}" if str(heading or "").strip() else ""
        labels.append(f"### Chunk {index}/{total}{suffix}\n")
    return labels


def _render_chunk_section(
    title: str,
    payloads: list[str],
    headings: list[str],
    budget: int,
    *,
    structured_payloads: bool = False,
) -> str:
    """Render all chunks fairly inside ``budget`` characters.

    Labels are retained for every chunk. Remaining room is divided evenly so a
    large early chunk cannot evict later analyses/evidence.
    """
    budget = max(0, int(budget))
    if budget == 0:
        return ""
    title_line = title.rstrip() + "\n"
    if len(title_line) >= budget:
        return title_line[:budget]
    if not payloads:
        return _fit_section(title, "(none)", budget)

    total = len(payloads)
    remaining = budget - len(title_line)
    labels = _chunk_labels(headings, total, compact=False)
    label_total = sum(len(label) + 1 for label in labels)
    if label_total > remaining:
        labels = _chunk_labels(headings, total, compact=True)
        label_total = sum(len(label) + 1 for label in labels)
    if label_total > remaining:
        # This requires an unusually large chunk count for the minimum 8K
        # source budget. Keep a compact manifest rather than cutting off later
        # chunks without saying so.
        manifest = "Chunks represented: " + ", ".join(
            str(index) for index in range(1, total + 1)
        )
        return _fit_section(title, manifest, budget)

    payload_room = remaining - label_total
    per_chunk, extra = divmod(payload_room, total)
    pieces = [title_line]
    for position, (label, payload) in enumerate(zip(labels, payloads)):
        allowance = per_chunk + (1 if position < extra else 0)
        pieces.append(label)
        pieces.append(
            _json_excerpt(payload, allowance)
            if structured_payloads
            else _balanced_excerpt(payload, allowance)
        )
        pieces.append("\n")
    rendered = "".join(pieces).rstrip()
    return rendered[:budget]


def _select_detail_level(
    chunk_analyses: list[dict],
    headings: list[str],
    analysis_cap: int,
) -> tuple[int, list[str], str]:
    """Most detailed level whose rendered analyses fit ``analysis_cap``.

    Returns ``(level_index, payloads, rendered_section)``. The last level is
    used even when it overflows; ``_render_chunk_section`` then excerpts it, and
    the caller reports that in the context notes.
    """
    level = 0
    payloads: list[str] = []
    rendered = ""
    for level, (_name, dropped) in enumerate(_DETAIL_LEVELS):
        payloads = _render_analysis_payloads(chunk_analyses, dropped)
        rendered = _render_chunk_section(
            "## Per-Chunk Analyses",
            payloads,
            headings,
            analysis_cap,
            structured_payloads=True,
        )
        # A level fits only when the section reproduced every payload in full —
        # the point of stepping down is to avoid excerpted (unparseable) JSON.
        if all(payload in rendered for payload in payloads):
            return level, payloads, rendered
    return level, payloads, rendered


def build_consolidated_stage_2_context(
    global_digest: dict,
    chunk_analyses: list[dict],
    chunk_meta: list,
    source_budget: int,
) -> str:
    """Build the shared Stage 2.4/2.6 whole-source context.

    Layout:
      1. what the budget forced out (never a silent cap);
      2. final rolling digest;
      3. every validated chunk analysis, at the most detailed level that fits;
      4. raw evidence from every source chunk, using the leftover budget.

    The returned string never exceeds ``source_budget``.
    """
    if len(chunk_analyses) != len(chunk_meta):
        raise RuntimeError(
            "[Stage 2 context] Chunk plan/analysis cardinality mismatch: "
            f"{len(chunk_meta)} planned chunks vs "
            f"{len(chunk_analyses)} analyses."
        )

    budget = max(1, int(source_budget or 8_000))
    header = (
        "# Consolidated Stage 2 Context\n"
        "Final rolling digest, all chunk analyses, and bounded raw evidence "
        "from every chunk follow.\n"
    )
    if len(header) >= budget:
        return header[:budget]

    digest_text = json.dumps(
        global_digest or {},
        ensure_ascii=False,
        indent=2,
        default=str,
    )
    raw_payloads: list[str] = []
    headings: list[str] = []
    for meta in chunk_meta:
        raw_payloads.append(str(meta[1] or ""))
        headings.append(str(meta[3] or ""))

    remaining = budget - len(header) - 2
    digest_budget = min(
        24_000,
        max(1_000, int(budget * 0.15)),
        remaining,
    )
    digest_section = _fit_json_section(
        "## Final Global Digest",
        digest_text,
        digest_budget,
    )
    remaining = max(0, remaining - len(digest_section) - 2)

    total_chunks = len(chunk_analyses)
    share = (
        _ANALYSIS_SHARE_SHORT if total_chunks <= 1 else _ANALYSIS_SHARE_LONG
    )
    level, _payloads, analysis_section = _select_detail_level(
        chunk_analyses, headings, int(remaining * share))

    level_name, dropped_fields = _DETAIL_LEVELS[level]
    notes = [
        f"- Chunks represented: {total_chunks}; per-chunk analyses rendered at "
        f"detail level {level + 1}/{len(_DETAIL_LEVELS)} ({level_name})."
    ]
    if dropped_fields:
        notes.append(
            "- Omitted from every chunk analysis to fit the budget: "
            + ", ".join(dropped_fields)
            + ". Do not treat an omitted field as absent from the source."
        )
    if _TRUNCATION_MARKER.strip() in analysis_section:
        notes.append(
            "- Some analyses were additionally excerpted at the minimum detail "
            "level; the omission is marked inline."
        )
    notes_section = _fit_section(
        "## Context Budget", "\n".join(notes), min(1_200, remaining))
    remaining = max(0, remaining - len(analysis_section) - len(notes_section) - 4)

    raw_section = _render_chunk_section(
        "## Bounded Raw Source Evidence",
        raw_payloads,
        headings,
        remaining,
    )
    result = "\n\n".join(
        section for section in (header.rstrip(), notes_section, digest_section,
                                analysis_section, raw_section)
        if section
    )
    return result[:budget]
