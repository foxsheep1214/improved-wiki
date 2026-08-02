from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from _config import Config
from _core import record_rate_limit as _record_rate_limit
from _schema import (
    list_existing_slugs,
    load_purpose_md,
    load_schema_md,
    schema_candidate_routes,
    schema_prompt_text,
)
from _llm_api import (
    _is_retryable_exception,
    _retry_jitter,
    call_anthropic_protocol,
)
from _parse import parse_yaml_block
from _stage_2_base import (
    _stage_2_title_cjk_bigrams,
    _stage_2_title_words,
)
from _language import build_language_directive

# ── Token estimation (tiktoken if installed, else CJK-aware heuristic) ──
# tiktoken is optional: the pipeline must not hard-depend on it. The heuristic
# counts CJK/kana/hangul as ~1 token each and Latin/other as ~1 token / 4 chars,
# which tracks real tokenizer output within ~15% for mixed technical text — good
# enough to keep a chunk under a token budget regardless of language.
try:
    import tiktoken as _tiktoken
    _ENCODER = _tiktoken.get_encoding("cl100k_base")
except Exception:
    _ENCODER = None


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    if _ENCODER is not None:
        return len(_ENCODER.encode_ordinary(text))
    cjk = sum(
        1 for ch in text
        if "一" <= ch <= "鿿"   # CJK unified ideographs
        or "぀" <= ch <= "ヿ"   # hiragana + katakana
        or "가" <= ch <= "힯"   # hangul
    )
    other = len(text) - cjk
    return cjk + other // 4 + 1


_HEADING_RE = re.compile(r"^#{1,6}\s+.+$", re.MULTILINE)
_FENCE_RE = re.compile(r"^[ \t]*(```+|~~~+)", re.MULTILINE)

# Rolling-digest cap fed from chunk N into chunk N+1's prompt. NashSU parity:
# ingest.ts `LONG_SOURCE_DIGEST_MAX = 15_000` — a fixed constant, deliberately
# not scaled to the model context window (user decision 2026-07-09).
_DIGEST_PROMPT_CAP = 15_000

# Cap on the existing-wiki slug list embedded in each 2.2 chunk prompt. The
# uncapped list grew with the wiki (6,253 pages → one 259KB prompt line,
# repeated per chunk — observed live 2026-07-09, and it broke answering
# subagents' Read tooling). NashSU trims its Current Wiki Index to 40K chars
# (ingest.ts buildChunkAnalysisSystemPrompt); Stage 2.4's prompt sections
# already rank-and-cap. 1000 slugs ≈ 40K chars — the same budget.
_EXISTING_SLUGS_CAP = 1000


def _stage_2_2_cap_existing_slugs(existing_slugs: list, chunk_text: str) -> list:
    """Bound the existing-wiki slug list shown to a chunk-analysis prompt.

    Rank by relevance to THIS chunk's text — containment of the slug's tokens
    in the chunk's token set (ASCII words ∪ CJK bigrams, reusing the 2.4
    linkable-fill tokenizers) — keep the best _EXISTING_SLUGS_CAP, alphabetize
    for stable presentation. The chunk text is fixed for the whole ingest, so
    the ranked prefix (and hence the conversation-handoff prompt hash) is
    stable across resumes. An alphabetical cut would systematically drop
    late-sorting CJK slugs — the same disease _rank_linkable_fill fixed for
    Stage 2.4.
    """
    if len(existing_slugs) <= _EXISTING_SLUGS_CAP:
        return existing_slugs
    from _stage_2_4_generation import _linkable_relevance_tokens
    ref = _stage_2_title_words(chunk_text) | _stage_2_title_cjk_bigrams(chunk_text)

    def _score(slug: str) -> float:
        cand = _linkable_relevance_tokens(slug)
        if not cand:
            return 0.0
        return len(cand & ref) / len(cand)

    ranked = sorted(existing_slugs, key=lambda s: (-_score(s), s))
    return sorted(ranked[:_EXISTING_SLUGS_CAP])

# Fraction of the window scanned backwards for a clean boundary.
_SEARCH_FRAC = 0.15
# A trailing chunk smaller than this fraction of the token budget is merged back
# into its predecessor instead of wasting a full analyze+generate round-trip.
_MIN_TAIL_FRAC = 0.25


def _stage_2_2_find_protected_ranges(text: str) -> list[tuple[int, int]]:
    """Char ranges that must never be split: fenced code blocks and markdown
    tables. Returns sorted, non-overlapping ``(start, end)`` spans."""
    ranges: list[tuple[int, int]] = []

    # Fenced code blocks: a closing fence must use the same marker character,
    # be at least as long as its opener, and contain no trailing info string.
    # Pairing every two fence-looking lines is unsafe for OCR/source excerpts:
    # a literal `````asm`` line inside a `````txt`` block would otherwise be
    # mistaken for its close, shifting every later pair and protecting a huge
    # span of ordinary prose from chunking.
    open_fence: tuple[int, str, int] | None = None
    for match in _FENCE_RE.finditer(text):
        marker = match.group(1)
        line_end = text.find("\n", match.end())
        content_end = len(text) if line_end == -1 else line_end
        trailing = text[match.end():content_end]

        if open_fence is None:
            open_fence = (match.start(), marker[0], len(marker))
            continue

        open_pos, open_char, open_len = open_fence
        is_close = (
            marker[0] == open_char
            and len(marker) >= open_len
            and not trailing.strip()
        )
        if not is_close:
            continue

        end = len(text) if line_end == -1 else line_end + 1
        ranges.append((open_pos, end))
        open_fence = None

    def _in_fence(pos: int) -> bool:
        return any(s <= pos < e for s, e in ranges)

    # Markdown tables: runs of >=2 consecutive lines containing a pipe, outside
    # any code fence.
    run_start: int | None = None
    run_end = 0
    pos = 0
    for line in text.splitlines(keepends=True):
        line_end = pos + len(line)
        is_table_line = "|" in line and not _in_fence(pos)
        if is_table_line:
            if run_start is None:
                run_start = pos
            run_end = line_end
        else:
            if run_start is not None and text.count("\n", run_start, run_end) >= 1:
                ranges.append((run_start, run_end))
            run_start = None
        pos = line_end
    if run_start is not None and text.count("\n", run_start, run_end) >= 1:
        ranges.append((run_start, run_end))

    return sorted(ranges)


def _stage_2_2_range_at(pos: int, ranges: list[tuple[int, int]]) -> tuple[int, int] | None:
    for s, e in ranges:
        if s < pos < e:
            return (s, e)
        if s >= pos:
            break
    return None


def _stage_2_2_pick_boundary(text, lo, hi, heading_positions, protected) -> int:
    """Best cut index in [lo, hi): heading > paragraph > newline > CJK/EN
    sentence end. Skips boundaries that fall inside a protected range. Returns
    the exclusive cut index, or -1 if none found."""
    for hp in reversed(heading_positions):
        if lo <= hp < hi and _stage_2_2_range_at(hp, protected) is None:
            return hp  # cut before the heading so it leads the next chunk
    for sep, off in (("\n\n", 2), ("\n", 1), ("。", 1), (". ", 2)):
        idx = text.rfind(sep, lo, hi)
        while idx != -1 and _stage_2_2_range_at(idx, protected) is not None:
            idx = text.rfind(sep, lo, idx)
        if idx != -1:
            return idx + off
    return -1


def _stage_2_2_snap_out(start: int, end: int, protected) -> int:
    """If ``end`` lands inside a protected block, move it to a safe edge: before
    the block (block leads the next chunk) when possible, else after it.

    Guard against a large block (e.g. a multi-thousand-char OCR table) that
    starts early in the window: snapping back to its start would collapse the
    chunk to a tiny pre-table slice, wasting a generation round-trip on near-
    empty text. Only snap back when it leaves at least half the attempted
    window; otherwise snap forward past the block (let the chunk overflow to
    include the whole table)."""
    r = _stage_2_2_range_at(end, protected)
    if r is None:
        return end
    attempted = end - start
    if r[0] > start and (r[0] - start) >= attempted // 2:
        return r[0]
    return r[1]


def _stage_2_2_chunk_text(text: str, target_chars: int, overlap_chars: int,
                          *, target_tokens: int | None = None) -> list[str]:
    """Split text into overlapping, token-bounded chunks.

    NashSU parity (ingest.ts L2107-2205): prefers markdown heading boundaries
    (H1-H6), then paragraph breaks, then sentence ends. Beyond parity:

    - **Token sizing**: the window is sized to ``target_tokens`` tokens,
      converted to chars via this text's measured chars/token ratio and capped
      at the hard char ceiling ``target_chars``. Keeps CJK and Latin chunks at a
      comparable *token* size instead of char size.
    - **Protected blocks**: never cuts inside a fenced code block or markdown
      table.
    - **Tail merge**: a tiny trailing chunk is folded into its predecessor.
    """
    if target_tokens is None:
        target_tokens = target_chars  # config target_chars is token-scale (derived from context window)

    if _estimate_tokens(text) <= target_tokens and len(text) <= target_chars:
        return [text]

    # Size the char window to ~target_tokens tokens for THIS text's language mix,
    # bounded by the hard char ceiling (target_chars).
    chars_per_token = len(text) / max(1, _estimate_tokens(text))
    window = min(int(target_tokens * chars_per_token), target_chars)
    window = max(window, 2000)  # never absurdly small

    # Overlap scales with the ACTUAL chunk size (NashSU parity: overlapChars =
    # clamp(chunk * 0.08, 800, 3000)). The passed ``overlap_chars`` (config, =3000)
    # is the upper cap; small chunks get proportionally less. At large-context
    # chunk sizes 8% far exceeds the cap, so this stays at 3000 there — it only
    # shrinks once chunks fall below ~37.5K chars (small books / small context).
    overlap_chars = max(800, min(overlap_chars, int(window * 0.08)))

    print(f"[chunk] Splitting {len(text)} chars (~{_estimate_tokens(text)} tok) into "
          f"~{target_tokens}-tok chunks (~{window} chars/chunk)...", flush=True)

    heading_positions = [m.start() for m in _HEADING_RE.finditer(text)]
    protected = _stage_2_2_find_protected_ranges(text)

    spans: list[tuple[int, int]] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + window, n)
        if end >= n:
            spans.append((start, n))
            break

        search_start = max(start, end - int(window * _SEARCH_FRAC))
        boundary = _stage_2_2_pick_boundary(text, search_start, end, heading_positions, protected)
        if boundary > start:
            end = boundary
        end = _stage_2_2_snap_out(start, end, protected)
        if end <= start:  # protected block fills the whole window — let it overflow
            r = _stage_2_2_range_at(start + 1, protected)
            end = r[1] if r else min(start + window, n)

        spans.append((start, end))
        new_start = end - overlap_chars
        start = new_start if new_start > start else end

    # Tail merge: fold an undersized final chunk into its predecessor.
    if len(spans) >= 2:
        s, e = spans[-1]
        if _estimate_tokens(text[s:e]) < target_tokens * _MIN_TAIL_FRAC:
            spans[-2] = (spans[-2][0], e)
            spans.pop()

    chunks = [c for c in (text[s:e].strip() for s, e in spans) if c]
    print(f"[chunk] Done — {len(chunks)} chunks "
          f"(tokenizer: {'tiktoken' if _ENCODER else 'heuristic'})", flush=True)
    return chunks


# ── Stage 2.2 chapter anchors ──
# OCR'd books promote front-matter titles ("出版说明", "目录") and figure
# captions to markdown headings, so the generic nearest-heading ancestor stack
# mislabeled nearly every chunk. Explicit chapter markers are far more reliable
# anchors; numeric section headings are the fallback tier when a book has none.
_CHAPTER_ANCHOR_RE = re.compile(
    r"^#{1,3}\s*(第[一二三四五六七八九十百0-9]+章[^\n]*|Chapter\s+\d+[^\n]*)",
    re.MULTILINE | re.IGNORECASE)
# Letter-spaced chapter-opener typography (Wiley ELINT live incident,
# 2026-07-10): some books' decorative chapter-title-page graphic OCRs as a
# BARE line of widely spaced single letters — "C H A P T E R 1", two-digit
# chapters even space the digits ("C H A P T E R 1 0") — sitting above the
# real "# <Chapter Title>" H1, not as a markdown heading itself. Meanwhile
# that same book's own Table of Contents lists each chapter as "## CHAPTER N"
# (OCR promotes TOC lines to real headings), which _CHAPTER_ANCHOR_RE matches
# perfectly — 100% false-positive on TOC noise, 0% match on the real openers.
# This anchor's true position is always far later in the book than any TOC
# mention, so once detected it naturally wins the "last anchor before
# chunk_end" comparison over the TOC's early-clustered noise.
_CHAPTER_SPACED_RE = re.compile(r"^C\s+H\s+A\s+P\s+T\s+E\s+R\s+((?:\d\s*)+)$",
                                 re.MULTILINE)
_NUMERIC_HEADING_RE = re.compile(r"^#{1,3}\s*(\d+(?:\.\d+)*[ \t][^\n]*)", re.MULTILINE)
_FRONT_MATTER_LABEL = "前置材料（前言/目录）"


def _stage_2_2_resolve_chunk_heading_path(text: str, chunk_start: int, chunk_end: int) -> str:
    """Resolve the heading label for a chunk's span, chapter-markers first.

    Chapter anchors (第N章 / Chapter N, else numeric section headings) are
    scanned and the label reflects the chunk's SPAN: the chapter most recently
    opened at chunk_start plus, if different, the last chapter opened before
    chunk_end — "第2章 MTI雷达 → 第3章 AMTI". A chunk starting before chapter 1
    gets the front-matter label, so OCR pseudo-headings (出版说明/目录/figure
    captions) can no longer leak into the path.

    Texts without any chapter anchor fall back to the original behavior
    (NashSU parity): nearest H1-H6 heading before chunk_start plus its ancestor
    stack, e.g. "Chapter 3 > Section 3.2", or "" if no heading found.
    """
    anchors = [(m.start(), m.group(1).strip())
               for m in _CHAPTER_ANCHOR_RE.finditer(text)]
    for m in _CHAPTER_SPACED_RE.finditer(text):
        num = re.sub(r"\s+", "", m.group(1))
        anchors.append((m.start(), f"Chapter {num}"))
    anchors.sort(key=lambda a: a[0])
    if not anchors:
        anchors = [(m.start(), m.group(1).strip())
                   for m in _NUMERIC_HEADING_RE.finditer(text)]
    if anchors:
        start_idx = end_idx = -1  # -1 → before the first chapter (front matter)
        for i, (pos, _title) in enumerate(anchors):
            if pos <= chunk_start:
                start_idx = i
            if pos < chunk_end:
                end_idx = i
            else:
                break
        start_label = anchors[start_idx][1] if start_idx >= 0 else _FRONT_MATTER_LABEL
        if end_idx > start_idx:
            return f"{start_label} → {anchors[end_idx][1]}"
        return start_label

    _HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)
    _heading_stack: list[tuple[int, str]] = []  # (level, title)

    for m in _HEADING_RE.finditer(text):
        if m.start() > chunk_start:
            break
        level = len(m.group(1))
        title = m.group(2).strip()
        # Pop headings of same or deeper level
        while _heading_stack and _heading_stack[-1][0] >= level:
            _heading_stack.pop()
        _heading_stack.append((level, title))

    if _heading_stack:
        return " > ".join(h[1] for h in _heading_stack)
    return ""


# ---------- Stage 2.2 prompt building + chunking ----------

_SOURCE_KIND_LABELS = {
    "applicationnote": "application note",
    "book": "book",
    "datasheet": "datasheet",
    "designexample": "design example",
    "news": "news article",
    "paper": "paper",
    "presentation": "presentation",
    "standard": "standard",
}


def _stage_2_2_source_kind(template: str, file_path: Path) -> str:
    """Return a stable source kind without mistaking a topic folder for it.

    ``raw/Paper/<topic>/x.pdf`` previously used ``file_path.parent.name`` and
    told the model that ``<topic>`` was the document type. Prefer the selected
    digest template; fall back to the first path component below ``raw/``.
    """
    match = re.match(r"\s*#\s*digest-([a-z0-9_-]+)", template or "", re.I)
    if match:
        key = match.group(1).lower().replace("-", "").replace("_", "")
        return _SOURCE_KIND_LABELS.get(key, key.replace("_", " "))

    parts = file_path.parts
    for index, part in enumerate(parts[:-1]):
        if part.lower() == "raw" and index + 1 < len(parts) - 1:
            key = parts[index + 1].lower().replace("-", "").replace("_", "")
            return _SOURCE_KIND_LABELS.get(key, "source")
    return "source"


def _stage_2_2_digest_meta_template(source_kind: str) -> str:
    """Build the compatibility metadata block for the actual source kind.

    ``book_meta`` is retained as an internal artifact key because existing
    checkpoints and downstream validators consume it. Its fields, however,
    must describe the real source instead of coercing papers into
    publisher/textbook metadata.
    """
    common = (
        "  book_meta:  # compatibility key used for every source type\n"
        '    title: "..."\n'
        '    authors: ["..."]\n'
        '    year: "..."\n'
        f'    source_kind: "{source_kind}"'
    )
    if source_kind == "book":
        return (
            common
            + '\n    publisher: "..."\n'
            + '    granularity: "textbook" | "manual"   '
            + '# "manual" ONLY for implementation/maintenance monographs'
        )
    if source_kind == "paper":
        return common + '\n    venue: "..."\n    doi: "..."\n    url: "..."'
    return common + '\n    venue: "..."\n    publisher: "..."\n    url: "..."'


def _stage_2_2_build_template_section(template: str, file_path: Path, max_chars: int = 4000) -> str:
    """Build the template injection section for a Stage 2.2 prompt.

    Truncates the template to *max_chars* and wraps it in a
    ``# Document Type Instructions`` block.  Returns an empty string when
    *template* is falsy.
    """
    if not template:
        return ""
    template_trimmed = template[:max_chars]
    source_kind = _stage_2_2_source_kind(template, file_path)
    return f"""
# Document Type Instructions
The source is a **{source_kind}**. Follow these type-specific conventions as
content-emphasis guidance. If the template names a type-specific metadata block
such as `paper_meta`, map those fields into the required compatibility
`book_meta` block in the Stage 2.2 output below; do not emit a competing second
metadata block.
<template>
{template_trimmed}
</template>

"""


def _stage_2_2_schema_types_block(
    config: Config,
    wiki_index_context: str = "",
) -> str:
    """Inject NashSU-style schema, purpose, and frozen current-index context."""
    text = load_schema_md(config)
    schema_context = schema_prompt_text(text)
    purpose_context = load_purpose_md(config).strip()[:6000]
    index_context = str(wiki_index_context or "").strip()[:40_000]
    if not schema_context and not purpose_context and not index_context:
        return ""

    routes = schema_candidate_routes(text)
    route_lines = ", ".join(
        f"{page_type} → wiki/{route}/"
        for page_type, route in sorted(routes.items())
    ) or "(none — generate only the pipeline-managed source/entity/concept pages)"
    schema_block = (
        "\n# Project Schema and Routing (AUTHORITATIVE)\n"
        "<schema>\n"
        f"{schema_context}\n"
        "</schema>\n"
        "Treat the Page Types table as the primary routing and frontmatter contract. "
        "For schema-typed candidates, `type` and `folder` MUST use the exact mapping "
        "below in the `schema_typed_candidates` output field. Recommend a typed page "
        "only when this source actually supports creating it or materially updating "
        "an existing page; NEVER invent goals, habits, journal entries, decisions, "
        "findings, or hypotheses.\n"
        f"Eligible source-grounded schema types: {route_lines}\n"
    ) if schema_context else ""
    lifecycle_lines: list[str] = []
    if "synthesis" in routes:
        lifecycle_lines.extend([
            "- `synthesis` is a cross-cutting summary or conclusion, distinct "
            "from the mandatory source summary. The current source may seed a "
            "new synthesis when it materially connects multiple concepts, "
            "findings, entities, or existing wiki topics; later sources merge "
            "into and refine that page.",
            "- Do not require a synthesis to be multi-source before its first "
            "creation unless the project schema explicitly requires that. "
            "Never infer cross-source facts from index titles alone.",
        ])
    if "thesis" in routes:
        lifecycle_lines.extend([
            "- `thesis` is a falsifiable working hypothesis and a living page. "
            "The current source may seed a `speculative` thesis when it "
            "explicitly advances a supported hypothesis; later evidence updates "
            "its confidence/status and supporting or refuting links.",
            "- Do not wait for multi-source consensus before creating a thesis "
            "unless the project schema explicitly requires it. Do not convert "
            "an ordinary source claim into a hypothesis.",
        ])
    lifecycle_block = (
        "\n# NashSU Synthesis / Thesis Lifecycle\n"
        + "\n".join(lifecycle_lines)
        + "\n"
    ) if lifecycle_lines else ""
    purpose_block = (
        "\n# Wiki Purpose\n"
        "<purpose>\n"
        f"{purpose_context}\n"
        "</purpose>\n"
        "Use the purpose to prioritize relevant material; it never overrides source "
        "evidence or the schema's routing contract.\n"
    ) if purpose_context else ""
    index_block = (
        "\n# Current Wiki Index (FROZEN FOR THIS SOURCE)\n"
        "<current_wiki_index>\n"
        f"{index_context}\n"
        "</current_wiki_index>\n"
        "This matches NashSU's stable ingest context. Use it to preserve page "
        "identity, recognize an existing synthesis/thesis that should be "
        "updated, and avoid duplicate pages. Index titles/descriptions are "
        "navigation context, not factual evidence; ground every new statement "
        "in the current source or explicitly available evidence.\n"
    ) if index_context else ""
    return schema_block + lifecycle_block + purpose_block + index_block


def _stage_2_2_granularity_block(accumulated_digest) -> str:
    """D2 (user ruling 2026-07-02): book-level granularity switch.

    Source: book_meta.granularity in the accumulated digest (rolled up by
    prior chunks; the first chunk has no prior digest yet → no granularity).
    For a "manual" (implementation/maintenance monograph organized around
    one device's circuits) inject a stronger COARSE-granularity directive on
    top of the always-on granularity gate below. "textbook" or absent → empty
    string (existing gate only).
    """
    book_meta = None
    if isinstance(accumulated_digest, dict):
        book_meta = accumulated_digest.get("book_meta")
    elif accumulated_digest:
        s_str = str(accumulated_digest).strip()
        if s_str and s_str not in ("{}", '""'):
            for _loader in (lambda t: __import__("json").loads(t),
                            lambda t: __import__("yaml").safe_load(t)):
                try:
                    d = _loader(s_str)
                    if isinstance(d, dict):
                        book_meta = d.get("book_meta")
                        break
                except Exception:
                    pass
    if not isinstance(book_meta, dict):
        return ""
    if str(book_meta.get("granularity", "")).strip().lower() != "manual":
        return ""
    return (
        "\n# Book Granularity: MANUAL — extract COARSE\n"
        "The accumulated digest classifies this book as a device manual "
        "(implementation/maintenance monograph organized around one device's "
        "circuits).\n"
        "COARSE granularity: chip/board/pin-level implementation details are NOT "
        "concepts — fold into system-level pages or entities; target "
        "system/subsystem-level concepts only.\n"
    )


def _stage_2_2_build_overlap_section(overlap_before: str) -> str:
    """Format the overlap boundary text for continuity context (NashSU parity).

    Uses paragraph/sentence-aware boundary trimming (not a raw tail slice)
    to give the LLM clean context when a concept spans a chunk boundary.
    Returns an empty string when *overlap_before* is falsy.
    """
    if not overlap_before:
        return ""
    overlap_for_boundary = overlap_before[-800:]  # search in last 800 chars
    boundary = -1
    # Priority 1: paragraph break in overlap window
    boundary = overlap_for_boundary.rfind("\n\n")
    # Priority 2: sentence boundary
    if boundary == -1:
        m = re.search(r'[.!?。！？]\s+', overlap_for_boundary)
        if m:
            boundary = m.start() + 1
    # Fallback: start at a word boundary
    if boundary == -1:
        boundary = max(0, len(overlap_for_boundary) - 500)
    overlap_trimmed = overlap_for_boundary[boundary:][-500:]
    return f"""
# Continuity: text right before this chunk (may span sentence boundary)
<overlap>
{overlap_trimmed}
</overlap>

"""


def _stage_2_2_build_prompt(
    chunk_text: str,
    chunk_index: int,
    chunk_total: int,
    global_digest: dict,
    file_path: Path,
    config: Config,
    template: str = "",
    accumulated_digest: str = "",
    overlap_before: str = "",
    heading_path: str = "",
    existing_slugs: list | None = None,
    wiki_index_context: str = "",
) -> str:
    """Build the prompt for Stage 2.2: Chunk Analysis.

    If accumulated_digest is provided (sequential mode), it replaces the
    static global_digest as the primary context — giving later chunks the
    benefit of all previous chunks' discoveries (NashSU parity).

    If overlap_before is provided, it's the tail-end text from the previous
    chunk that this chunk overlaps with — gives the LLM continuity context
    when a sentence/concept spans a chunk boundary (NashSU parity).

    If heading_path is provided, it tells the LLM which chapter/section
    hierarchy this chunk belongs to (NashSU parity: chunk.headingPath).

    ``existing_slugs`` and ``wiki_index_context`` are per-source SNAPSHOTS
    taken when the source first enters Stage 2.2 (persisted under
    "slugs_snapshot_2_2" and "wiki_index_snapshot_2_2" by _ingest_chunks).
    Stage 2.2 is contractually snapshot-stable: fresh live reads while a
    parallel batch source writes wiki pages would make prompt hashes drift and
    cause conversation cache misses on every resume. ``existing_slugs=None``
    retains a live-read fallback for legacy callers/tests only; the pipeline
    always passes both snapshots.
    """
    if accumulated_digest:
        # Sequential mode: use accumulated digest from previous chunks
        digest_str = accumulated_digest
    else:
        # Legacy / first-chunk mode: crop global digest to essentials
        digest_compact = {}
        for key in ("book_meta", "outline", "key_entities", "key_concepts"):
            if key in global_digest:
                digest_compact[key] = global_digest[key]
        digest_str = json.dumps(digest_compact, ensure_ascii=False, indent=2)
    # NashSU parity (user decision 2026-07-09): the chunk→chunk digest transfer
    # matches NashSU's volume AND granularity. NashSU ingest.ts caps the rolling
    # digest at a FIXED `LONG_SOURCE_DIGEST_MAX = 15_000` chars — deliberately
    # NOT scaled to the model context (chunk size scales; the digest does not) —
    # paired with a "compact document-level digest" instruction so the LLM
    # condenses rather than accumulates verbatim (see the updated_global_digest
    # template below). Detail is NOT lost by this: each chunk's full analysis is
    # persisted in chunk_analyses; Stage 2.4 selects eligible key page candidates
    # and synthesizes core claims. The digest is only the
    # lightweight continuity channel. Earlier fixed caps (6K, 24K) and an
    # interim dynamic cap (target_chars) predate this parity decision.
    if len(digest_str) > _DIGEST_PROMPT_CAP:
        digest_str = digest_str[:_DIGEST_PROMPT_CAP] + "\n... (truncated)"
    if existing_slugs is None:
        existing_slugs = list_existing_slugs(config)
    existing_slugs = _stage_2_2_cap_existing_slugs(list(existing_slugs), chunk_text)

    source_kind = _stage_2_2_source_kind(template, file_path)
    digest_meta_template = _stage_2_2_digest_meta_template(source_kind)
    template_section = _stage_2_2_build_template_section(template, file_path, max_chars=2000)

    overlap_section = _stage_2_2_build_overlap_section(overlap_before)

    schema_types_section = _stage_2_2_schema_types_block(
        config, wiki_index_context=wiki_index_context)

    granularity_section = _stage_2_2_granularity_block(accumulated_digest)

    # ── Heading path (NashSU parity: chunk.headingPath) ──
    heading_section = ""
    if heading_path:
        heading_section = f"""
# Current location in the source
You are analyzing content from: **{heading_path}**

"""

    language_directive = build_language_directive(chunk_text)

    # NashSU v0.6.6 policy: identify only genuinely important key
    # entities/concepts and stay "thorough but concise". There is deliberately no
    # per-character target, minimum count, completeness ledger, or instruction to
    # turn every mentioned building block into a page candidate.
    density_hint = (
        f"This chunk is ~{len(chunk_text):,} characters"
        + (f" spanning **{heading_path}**" if heading_path else "")
        + ". Be thorough but concise. Focus on what is genuinely important: identify "
        "new or materially updated key concepts/entities, not an inventory of every "
        "term, prerequisite, or passing mention. There is no numeric target; do not "
        "pad, split one coherent topic into several entries, or copy background "
        "knowledge merely because it appears in the text."
    )

    return f"""{language_directive}

# Role
You are the LLM maintainer of a Karpathy-pattern personal knowledge base.
You are performing **Stage 2.2: Chunk Analysis** (chunk {chunk_index + 1}/{chunk_total}) of a source ingest pipeline.
{template_section}{schema_types_section}{granularity_section}
# Context: Accumulated Global Digest
This digest is cumulative context rolled up across all PREVIOUS chunks — use
it for continuity and to avoid re-writing the same *prose* twice.
Keep stable names consistent with the existing wiki and prior digest: when this
chunk re-encounters a concept/entity already named there, reuse that EXACT name
(stable names → stable slugs → downstream dedup works).
It is prior cross-chunk context, not a checklist to reproduce. If this chunk
materially updates an earlier concept/entity, reuse its stable name and record
the update. Otherwise do not repeat it merely to keep an exhaustive inventory.
Deduplication against REAL existing pages still happens downstream
(Stage 2.3/2.4).

```yaml
{digest_str}
```
{heading_section}{overlap_section}
# Input
- Source: {file_path.stem}
- Chunk {chunk_index + 1} of {chunk_total}
- Extracted text of this chunk:
<extracted_text>
{chunk_text}
</extracted_text>

- Existing wiki pages: {', '.join(existing_slugs)}

# Task
{density_hint}

Analyze THIS CHUNK of the source. Extract:

1. **Key concepts** — theories, methods, techniques, and phenomena that are new
   or materially updated in this chunk and genuinely important to understanding
   the source. Recommend a standalone page only when the topic is coherent,
   reusable, and substantively explained or applied. Exclude passing mentions,
   prerequisites used only as background, and facets better kept together on one
   page. Chip/board implementation details are not concepts; fold them into the
   relevant system page or entity.
2. **Key entities** — people, organizations, products/systems, standards, tools,
   or datasets that are central or materially discussed, not every proper noun.
   A named theoretical/statistical model, method, or technique (e.g. Swerling
   model, matched filter) is a CONCEPT, not an entity.
3. Core claims/findings, their evidence, formulas, and material data points
4. Connections to existing wiki pages (if any)
5. An **Updated Global Digest** — a COMPACT document-level digest that
   incorporates this chunk and preserves prior cross-chunk context. This is a
   continuity digest, NOT an archive. Preserve the prior cross-chunk context
   needed to interpret later chunks, but condense or drop peripheral detail.
   Your full per-chunk detail is already saved separately (concepts_found /
   claims / formulas above); do NOT duplicate it here. Target well under
   15,000 characters — anything beyond is hard-truncated before the next
   chunk sees it.
6. **Schema-typed page candidates** — use only the eligible type→directory
   mappings in the authoritative schema block above (for example finding,
   methodology, thesis, comparison, or synthesis). Recommend a page only when
   the current source evidence genuinely satisfies that type's schema semantics
   and creates a useful new page or materially updates an indexed existing page.
   A later chunk may recommend a comparison or other typed page when THIS chunk
   plus the accumulated digest substantively establish it; the digest is
   continuity evidence, not permission to invent facts. Follow the NashSU
   synthesis/thesis lifecycle above: a synthesis candidate may be seeded from
   a source that supports a cross-cutting conclusion, and an explicitly
   supported working thesis may begin as speculative, unless the project schema
   imposes a stricter gate. NEVER invent goals, habits, journal entries,
   decisions, findings, hypotheses, or other records that are not present in
   the source.

# Output (YAML only, in ```yaml block)
```yaml
chunk_index: {chunk_index + 1}
chunk_total: {chunk_total}

# ⚠️ YAML STRING QUOTING (CRITICAL — bad escaping aborts the whole parse):
#   - ANY value containing a backslash (LaTeX: \\text \\frac \\propto \\cdot) or '$'
#     MUST be SINGLE-quoted. Single quotes treat '\\' as a literal char — no
#     escaping needed. Inside single quotes, double a literal ' as ''.
#   - NEVER put LaTeX or '$' in DOUBLE quotes: "\\text" → \\t becomes TAB,
#     "\\frac" → \\f becomes form-feed, "\\$x" is an invalid escape that ABORTS
#     the YAML parse and loses every concept below it.
#   - Plain prose without \\ or $ may use double or single quotes.
#
# ⚠️ FORMULAS — LaTeX ALWAYS (the basis for understanding; prevents drift):
#   EVERY formula you record — whether in a definition, in key_details, or in the
#   formulas list — MUST be written as LaTeX, transcribed VERBATIM from the source
#   (same variables, same form). Never paraphrase a formula into words and never
#   reconstruct it from memory. LaTeX-bearing values follow the single-quote rule
#   above.

entities_found:
  - name: "..."
    significance: "..."     # why this entity matters (1 sentence)

concepts_found:
  - name: "..."
    importance: "core" | "supporting" | "mentioned"
    definition: "..."      # the concept's definition as stated in the source
    key_details: ["...", "..."]   # concise source-grounded facts/formulas/rules; [] is valid

# ⚠️  CONCEPT NAMING RULES:
#   - name MUST be a SHORT, SPECIFIC topic (3-6 words), e.g. "DC-Link Voltage Control", "IGBT Thermal Modeling"
#   - NEVER use the source title or filename as a concept name
#   - NEVER include "Chunk N", "Chapter N" or page numbers in the name
#   - Create separate entries only for independently useful topics; keep facets
#     of one coherent topic together
#   - Use the actual technical term from the source, not a generic description
#   - `mentioned` is context only and will not become a standalone page. Prefer
#     omitting such items unless retaining the name prevents ambiguity.

# ⚠️  CLAIM EXTRACTION RULES (ground every claim in the source text):
#   1. READ the <extracted_text> for THIS chunk before listing claims.
#      Do NOT generate claims from domain knowledge or memory — every claim
#      must be grounded in text you actually read in this chunk.
#   2. EVERY claim MUST have an evidence field citing a SPECIFIC source-text
#      anchor: section number (§X.X), equation number (式(N) or Eq. (N)),
#      figure number (Figure N / 图N.N), or table number (Table N).
#      Generic evidence like "Ch.3" or "this section" is NOT acceptable —
#      use the most specific anchor available. (Front-matter chunks — preface,
#      TOC, colophon before chapter 1 — may cite the preface/section name when
#      no numbered anchor exists in the text.)
#   3. Keep only core claims/findings. Their number is determined by the source;
#      zero is valid for a chunk with no substantive claim. Never pad to a quota.
#   4. Claims must be falsifiable/actionable assertions (quantitative results,
#      design rules, comparative verdicts, limits, mechanisms) — NOT scope
#      descriptions or bare definitions.
#   5. `source_quotes` is optional audit support, not a count gate. Include a
#      short exact excerpt only when it materially helps verify a claim.

source_quotes: |
  # Optional short verbatim excerpt(s) from THIS chunk with a precise anchor.
  # Leave empty when the claims' evidence anchors are sufficient. Example:
  # §2.3.4: "The Barker code of length 13 provides optimal peak sidelobe
  # level of -1/N for code length N."
  # 式(3.6): "Modulating waveform = exp(j*pi*tau*B*t^2)"

claims:
  - claim: "..."
    evidence: "§X.X or 式(N) or Figure N — specific source-text anchor (NOT generic chapter ref)"
    confidence: "high" | "medium" | "low"
    table_ref: "Table N or Figure N"   # for datasheets: REQUIRED; otherwise omit if no table/figure source
    page_ref: "p.NN"                   # for datasheets: REQUIRED; otherwise omit if not applicable

formulas:
  - formula: '\\text{{Energy}} = \\frac{{1}}{{2}} C V^2'   # SINGLE-quoted; transcribe verbatim, never paraphrase
    meaning: '...'
    table_ref: "Table N"      # cite source table/figure when available

connections_to_existing_wiki:
  - existing_page: "..."
    relationship: "extends" | "contrasts" | "applies" | "cites"

# Schema-typed page candidates (NashSU 0.6.6 parity). ONLY use eligible mappings
# listed in the authoritative schema block. The current source may create or
# materially update one when its evidence satisfies that type's schema
# semantics. Cross-chunk candidates may use the accumulated digest for
# continuity, and existing page identity may come from the frozen wiki index.
# Leave empty (`[]`) when no eligible type fits.
# NEVER invent goals/habits/journal/decisions/findings/hypotheses.
schema_typed_candidates:
  - type: "finding" | "methodology" | "thesis" | "comparison" | "synthesis" | "..."   # a schema-declared type
    name: "..."        # short specific kebab-case-friendly name (3-6 words)
    folder: "findings"  # the wiki/<folder>/ the page should land in
    rationale: "..."    # one sentence: why the source evidence supports this typed page

updated_global_digest: |
  # Compact Global Digest (after chunk {chunk_index + 1}/{chunk_total}) — NashSU parity
  # A compact document-level digest that incorporates this chunk and preserves
  # useful prior cross-chunk context. Keep the whole digest well under 15,000
  # chars — overflow is hard-truncated. Condense or drop peripheral detail under
  # budget pressure; retain stable names for concepts/entities that remain
  # genuinely important, keep key_claims to the source's core arguments, and
  # retain only genuinely supported schema-typed candidates needed by later
  # chunks. MUST contain the first 5 top-level keys below; the optional sixth
  # carries typed-candidate continuity. The FIRST chunk ESTABLISHES the
  # compatibility `book_meta` source-metadata block and outline; later chunks
  # refine them and append to the other fields. The key remains `book_meta`
  # for checkpoint compatibility even when source_kind is not a book.
{digest_meta_template}
  outline:
    - "Chapter/Section ..."
  key_entities:
    - name: "..."
      type: "person" | "organization" | "system" | "model"
  key_concepts:
    - name: "..."
      definition: "..."   # ONE short line, not a paragraph; no key_details here
  key_claims:
    - claim: "..."        # ONE line; keep only the source's MAIN arguments here
      evidence: "..."
  schema_typed_candidates:
    - type: "..."
      name: "..."
      rationale: "..."    # compact; omit weak or unsupported candidates

# Do not turn this compact digest into an additional exhaustive page inventory.
```
"""


class _YamlNotDictError(RuntimeError):
    """Stage 2.2 agent answered with YAML that parses to a non-dict (list /
    plain text). Treated as a parse failure: retried like a transient error,
    raised when retries are exhausted (no-silent-fallback)."""


class ChunkAnalysisValidationError(RuntimeError):
    """Stage 2.2 returned a mapping whose nested schema is unsafe to consume."""


_CHUNK_ANALYSIS_LIST_FIELDS = (
    "entities_found",
    "concepts_found",
    "claims",
    "formulas",
    "connections_to_existing_wiki",
    "schema_typed_candidates",
)


def _analysis_nonempty_string(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChunkAnalysisValidationError(
            f"{field} must be a non-empty string")
    return value.strip()


def normalize_and_validate_chunk_analysis(
    analysis: dict,
    *,
    expected_index: int | None = None,
    expected_total: int | None = None,
) -> dict:
    """Normalize optional fields and strictly validate Stage 2.2's contract.

    The previous boundary accepted any top-level mapping. A malformed YAML
    fallback could therefore turn ``concepts_found`` into a list of strings
    while the stage was still cached as complete. Downstream code then either
    crashed or silently coerced those strings. This function is the one schema
    gate used both immediately after parsing and when restoring a checkpoint.
    """
    if not isinstance(analysis, dict):
        raise ChunkAnalysisValidationError(
            f"analysis must be a mapping, got {type(analysis).__name__}")
    normalized = dict(analysis)

    for field in _CHUNK_ANALYSIS_LIST_FIELDS:
        value = normalized.get(field, [])
        if value is None:
            value = []
        if not isinstance(value, list):
            raise ChunkAnalysisValidationError(
                f"{field} must be a list, got {type(value).__name__}")
        if any(not isinstance(item, dict) for item in value):
            bad = next(item for item in value if not isinstance(item, dict))
            raise ChunkAnalysisValidationError(
                f"{field} items must be mappings, got "
                f"{type(bad).__name__}: {str(bad)[:80]}")
        normalized[field] = [dict(item) for item in value]

    for position, concept in enumerate(normalized["concepts_found"], 1):
        prefix = f"concepts_found[{position}]"
        concept["name"] = _analysis_nonempty_string(
            concept.get("name"), f"{prefix}.name")
        importance = _analysis_nonempty_string(
            concept.get("importance"), f"{prefix}.importance").lower()
        if importance not in {"core", "supporting", "mentioned"}:
            raise ChunkAnalysisValidationError(
                f"{prefix}.importance must be core/supporting/mentioned, "
                f"got {importance!r}")
        concept["importance"] = importance
        concept["definition"] = _analysis_nonempty_string(
            concept.get("definition"), f"{prefix}.definition")
        details = concept.get("key_details", [])
        if not isinstance(details, list):
            raise ChunkAnalysisValidationError(
                f"{prefix}.key_details must be a list")
        concept["key_details"] = [
            _analysis_nonempty_string(item, f"{prefix}.key_details")
            for item in details
        ]

    for position, entity in enumerate(normalized["entities_found"], 1):
        prefix = f"entities_found[{position}]"
        entity["name"] = _analysis_nonempty_string(
            entity.get("name"), f"{prefix}.name")
        entity["significance"] = _analysis_nonempty_string(
            entity.get("significance"), f"{prefix}.significance")

    for position, claim in enumerate(normalized["claims"], 1):
        prefix = f"claims[{position}]"
        claim["claim"] = _analysis_nonempty_string(
            claim.get("claim"), f"{prefix}.claim")
        claim["evidence"] = _analysis_nonempty_string(
            claim.get("evidence"), f"{prefix}.evidence")
        if "confidence" in claim:
            confidence = _analysis_nonempty_string(
                claim["confidence"], f"{prefix}.confidence").lower()
            if confidence not in {"high", "medium", "low"}:
                raise ChunkAnalysisValidationError(
                    f"{prefix}.confidence must be high/medium/low")
            claim["confidence"] = confidence

    if "source_quotes" in normalized:
        if normalized.get("source_quotes") not in (None, ""):
            normalized["source_quotes"] = _analysis_nonempty_string(
                normalized.get("source_quotes"), "source_quotes")
        else:
            normalized["source_quotes"] = ""

    for position, formula in enumerate(normalized["formulas"], 1):
        prefix = f"formulas[{position}]"
        formula["formula"] = _analysis_nonempty_string(
            formula.get("formula"), f"{prefix}.formula")
        formula["meaning"] = _analysis_nonempty_string(
            formula.get("meaning"), f"{prefix}.meaning")

    for position, connection in enumerate(
            normalized["connections_to_existing_wiki"], 1):
        prefix = f"connections_to_existing_wiki[{position}]"
        connection["existing_page"] = _analysis_nonempty_string(
            connection.get("existing_page"), f"{prefix}.existing_page")
        connection["relationship"] = _analysis_nonempty_string(
            connection.get("relationship"), f"{prefix}.relationship")

    for position, candidate in enumerate(
            normalized["schema_typed_candidates"], 1):
        prefix = f"schema_typed_candidates[{position}]"
        for field in ("type", "name", "folder", "rationale"):
            candidate[field] = _analysis_nonempty_string(
                candidate.get(field), f"{prefix}.{field}")

    digest = normalized.get("updated_global_digest")
    if isinstance(digest, str):
        if len(digest.strip()) <= 50:
            raise ChunkAnalysisValidationError(
                "updated_global_digest must be a substantive string")
        normalized["updated_global_digest"] = digest.strip()
    elif not isinstance(digest, dict) or not digest:
        raise ChunkAnalysisValidationError(
            "updated_global_digest must be a non-empty string or mapping")

    for field, expected in (
        ("chunk_index", expected_index),
        ("chunk_total", expected_total),
    ):
        if expected is None:
            continue
        try:
            actual = int(normalized.get(field))
        except (TypeError, ValueError):
            raise ChunkAnalysisValidationError(
                f"{field} must equal {expected}")
        if actual != expected:
            raise ChunkAnalysisValidationError(
                f"{field}={actual}, expected {expected}")
        normalized[field] = actual

    return normalized


def _stage_2_2_chunk_retries() -> int:
    """Max attempts per chunk (1 initial + N retries). Default 2 retries → 3 total attempts."""
    env = os.environ.get("LLM_CHUNK_RETRIES", "")
    if env:
        try:
            return max(0, int(env))
        except ValueError:
            pass
    return 2




def _stage_2_2_analyze_chunk(
    chunk: str,
    chunk_idx: int,
    chunk_total: int,
    global_digest: dict,
    accumulated_digest: str,
    overlap_before: str,
    heading_path: str,
    file_path: Path,
    config: Config,
    template: str = "",
    max_retries: int = 2,
    verbose: bool = False,
    existing_slugs: list | None = None,
    wiki_index_context: str = "",
) -> dict:
    """Analyze a single chunk.

    Used by Stage 2.2's serial analysis pass. Every chunk is analyzed first;
    Stage 2.3 association and the single consolidated Stage 2.4 generation call
    run only after the full analysis pass completes.

    Returns analysis dict with keys: concepts_found, entities_found, claims,
    formulas, connections_to_existing_wiki, digest_updates, plus _chunk_index,
    _chunk_size, _attempts.
    On failure (transient retries exhausted, or a non-retryable error):
    raises RuntimeError — no error-dict sentinel (no-silent-fallback; the
    cached prior chunks make a resume cheap).
    """
    prompt = _stage_2_2_build_prompt(
        chunk, chunk_idx, chunk_total, global_digest, file_path, config,
        template=template, accumulated_digest=accumulated_digest,
        overlap_before=overlap_before, heading_path=heading_path,
        existing_slugs=existing_slugs,
        wiki_index_context=wiki_index_context,
    )

    validation_feedback = ""
    for attempt in range(1 + max_retries):
        try:
            t0 = time.time()
            if attempt == 0:
                print(f"  [chunk {chunk_idx+1}/{chunk_total}] analyzing ({len(chunk):,} chars)...",
                      flush=True)
            active_prompt = prompt
            if validation_feedback:
                active_prompt += (
                    "\n\n# REQUIRED CORRECTION FOR THIS RETRY\n"
                    "The previous answer was rejected by the Stage 2.2 schema "
                    f"validator: {validation_feedback}\n"
                    "Return a fresh complete YAML answer following the exact "
                    "output schema above. Do not omit required nested fields "
                    "and do not turn mapping items into strings.\n"
                )
            response, stop_reason = call_anthropic_protocol(
                active_prompt, config,
                max_tokens=config.compute_max_tokens(8192))
            analysis = parse_yaml_block(response)
            if not isinstance(analysis, dict):
                raise _YamlNotDictError(
                    f"chunk {chunk_idx+1}/{chunk_total}: parse_yaml_block returned "
                    f"{type(analysis).__name__}, expected a YAML mapping (dict)")
            analysis = normalize_and_validate_chunk_analysis(
                analysis,
                expected_index=chunk_idx + 1,
                expected_total=chunk_total,
            )
            analysis["_chunk_index"] = chunk_idx + 1
            analysis["_chunk_size"] = len(chunk)
            analysis["_attempts"] = attempt + 1
            dt = time.time() - t0
            n_c = len(analysis.get("concepts_found") or [])
            n_e = len(analysis.get("entities_found") or [])
            tag = f" (retry #{attempt})" if attempt > 0 else ""
            print(f"  [chunk {chunk_idx+1}/{chunk_total}] analyze OK{tag} — "
                  f"{n_c} concepts, {n_e} entities, {dt:.0f}s")
            if verbose:
                print(f"    response: {response[:500]}...")
            break  # success — exit retry loop

        except Exception as e:
            schema_error = isinstance(
                e, (_YamlNotDictError, ChunkAnalysisValidationError))
            if attempt < max_retries and (
                    _is_retryable_exception(e) or schema_error):
                if schema_error:
                    validation_feedback = str(e)[:500]
                _record_rate_limit()
                wait = _retry_jitter(2.0, attempt)
                err_label = type(e).__name__
                print(f"  [chunk {chunk_idx+1}/{chunk_total}] analyze retry {attempt+1}/{1+max_retries}"
                      f" ({err_label}: {str(e)[:80]}) — {wait:.1f}s...")
                time.sleep(wait)
                continue
            print(f"  [chunk {chunk_idx+1}/{chunk_total}] analyze FAILED: {e}")
            # No error-dict sentinel: a failed chunk analysis must PAUSE the
            # ingest (no-silent-fallback). Prior chunks are cached, so a
            # resume after the transient clears is cheap.
            raise RuntimeError(
                f"Stage 2.2 chunk {chunk_idx+1}/{chunk_total} analysis failed "
                f"after {attempt+1} attempt(s): {type(e).__name__}: {e}") from e

    return analysis
