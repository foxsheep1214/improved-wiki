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
# (ingest.ts buildChunkAnalysisSystemPrompt); 2.4 (_LINKABLE_TOTAL_CAP) and
# 2.6 ([:1500]) already rank-and-cap. 1000 slugs ≈ 40K chars — same budget.
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
    2.4/2.6.
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


def _stage_2_1_find_protected_ranges(text: str) -> list[tuple[int, int]]:
    """Char ranges that must never be split: fenced code blocks and markdown
    tables. Returns sorted, non-overlapping ``(start, end)`` spans."""
    ranges: list[tuple[int, int]] = []

    # Fenced code blocks: pair consecutive fence markers (```/~~~).
    fences = [m.start() for m in _FENCE_RE.finditer(text)]
    for i in range(0, len(fences) - 1, 2):
        open_pos = fences[i]
        close_line_end = text.find("\n", fences[i + 1])
        end = len(text) if close_line_end == -1 else close_line_end + 1
        ranges.append((open_pos, end))

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


def _stage_2_1_range_at(pos: int, ranges: list[tuple[int, int]]) -> tuple[int, int] | None:
    for s, e in ranges:
        if s < pos < e:
            return (s, e)
        if s >= pos:
            break
    return None


def _stage_2_1_pick_boundary(text, lo, hi, heading_positions, protected) -> int:
    """Best cut index in [lo, hi): heading > paragraph > newline > CJK/EN
    sentence end. Skips boundaries that fall inside a protected range. Returns
    the exclusive cut index, or -1 if none found."""
    for hp in reversed(heading_positions):
        if lo <= hp < hi and _stage_2_1_range_at(hp, protected) is None:
            return hp  # cut before the heading so it leads the next chunk
    for sep, off in (("\n\n", 2), ("\n", 1), ("。", 1), (". ", 2)):
        idx = text.rfind(sep, lo, hi)
        while idx != -1 and _stage_2_1_range_at(idx, protected) is not None:
            idx = text.rfind(sep, lo, idx)
        if idx != -1:
            return idx + off
    return -1


def _stage_2_1_snap_out(start: int, end: int, protected) -> int:
    """If ``end`` lands inside a protected block, move it to a safe edge: before
    the block (block leads the next chunk) when possible, else after it.

    Guard against a large block (e.g. a multi-thousand-char OCR table) that
    starts early in the window: snapping back to its start would collapse the
    chunk to a tiny pre-table slice, wasting a generation round-trip on near-
    empty text. Only snap back when it leaves at least half the attempted
    window; otherwise snap forward past the block (let the chunk overflow to
    include the whole table)."""
    r = _stage_2_1_range_at(end, protected)
    if r is None:
        return end
    attempted = end - start
    if r[0] > start and (r[0] - start) >= attempted // 2:
        return r[0]
    return r[1]


def _stage_2_1_chunk_text(text: str, target_chars: int, overlap_chars: int,
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
    protected = _stage_2_1_find_protected_ranges(text)

    spans: list[tuple[int, int]] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + window, n)
        if end >= n:
            spans.append((start, n))
            break

        search_start = max(start, end - int(window * _SEARCH_FRAC))
        boundary = _stage_2_1_pick_boundary(text, search_start, end, heading_positions, protected)
        if boundary > start:
            end = boundary
        end = _stage_2_1_snap_out(start, end, protected)
        if end <= start:  # protected block fills the whole window — let it overflow
            r = _stage_2_1_range_at(start + 1, protected)
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

def _stage_2_2_build_template_section(template: str, file_path: Path, max_chars: int = 4000) -> str:
    """Build the template injection section for a Stage 2.2 prompt.

    Truncates the template to *max_chars* and wraps it in a
    ``# Document Type Instructions`` block.  Returns an empty string when
    *template* is falsy.
    """
    if not template:
        return ""
    template_trimmed = template[:max_chars]
    return f"""
# Document Type Instructions
The source is a **{file_path.parent.name}** document. Follow these type-specific conventions:
<template>
{template_trimmed}
</template>

"""


def _stage_2_2_schema_types_block(config: Config) -> str:
    """Inject authoritative NashSU-style schema and optional project purpose."""
    text = load_schema_md(config)
    schema_context = schema_prompt_text(text)
    purpose_context = load_purpose_md(config).strip()[:6000]
    if not schema_context and not purpose_context:
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
        "below in the `schema_typed_candidates` output field. Use a typed page only "
        "when THIS source genuinely supports it; NEVER invent goals, habits, journal "
        "entries, decisions, findings, or hypotheses.\n"
        f"Eligible source-grounded schema types: {route_lines}\n"
    ) if schema_context else ""
    purpose_block = (
        "\n# Wiki Purpose\n"
        "<purpose>\n"
        f"{purpose_context}\n"
        "</purpose>\n"
        "Use the purpose to prioritize relevant material; it never overrides source "
        "evidence or the schema's routing contract.\n"
    ) if purpose_context else ""
    return schema_block + purpose_block


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

    ``existing_slugs`` is the per-book SNAPSHOT of existing wiki slugs taken
    when the book first entered Stage 2.2 (persisted under
    "slugs_snapshot_2_2" in progress by _ingest_chunks). 2.2 is contractually
    wiki-independent; a live list_existing_slugs() read here made the prompt
    hash drift while a parallel batch book wrote wiki pages → conversation
    cache misses on every resume. None falls back to a live read (legacy
    callers/tests only — the pipeline always passes the snapshot).
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
    # template below). Detail is NOT lost by this: each chunk's full analysis
    # (concepts/claims/formulas) is persisted in chunk_analyses and flows to
    # 2.4 (per-chunk generation) and 2.6 (chunk_claims) separately — the digest
    # is only the lightweight continuity channel. Earlier fixed caps (6K, 24K)
    # and an interim dynamic cap (target_chars) predate this parity decision.
    if len(digest_str) > _DIGEST_PROMPT_CAP:
        digest_str = digest_str[:_DIGEST_PROMPT_CAP] + "\n... (truncated)"
    if existing_slugs is None:
        existing_slugs = list_existing_slugs(config)
    existing_slugs = _stage_2_2_cap_existing_slugs(list(existing_slugs), chunk_text)

    template_section = _stage_2_2_build_template_section(template, file_path, max_chars=2000)

    overlap_section = _stage_2_2_build_overlap_section(overlap_before)

    schema_types_section = _stage_2_2_schema_types_block(config)

    granularity_section = _stage_2_2_granularity_block(accumulated_digest)

    # ── Heading path (NashSU parity: chunk.headingPath) ──
    heading_section = ""
    if heading_path:
        heading_section = f"""
# Current location in the book
You are analyzing content from: **{heading_path}**

"""

    language_directive = build_language_directive(chunk_text)

    # Extraction-completeness guideline (2026-07-02): keep the behavioral
    # anti-under-extraction nudge but DROP the former per-char concept-COUNT target
    # (~1 concept/20K chars). Concept density is a property of content, not char
    # count, and a numeric target invited padding / concept-splitting. NashSU gives
    # no count target at all; this is the closest content-driven form — quality over
    # count. The chunk-size mention stays only to anchor "read all of it, section by
    # section", not to imply a quota.
    density_hint = (
        f"This chunk is ~{len(chunk_text):,} characters"
        + (f" spanning **{heading_path}**" if heading_path else "")
        + ". Enumerate it **section by section** so no part is under-extracted: list "
        "every genuine page-worthy concept the source defines or materially uses. "
        "Quality over count — do NOT pad with trivial mentions, do NOT split one "
        "concept into several, and do NOT skip a real concept to keep the list short."
    )

    return f"""{language_directive}

# Role
You are the LLM maintainer of a Karpathy-pattern personal knowledge base.
You are performing **Stage 2.2: Chunk Analysis** (chunk {chunk_index + 1}/{chunk_total}) of a book ingest pipeline.
{template_section}{schema_types_section}{granularity_section}
# Context: Accumulated Global Digest
This digest is cumulative context rolled up across all PREVIOUS chunks — use
it for continuity and to avoid re-writing the same *prose* twice.
Keep stable names consistent with the existing wiki and prior digest: when this
chunk re-encounters a concept/entity already named there, reuse that EXACT name
(stable names → stable slugs → downstream dedup works).
It is NOT a list of existing wiki pages: a concept named here has NOT necessarily
been turned into a page yet. Do NOT drop a page-worthy concept from
`concepts_found` just because its name appears in this digest — that includes
foundational / "preliminaries" concepts (the well-known building blocks a new
method is built from). Deduplication against REAL existing pages happens
downstream (Stage 2.3/2.4), not here. When in doubt, LIST the concept.

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

Analyze THIS CHUNK of the book. Extract:

1. Every concept this chunk defines, derives, or materially relies on — INCLUDING
   foundational / "preliminaries" concepts the source treats as background (e.g. the
   building-block techniques a new method is built from). Each distinct building
   block the source actually defines or uses deserves its own concept entry. Do NOT
   collapse several distinct concepts into one page, and do NOT skip a concept merely
   because it is "well known" or already named in the digest — downstream dedup
   (Stage 2.3/2.4) will link it to an existing page if one already exists.
   Granularity gate: a CONCEPT must be reusable beyond this single device/product.
   Chip-level or board-level implementation details (connector pinouts, board
   designators, one unit's internal signals) are NOT concepts — record them as
   entities or fold them into the system-level concept page.
2. All **entities** — specific *named* things identified by their name, not by a
   definition: people, organizations, products/systems, standards. (Tie-breaker:
   a named *theoretical or statistical model*, *method*, or *technique* — e.g. the
   Swerling model, chi-square fluctuation model, matched filter — is a CONCEPT, not
   an entity. Reserve "entity" for named people, organizations, products/systems,
   and standards.)
3. Key claims, formulas, data points
4. Connections to existing wiki pages (if any)
5. An **Updated Global Digest** — a COMPACT document-level digest that
   incorporates this chunk and preserves prior cross-chunk context. This is a
   continuity ledger, NOT an archive: every concept/entity NAME from the prior
   digest must survive (so later chunks know what is already covered), but keep
   each entry to ONE short line — condense prior definitions/claims freely.
   Your full per-chunk detail is already saved separately (concepts_found /
   claims / formulas above); do NOT duplicate it here. Target well under
   15,000 characters — anything beyond is hard-truncated before the next
   chunk sees it.
6. **Schema-typed page candidates** — use only the eligible type→directory
   mappings in the authoritative schema block above (e.g. finding, decision,
   methodology), and only when this chunk genuinely contains matching content.
   NEVER invent goals, habits, journal entries, decisions, findings, hypotheses,
   or other records that are not present in the source.

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
    definition: "..."      # the concept's definition as stated in the book
    key_details: ["...", "..."]   # 2-4 key facts / formulas / design rules

# ⚠️  CONCEPT NAMING RULES:
#   - name MUST be a SHORT, SPECIFIC topic (3-6 words), e.g. "DC-Link Voltage Control", "IGBT Thermal Modeling"
#   - NEVER use the book title or filename as a concept name
#   - NEVER include "Chunk N", "Chapter N" or page numbers in the name
#   - If the chunk covers multiple topics, list each topic as a SEPARATE concept
#   - Use the actual technical term from the book, not a generic description
#   - key_details: 2-4 key facts per concept. If a concept needs more than
#     that, it is NOT one concept: it is several disjoint topics bundled into
#     one umbrella page. Split it — e.g. "unit conversions", "Doppler shift",
#     "radar horizon" and "modulation types" are FOUR concepts, not four
#     key_details of one "fundamentals" page.

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
#   3. Minimum 3 claims per chunk (more for dense technical chapters).
#      Exception: front-matter chunks (preface/TOC/colophon before chapter 1)
#      need only 1 substantive claim.
#   4. Claims must be falsifiable/actionable assertions (quantitative results,
#      design rules, comparative verdicts, limits, mechanisms) — NOT scope
#      descriptions or bare definitions.
#   5. Before listing claims, quote 2-3 key sentences from the source text
#      that you read (verbatim, with their section/equation anchor) to prove
#      you grounded them in the actual text. Place these quotes in the
#      `source_quotes` field below.

source_quotes: |
  # 2-3 verbatim key sentences from THIS chunk's source text, with their
  # section/equation/figure anchor. This proves you read the text before
  # extracting claims. Example:
  # §2.3.4: "The Barker code of length 13 provides optimal peak sidelobe
  # level of -1/N for code length N."
  # 式(3.6): "Modulating waveform = exp(j*pi*tau*B*t^2)"

claims:
  - claim: "..."
    evidence: "§X.X or 式(N) or Figure N — specific source-text anchor (NOT generic chapter ref)"
    confidence: "high" | "medium" | "low"
    table_ref: "Table N or Figure N"   # for datasheets: REQUIRED; for books: omit if no table source
    page_ref: "p.NN"                   # for datasheets: REQUIRED; for books: omit if not applicable

formulas:
  - formula: '\\text{{Energy}} = \\frac{{1}}{{2}} C V^2'   # SINGLE-quoted; transcribe verbatim, never paraphrase
    meaning: '...'
    table_ref: "Table N"      # cite source table/figure when available

connections_to_existing_wiki:
  - existing_page: "..."
    relationship: "extends" | "contrasts" | "applies" | "cites"

# Schema-typed page candidates (NashSU parity). ONLY use eligible mappings
# listed in the authoritative schema block, and only when this chunk genuinely
# contains matching content. Leave empty (`[]`) when no eligible type fits.
# NEVER invent goals/habits/journal/decisions/findings/hypotheses.
schema_typed_candidates:
  - type: "finding" | "decision" | "methodology" | "..."   # a schema-declared type
    name: "..."        # short specific kebab-case-friendly name (3-6 words)
    folder: "findings"  # the wiki/<folder>/ the page should land in
    rationale: "..."    # one sentence: why this chunk supports this typed page

updated_global_digest: |
  # Compact Global Digest (after chunk {chunk_index + 1}/{chunk_total}) — NashSU parity
  # A compact continuity ledger, not an archive: every prior concept/entity
  # NAME survives, but each entry is ONE short line (condense prior prose;
  # full detail already lives in each chunk's own analysis). Keep the whole
  # digest well under 15,000 chars — overflow is hard-truncated.
  # When approaching the budget, compress OLDER entries' gists down to bare
  # names (names are non-negotiable, gists are droppable), keep book_meta +
  # outline intact, and keep key_claims to the book's MAIN arguments only.
  # MUST contain these 5 top-level keys. The FIRST chunk ESTABLISHES book_meta
  # and outline; later chunks refine them and append to the other three.
  book_meta:
    title: "..."
    authors: ["..."]
    year: "..."
    publisher: "..."
    granularity: "textbook" | "manual"   # "manual" ONLY for implementation/maintenance monographs
  outline:
    - "Chapter/Section ..."
  key_entities:
    - name: "..."
      type: "person" | "organization" | "system" | "model"
  key_concepts:
    - name: "..."
      definition: "..."   # ONE short line, not a paragraph; no key_details here
  key_claims:
    - claim: "..."        # ONE line; keep only the book's MAIN arguments here
      evidence: "..."

# Do NOT propose new wiki pages — that's Stage 2
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
        details = concept.get("key_details")
        if not isinstance(details, list) or not details:
            raise ChunkAnalysisValidationError(
                f"{prefix}.key_details must be a non-empty list")
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

    if normalized["claims"]:
        normalized["source_quotes"] = _analysis_nonempty_string(
            normalized.get("source_quotes"), "source_quotes")

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
) -> dict:
    """Analyze a single chunk.

    Used by the barrier-free pipeline in _do_prepare where each chunk is
    analyzed and immediately generated before moving to the next chunk.

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
