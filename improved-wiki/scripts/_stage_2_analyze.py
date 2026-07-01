from __future__ import annotations

from _stage_2_base import *
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


def _stage_2_2_resolve_chunk_heading_path(text: str, chunk_start: int, chunk_end: int) -> str:
    """Find the heading hierarchy that a chunk falls under (NashSU parity).

    Scans backwards from chunk_start to find the nearest H1-H6 heading, then
    walks further back to build the full ancestor path. Returns a string like
    "Chapter 3 > Section 3.2 > Subsection 3.2.1" or "" if no heading found.
    """
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


# ---------- Stage 1: Global Digest ----------

def _stage_2_1_build_prompt(
    extracted_text: str,
    file_path: Path,
    config: Config,
    template: str = "",
) -> str:
    """Build the prompt for Stage 1: Global Digest."""
    summary_text = extracted_text[:config.source_budget]
    existing_slugs = list_existing_slugs(config)

    # Inject type-specific template instructions (first 4000 chars — enough for schema guidance)
    template_section = ""
    if template:
        template_trimmed = template[:4000]
        template_section = f"""
# Document Type Instructions
The source is a **{file_path.parent.name}** document. Follow these type-specific conventions:
<template>
{template_trimmed}
</template>

"""

    language_directive = build_language_directive(summary_text)
    return f"""{language_directive}

# Role
You are the LLM maintainer of a Karpathy-pattern personal knowledge base.
You are performing **Stage 1: Global Digest** of a book ingest pipeline.
{template_section}
# Input
- Source file: {file_path.stem}
- Extracted text (first {config.source_budget:,} chars of full book):
<extracted_text>
{summary_text}
</extracted_text>

- Existing wiki pages: {', '.join(existing_slugs)}

# Task
Read the extracted text and produce a **high-level structural summary** of this book.
This will be used as context for per-chapter detailed analysis in the next stage.

# Output (YAML only, in ```yaml block)
```yaml
book_meta:
  title: "..."
  authors: [...]
  year: N
  pages: N
  publisher: "..."
  language: "zh" | "en" | "mixed"

outline:
  # Complete chapter tree with approximate page/char ranges
  - chapter: 1
    title: "..."
    key_topics: ["...", "..."]
    # Key: give a unique start marker (first 30 chars of chapter text)
    # so the chunker can align chunks to chapter boundaries
    start_marker: "..."

key_entities:
  - name: "..."

key_concepts:
  - name: "..."
    importance: "core" | "supporting" | "mentioned"

key_claims:
  - claim: "..."
    chapter: N
```

# Constraints
- Focus on STRUCTURE, not details — per-chapter details come in Stage 2.2
- The outline must be as complete as possible
- chapter_map.start_marker is critical for accurate chunking in Stage 2.2
- Do NOT propose new wiki pages yet — that's Stage 2 (Synthesis)
"""


def stage_2_1_global_digest(
    extracted_text: str,
    file_path: Path,
    config: Config,
    template: str = "",
    verbose: bool = False,
) -> dict:
    """Stage 1: One LLM call for book-level structural summary."""
    print(f"[stage 2.1] Global Digest — sending {min(len(extracted_text), config.source_budget):,} chars to LLM...")
    prompt = _stage_2_1_build_prompt(extracted_text, file_path, config, template)
    response, stop_reason = call_anthropic_protocol(
        prompt, config, max_tokens=config.compute_max_tokens(8192), label="global digest")
    if verbose:
        print(f"[stage 2.1] Raw response ({len(response)} chars, stop={stop_reason}):\n{response[:3000]}...\n")
    digest = parse_yaml_block(response)
    print(f"[stage 2.1] Done — {len(digest)} top-level keys in digest")
    return digest


# ---------- Stage 2.2: Chunk Analysis ----------

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
    """NashSU parity — tell Stage 2.2 which schema-defined page types
    (beyond entity/concept) this project supports, so the analysis can flag
    schema-typed candidates for the generation stage to route.

    Empty for default projects (schema.md absent or no extra folders) so the
    heavily-tuned book-ingest prompt sees zero noise.
    """
    text = load_schema_md(config)
    if not text.strip():
        return ""
    extra = schema_folders(text) - BASE_PAGE_DIRS - SCHEMA_NON_PAGE_DIRS
    if not extra:
        return ""
    return (
        "\n# Schema-Defined Page Types (NashSU parity)\n"
        "This project's schema.md defines extra typed page types beyond entity/concept. "
        "When THIS chunk genuinely contains content fitting one of these types, record it "
        "under `schema_typed_candidates` below so the generation stage can route a page "
        "into the matching folder. Use a type ONLY when the source actually supports it; "
        "NEVER invent goals, habits, journal entries, decisions, or other user-authored "
        "records that are not present in the source.\n"
        f"Available schema types: {', '.join(sorted(extra))}\n"
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
    # cap to keep prompts lean
    if len(digest_str) > 6000:
        digest_str = digest_str[:6000] + "\n... (truncated)"
    existing_slugs = list_existing_slugs(config)

    template_section = _stage_2_2_build_template_section(template, file_path, max_chars=2000)

    overlap_section = _stage_2_2_build_overlap_section(overlap_before)

    schema_types_section = _stage_2_2_schema_types_block(config)

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
{template_section}{schema_types_section}
# Context: Accumulated Global Digest
This digest is cumulative context from the Stage 2.1 outline and all PREVIOUS
chunks — use it for continuity and to avoid re-writing the same *prose* twice.
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
2. All **entities** — specific *named* things identified by their name, not by a
   definition: people, organizations, products/systems, standards. (Tie-breaker:
   a named *theoretical or statistical model*, *method*, or *technique* — e.g. the
   Swerling model, chi-square fluctuation model, matched filter — is a CONCEPT, not
   an entity. Reserve "entity" for named people, organizations, products/systems,
   and standards.)
3. Key claims, formulas, data points
4. Connections to existing wiki pages (if any)
5. An **Updated Global Digest** — merge this chunk's key discoveries into the
   Accumulated Global Digest above, so the next chunk benefits from everything
   learned so far. Keep it concise but cumulative: add new concepts, entities,
   and key claims. Do NOT remove anything from the existing digest.
6. **Schema-typed page candidates** — if the project schema defines page types
   beyond entity/concept (e.g. finding, decision, methodology) AND this chunk
   genuinely contains matching content, note it for the generation stage. Use a
   schema-defined type ONLY when the source actually supports it; NEVER invent
   goals, habits, journal entries, decisions, or other user-authored records
   that are not present in the source.

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

# ⚠️  CONCEPT NAMING RULES (CRITICAL):
#   - name MUST be a SHORT, SPECIFIC topic (3-6 words), e.g. "DC-Link Voltage Control", "IGBT Thermal Modeling"
#   - NEVER use the book title or filename as a concept name
#   - NEVER include "Chunk N", "Chapter N" or page numbers in the name
#   - If the chunk covers multiple topics, list each topic as a SEPARATE concept
#   - Use the actual technical term from the book, not a generic description

claims:
  - claim: "..."
    evidence: "..."
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

# Schema-typed page candidates (NashSU parity). ONLY when the project
# schema defines extra types AND this chunk genuinely contains matching content.
# `type` MUST be one of the schema types listed above. Leave empty (`[]`) when
# the schema adds no types or this chunk has no matching content. NEVER invent
# goals/habits/journal/decisions not present in the source.
schema_typed_candidates:
  - type: "finding" | "decision" | "methodology" | "..."   # a schema-declared type
    name: "..."        # short specific kebab-case-friendly name (3-6 words)
    folder: "findings"  # the wiki/<folder>/ the page should land in
    rationale: "..."    # one sentence: why this chunk supports this typed page

updated_global_digest: |
  # Accumulated Global Digest (after chunk {chunk_index + 1}/{chunk_total})
  # Merge this chunk's key concepts, entities, and claims into the prior digest.
  # Be cumulative — keep everything from before, add only what's new.
  ...

# Do NOT propose new wiki pages — that's Stage 2
```
"""


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
) -> dict:
    """Analyze a single chunk.

    Used by the barrier-free pipeline in _do_prepare where each chunk is
    analyzed and immediately generated before moving to the next chunk.

    Returns analysis dict with keys: concepts_found, entities_found, claims,
    formulas, connections_to_existing_wiki, digest_updates, plus _chunk_index,
    _chunk_size, _attempts.
    On failure: returns dict with chunk_index + error key.
    """
    prompt = _stage_2_2_build_prompt(
        chunk, chunk_idx, chunk_total, global_digest, file_path, config,
        template=template, accumulated_digest=accumulated_digest,
        overlap_before=overlap_before, heading_path=heading_path,
    )

    for attempt in range(1 + max_retries):
        try:
            t0 = time.time()
            if attempt == 0:
                print(f"  [chunk {chunk_idx+1}/{chunk_total}] analyzing ({len(chunk):,} chars)...",
                      flush=True)
            response, stop_reason = call_anthropic_protocol(
                prompt, config, max_tokens=config.compute_max_tokens(8192))
            analysis = parse_yaml_block(response)
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
            return analysis

        except Exception as e:
            if attempt < max_retries and _is_retryable_exception(e):
                _record_rate_limit()
                wait = _retry_jitter(2.0, attempt)
                err_label = type(e).__name__
                print(f"  [chunk {chunk_idx+1}/{chunk_total}] analyze retry {attempt+1}/{1+max_retries}"
                      f" ({err_label}: {str(e)[:80]}) — {wait:.1f}s...")
                time.sleep(wait)
                continue
            print(f"  [chunk {chunk_idx+1}/{chunk_total}] analyze FAILED: {e}")
            return {
                "chunk_index": chunk_idx + 1, "error": str(e),
                "chunk_text_length": len(chunk), "_attempts": 1 + max_retries,
            }



