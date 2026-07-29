"""Shared consolidated Stage 2 context for NashSU-style generation.

NashSU 0.6.6 analyzes long sources chunk by chunk, then performs one final
generation call with the final rolling digest and the complete set of chunk
analyses.  improved-wiki additionally keeps bounded raw evidence from every
chunk so exact formulas, identifiers, and late-source details remain grounded.

The builder below is deterministic and hard-bounded by ``source_budget``.
Every chunk remains represented even when its analysis or raw text must be
excerpted; truncation is balanced between the beginning and end instead of
silently dropping the tail of the book.
"""
from __future__ import annotations

import json


STAGE_2_CONTEXT_POLICY_VERSION = "nashsu-0.6.6-consolidated-v1"
_TRUNCATION_MARKER = "\n... [middle omitted to fit context budget] ...\n"


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


def _fit_section(title: str, body: str, budget: int) -> str:
    """Fit one titled section into an exact character budget."""
    budget = max(0, int(budget))
    if budget == 0:
        return ""
    prefix = title.rstrip() + "\n"
    if len(prefix) >= budget:
        return prefix[:budget]
    return prefix + _balanced_excerpt(body, budget - len(prefix))


def _render_chunk_section(
    title: str,
    payloads: list[str],
    headings: list[str],
    budget: int,
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
    labels: list[str] = []
    for index, heading in enumerate(headings, 1):
        suffix = f" — {heading.strip()}" if str(heading or "").strip() else ""
        labels.append(f"### Chunk {index}/{total}{suffix}\n")

    remaining = budget - len(title_line)
    label_total = sum(len(label) + 1 for label in labels)
    if label_total > remaining:
        labels = [f"### {index}/{total}\n" for index in range(1, total + 1)]
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
        pieces.append(_balanced_excerpt(payload, allowance))
        pieces.append("\n")
    rendered = "".join(pieces).rstrip()
    return rendered[:budget]


def build_consolidated_stage_2_context(
    global_digest: dict,
    chunk_analyses: list[dict],
    chunk_meta: list,
    source_budget: int,
) -> str:
    """Build the shared Stage 2.4/2.6 whole-source context.

    Layout:
      1. final rolling digest;
      2. every validated chunk analysis;
      3. bounded raw evidence from every source chunk.

    Single-chunk sources reserve most room for raw text. Multi-chunk sources
    reserve most room for the cross-chunk analyses while still representing
    every raw chunk. The returned string never exceeds ``source_budget``.
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
    analysis_payloads: list[str] = []
    raw_payloads: list[str] = []
    headings: list[str] = []
    for analysis, meta in zip(chunk_analyses, chunk_meta):
        clean_analysis = {
            key: value
            for key, value in (analysis or {}).items()
            if (
                not str(key).startswith("_")
                and key != "updated_global_digest"
            )
        }
        analysis_payloads.append(json.dumps(
            clean_analysis,
            ensure_ascii=False,
            indent=2,
            default=str,
        ))
        raw_payloads.append(str(meta[1] or ""))
        headings.append(str(meta[3] or ""))

    remaining = budget - len(header) - 2
    digest_budget = min(
        24_000,
        max(1_000, int(budget * 0.15)),
        remaining,
    )
    digest_section = _fit_section(
        "## Final Global Digest",
        digest_text,
        digest_budget,
    )
    remaining -= len(digest_section)
    remaining = max(0, remaining - 2)

    if len(chunk_analyses) <= 1:
        analysis_budget = int(remaining * 0.28)
    else:
        analysis_budget = int(remaining * 0.68)
    raw_budget = max(0, remaining - analysis_budget - 2)

    analysis_section = _render_chunk_section(
        "## Per-Chunk Analyses",
        analysis_payloads,
        headings,
        analysis_budget,
    )
    raw_section = _render_chunk_section(
        "## Bounded Raw Source Evidence",
        raw_payloads,
        headings,
        raw_budget,
    )
    result = "\n\n".join(
        section for section in (header.rstrip(), digest_section,
                                analysis_section, raw_section)
        if section
    )
    return result[:budget]
