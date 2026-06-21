
from _stage_2_base import *

def _stage_2_1_chunk_text(text: str, target_chars: int, overlap_chars: int) -> list[str]:
    """Split text into overlapping chunks.

    NashSU parity (ingest.ts L2107-2205): prefers markdown heading boundaries
    (H1-H6), then paragraph breaks, then sentence ends near target_chars.
    """
    if len(text) <= target_chars:
        return [text]

    print(f"[chunk] Splitting {len(text)} chars into ~{target_chars}-char chunks...", flush=True)

    # Pre-scan: find all heading boundaries for heading-aware splitting
    _HEADING_RE = re.compile(r'^#{1,6}\s+.+$', re.MULTILINE)
    heading_positions = [m.start() for m in _HEADING_RE.finditer(text)]

    chunks = []
    start = 0
    while start < len(text):
        end = min(start + target_chars, len(text))
        if end >= len(text):
            chunks.append(text[start:].strip())
            break

        search_start = max(start, end - int(target_chars * 0.15))

        # Priority 1: markdown heading boundary (NashSU heading-aware)
        boundary = -1
        for hp in reversed(heading_positions):
            if search_start <= hp < end:
                boundary = hp
                break

        # Priority 2: paragraph break
        if boundary == -1:
            boundary = text.rfind("\n\n", search_start, end)

        # Priority 3: single newline
        if boundary == -1:
            boundary = text.rfind("\n", search_start, end)

        # Priority 4: CJK sentence end
        if boundary == -1:
            boundary = text.rfind("。", search_start, end)

        # Priority 5: English sentence end
        if boundary == -1:
            boundary = text.rfind(". ", search_start, end)

        if boundary > start:
            end = boundary + 1

        chunks.append(text[start:end].strip())
        new_start = end - overlap_chars
        if new_start <= start:
            break
        start = new_start

    print(f"[chunk] Done — {len(chunks)} chunks", flush=True)
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

    return f"""# Role
You are the LLM maintainer of a Karpathy-pattern personal knowledge base.
You are performing **Stage 1: Global Digest** of a book ingest pipeline.
{template_section}
# Input
- Source file: {file_path.stem}
- Extracted text (first {config.source_budget:,} chars of full book):
<extracted_text>
{summary_text}
</extracted_text>

- Existing wiki pages: {', '.join(existing_slugs[:300])}

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
    role: "person" | "organization" | "system" | "model" | "standard"

key_concepts:
  - name: "..."
    importance: "core" | "supporting" | "mentioned"

key_claims:
  - claim: "..."
    chapter: N

chunk_plan:
  # How many chunks needed? Where's the natural split?
  estimated_total_chunks: N
  # For each chunk: which chapters does it cover?
  - chunk: 1
    chapters: [1, 2]
    estimated_chars: N
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
    response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=8192, label="global digest")
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

    # ── Heading path (NashSU parity: chunk.headingPath) ──
    heading_section = ""
    if heading_path:
        heading_section = f"""
# Current location in the book
You are analyzing content from: **{heading_path}**

"""

    return f"""# Role
You are the LLM maintainer of a Karpathy-pattern personal knowledge base.
You are performing **Stage 2.2: Chunk Analysis** (chunk {chunk_index + 1}/{chunk_total}) of a book ingest pipeline.
{template_section}
# Context: Accumulated Global Digest
This digest includes discoveries from all PREVIOUS chunks. Use it to avoid
re-extracting the same concepts and to build on what earlier chunks found.
If a concept was already defined in a prior chunk, note it as a
cross-reference rather than re-defining it.

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

- Existing wiki pages: {', '.join(existing_slugs[:200])}

# Task
Analyze THIS CHUNK of the book. Extract:

1. All concepts defined or heavily used in this chunk (skip if already in the
   Accumulated Global Digest — just cross-reference instead)
2. All entities (people, organizations, systems, models, standards) mentioned
3. Key claims, formulas, data points
4. Connections to existing wiki pages (if any)
5. An **Updated Global Digest** — merge this chunk's key discoveries into the
   Accumulated Global Digest above, so the next chunk benefits from everything
   learned so far. Keep it concise but cumulative: add new concepts, entities,
   and key claims. Do NOT remove anything from the existing digest.

# Output (YAML only, in ```yaml block)
```yaml
chunk_index: {chunk_index + 1}
chunk_total: {chunk_total}

entities_found:
  - name: "..."
    role: "person" | "organization" | "system" | "model" | "standard"
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

formulas:
  - formula: "LaTeX"
    meaning: "..."

connections_to_existing_wiki:
  - existing_page: "..."
    relationship: "extends" | "contrasts" | "applies" | "cites"

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




def _stage_2_2_locate_chunk_text(
    extracted_text: str,
    chunks: list[str],
    i: int,
    config: Config,
) -> tuple[int, str]:
    """Locate chunk *i* in the full extracted text and resolve its heading path.

    Returns ``(chunk_pos, heading_path)``.  For chunk 0 the position is
    always 0; for subsequent chunks ``str.find`` is used with a char-offset
    fallback when the exact substring is not found.
    """
    chunk = chunks[i]
    chunk_len = len(chunk)
    if i == 0:
        chunk_pos = 0
    else:
        chunk_pos = extracted_text.find(chunk)
        if chunk_pos == -1:
            chunk_pos = i * config.target_chars  # fallback estimate
    heading_path = _stage_2_2_resolve_chunk_heading_path(
        extracted_text, chunk_pos, chunk_pos + chunk_len,
    )
    return chunk_pos, heading_path


def _stage_2_2_analyze_one_chunk(
    chunk_text: str,
    chunk_idx: int,
    chunk_total: int,
    config: Config,
    accumulated_digest_str: str,
    t0: float,
    global_digest: dict,
    file_path: Path,
    template: str,
    overlap_before: str,
    heading_path: str,
    max_retries: int,
) -> tuple[dict | None, str | None, Exception | None, str]:
    """Analyze a single chunk with retries.

    Returns a 4-tuple ``(analysis, updated_digest, error, last_error_str)``.
    On success *analysis* is a dict and *error*/*last_error_str* are ``None``.
    On failure *analysis* is ``None`` and *error* carries the exception.

    *accumulated_digest_str* is **not** mutated — the caller decides whether
    to adopt the returned *updated_digest*.
    """
    chunk_len = len(chunk_text)
    last_error = None

    for attempt in range(1 + max_retries):
        prompt = _stage_2_2_build_prompt(
            chunk_text, chunk_idx, chunk_total, global_digest, file_path, config,
            template=template, accumulated_digest=accumulated_digest_str,
            overlap_before=overlap_before, heading_path=heading_path,
        )

        try:
            t_chunk = time.time()
            response, stop_reason = call_anthropic_protocol(
                prompt, config, max_tokens=8192, label=f"chunk {chunk_idx+1} analysis",
            )
            analysis = parse_yaml_block(response)
            analysis["_chunk_index"] = chunk_idx + 1
            analysis["_chunk_size"] = chunk_len
            analysis["_attempts"] = attempt + 1
            dt = time.time() - t_chunk
            n_c = len(analysis.get("concepts_found") or [])
            n_e = len(analysis.get("entities_found") or [])
            elapsed = time.time() - t0
            done_count = chunk_idx + 1
            eta = (elapsed / done_count) * (chunk_total - done_count) if done_count > 0 else 0
            pct = done_count * 100 // chunk_total
            tag = f" (retry #{attempt})" if attempt > 0 else ""
            print(f"  [stage 2.2] chunk {chunk_idx+1}/{chunk_total} OK{tag} — "
                  f"{n_c} concepts, {n_e} entities, {dt:.0f}s "
                  f"[{pct}% ETA {eta:.0f}s]")

            # Extract updated global digest from this chunk's analysis
            updated_digest = analysis.get("updated_global_digest", "")
            if isinstance(updated_digest, str) and len(updated_digest.strip()) > 50:
                new_digest = updated_digest.strip()
            elif isinstance(updated_digest, dict):
                new_digest = json.dumps(updated_digest, ensure_ascii=False, indent=2)
            else:
                new_digest = accumulated_digest_str  # keep previous

            return analysis, new_digest, None, None

        except Exception as e:
            err_str = str(e)[:200]
            last_error = e
            if _is_retryable_exception(e):
                _record_rate_limit()
            if attempt < max_retries and _is_retryable_exception(e):
                wait = _retry_jitter(2.0, attempt)
                err_label = type(e).__name__
                print(f"  [stage 2.2] chunk {chunk_idx+1}/{chunk_total} attempt {attempt+1} failed "
                      f"({err_label}: {err_str[:80]}) — retrying in {wait:.1f}s...")
                time.sleep(wait)
                continue
            print(f"  [stage 2.2] chunk {chunk_idx+1}/{chunk_total} FAILED after "
                  f"{1 + max_retries} attempts: {err_str[:120]}")
            return None, accumulated_digest_str, e, err_str

    # Should be unreachable (loop always returns), but satisfy the type checker
    return None, accumulated_digest_str, last_error, str(last_error)[:200] if last_error else ""


def _stage_2_2_update_digest_and_checkpoint(
    config: Config,
    source_hash: str,
    chunk_total: int,
    accumulated_digest: str,
    analyses: list[dict],
    analysis: dict,
) -> list[dict]:
    """Append *analysis* to *analyses* and save a per-chunk checkpoint.

    Returns a **new** list (immutable convention) with the appended analysis.
    """
    updated_analyses = [*analyses, analysis]
    if source_hash:
        _stage_2_2_checkpoint(config, source_hash, chunk_total, accumulated_digest, updated_analyses)
    return updated_analyses


def stage_2_2_chunk_analysis(
    extracted_text: str,
    global_digest: dict,
    file_path: Path,
    config: Config,
    template: str = "",
    verbose: bool = False,
    source_hash: str = "",
) -> list[dict]:
    """Stage 2.2: Split text into chunks and analyze each one SEQUENTIALLY.

    NashSU parity: each chunk builds on the accumulated discoveries of all
    previous chunks via an "Updated Global Digest" that grows with each step.
    Later chunks get richer context — concepts found in chunk 3 are available
    to chunk 8, preventing duplicate extraction and improving cross-chapter
    awareness.

    Per-chunk checkpoint (NashSU parity): after each successful chunk, saves
    accumulated digest + partial analyses to the progress file.  On resume,
    completed chunks are skipped and processing resumes from the last checkpoint.

    Still supports per-chunk retries (LLM_CHUNK_RETRIES, default 2 → 3 total).
    """
    chunks = _stage_2_1_chunk_text(extracted_text, config.target_chars, config.chunk_overlap)
    chunk_total = len(chunks)
    max_retries = _stage_2_2_chunk_retries()
    print(f"[stage 2.2] Chunk Analysis — {chunk_total} chunks "
          f"(target {config.target_chars:,} chars/chunk, overlap {config.chunk_overlap:,}, "
          f"sequential NashSU mode, retries={max_retries})")

    t0 = time.time()
    analyses: list[dict] = []
    accumulated_digest = ""
    start_chunk = 0

    # ── Resume from per-chunk checkpoint (NashSU parity: LongSourceCheckpoint) ──
    if source_hash:
        progress = load_progress(config, source_hash)
        cp = (progress or {}).get("stage_2_2_cp") if progress else None
        if cp and cp.get("chunk_total") == chunk_total:
            analyses = cp.get("analyses", [])
            accumulated_digest = cp.get("accumulated_digest", "")
            start_chunk = len(analyses)
            if start_chunk > 0:
                print(f"[stage 2.2] Resuming from chunk {start_chunk + 1}/{chunk_total} "
                      f"({start_chunk} completed, digest={len(accumulated_digest)} chars)")

    # Build initial digest string from Stage 2.1 global digest (first chunk only)
    if not accumulated_digest:
        digest_compact = {}
        for key in ("book_meta", "outline", "key_entities", "key_concepts"):
            if key in global_digest:
                digest_compact[key] = global_digest[key]
        accumulated_digest = json.dumps(digest_compact, ensure_ascii=False, indent=2)

    for i in range(start_chunk, chunk_total):
        chunk = chunks[i]
        chunk_len = len(chunk)
        overlap_before = chunks[i - 1] if i > 0 else ""
        _chunk_pos, heading_path = _stage_2_2_locate_chunk_text(
            extracted_text, chunks, i, config,
        )

        analysis, new_digest, error, err_str = _stage_2_2_analyze_one_chunk(
            chunk_text=chunk,
            chunk_idx=i,
            chunk_total=chunk_total,
            config=config,
            accumulated_digest_str=accumulated_digest,
            t0=t0,
            global_digest=global_digest,
            file_path=file_path,
            template=template,
            overlap_before=overlap_before,
            heading_path=heading_path,
            max_retries=max_retries,
        )

        if error is not None:
            # All retries exhausted — record failure and checkpoint
            analyses.append({
                "chunk_index": i + 1, "error": str(error),
                "chunk_text_length": chunk_len, "_attempts": 1 + max_retries,
            })
            if source_hash:
                _stage_2_2_checkpoint(config, source_hash, chunk_total, accumulated_digest, analyses)
        else:
            accumulated_digest = new_digest
            analyses = _stage_2_2_update_digest_and_checkpoint(
                config, source_hash, chunk_total, accumulated_digest, analyses, analysis,
            )

    total_concepts = sum(len(a.get("concepts_found") or []) for a in analyses)
    total_entities = sum(len(a.get("entities_found") or []) for a in analyses)
    errored = sum(1 for a in analyses if "error" in a)
    elapsed = time.time() - t0
    speed = chunk_total / elapsed if elapsed > 0 else 0
    print(f"[stage 2.2] Done — {chunk_total} chunks in {elapsed:.0f}s ({speed:.1f} chunks/s), "
          f"{errored} failed, {total_concepts} concepts, {total_entities} entities total")
    if errored > 0:
        failed_indices = [a.get("chunk_index", -1) for a in analyses if "error" in a]
        print(f"[stage 2.2] ⚠️  Failed chunks: {failed_indices} — Stage 2 synthesis may be incomplete")
    return analyses


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
    """Analyze a single chunk (extracted from stage_2_2_chunk_analysis).

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
            response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=8192)
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


def _stage_2_2_checkpoint(config: Config, source_hash: str, chunk_total: int,
                    accumulated_digest: str, analyses: list[dict]) -> None:
    """Save per-chunk checkpoint for Stage 2.2 resume (NashSU parity)."""
    # Merge into existing progress to preserve other stage data
    progress = load_progress(config, source_hash) or {}
    progress["stage"] = "stage_2_2_partial"
    progress["stage_2_2_cp"] = {
        "chunk_total": chunk_total,
        "accumulated_digest": accumulated_digest,
        "analyses": analyses,
    }
    save_progress(config, source_hash, progress)



