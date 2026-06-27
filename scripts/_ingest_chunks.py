"""_ingest_chunks.py — chunk analysis pipeline 2.2→2.4 (extracted from ingest.py)."""
from __future__ import annotations

import json
import time
from pathlib import Path

from _core import (
    Config,
    stage_begin as _stage_begin,
    file_sha256,
    is_stage_done,
    unmark_stage_done,
)
from _stage_2_analyze import (
    _stage_2_1_chunk_text,
    _stage_2_2_analyze_chunk,
    _stage_2_2_chunk_retries,
    _stage_2_2_resolve_chunk_heading_path,
)
from _stage_2_4_generation import (
    stage_2_4_generate_chunk,
    stage_2_4_generate_all,
    _stage_2_4_extract_names,
    _stage_2_4_per_concept_fallback,
)
from _stage_validators import _verify_stage_2_2_chunks

def _analyze_all_chunks(
    chunk_meta: list, global_digest: dict, accumulated_digest: str,
    raw_file: Path, config: Config, template_content: str,
    chunk_total: int, t_start: float, verbose: bool,
) -> list:
    """Stage 2.2: analyze all chunks, serially.

    Serial preserves cross-chunk ``accumulated_digest`` refinement \u2014 each
    chunk's analysis is informed by the previous chunk's updated digest.
    Conversation mode is the only text-gen path, so there is no parallel
    branch: every call is a manual round-trip, which is inherently serial.
    Returns chunk_analyses indexed by chunk order.
    """
    chunk_analyses: list = []

    for i, chunk, overlap_before, heading_path in chunk_meta:
        ca = _stage_2_2_analyze_chunk(
            chunk, i, chunk_total, global_digest, accumulated_digest,
            overlap_before, heading_path, raw_file, config, template_content,
            max_retries=_stage_2_2_chunk_retries(), verbose=verbose)
        chunk_analyses.append(ca)
        updated = ca.get("updated_global_digest", "")
        if isinstance(updated, str) and len(updated.strip()) > 50:
            accumulated_digest = updated.strip()
        elif isinstance(updated, dict):
            accumulated_digest = json.dumps(updated, ensure_ascii=False, indent=2)
        done = i + 1
        pct = done * 100 // chunk_total
        eta = ((time.time() - t_start) / done) * (chunk_total - done) if done > 0 else 0
        print(f"  [analyze] {done}/{chunk_total} [{pct}% ETA {eta:.0f}s]")
    return chunk_analyses

def _generate_all_chunks(
    chunk_meta: list, chunk_analyses: list, existing_refs: dict,
    raw_file: Path, config: Config, template_content: str,
    chunk_total: int, t_start: float, verbose: bool,
    related_pages: list[dict] | None = None,
) -> tuple[list, list]:
    """Stage 2.4: single-shot generation across all chunks (NashSU parity, 2026-06-27).

    One LLM call emits FILE blocks for every chunk's concepts/entities at once,
    replacing the former per-chunk loop (N calls → 1). ``existing_refs`` and
    ``related_pages`` (Stage 2.3 outputs) are folded into the single prompt so
    the LLM wikilinks to existing pages instead of regenerating them. The
    per-concept fallback (caller-side) catches any concepts missed, including
    output-truncation gaps.
    """
    all_file_blocks, generated_slugs, _stop_reason = stage_2_4_generate_all(
        chunk_analyses, raw_file, config, template_content,
        verbose=verbose, existing_refs=existing_refs,
        related_pages=related_pages,
    )
    done = chunk_total
    dt = time.time() - t_start
    print(f"  [generate] {done}/{chunk_total} [single-shot, {dt:.0f}s]")
    return all_file_blocks, generated_slugs

def _run_chunk_pipeline(
    extracted_text: str, global_digest: dict, raw_file: Path, config: Config,
    template_content: str, progress: dict | None, verbose: bool,
) -> tuple[list, dict, list, dict]:
    """Stage 2.2 \u2192 2.3 \u2192 2.4: analyze all chunks, detect existing-wiki
    associations, then generate pages with associations fed into each prompt.

    Split (2026-06-21): analysis and generation are separate phases so Stage 2.3
    (incremental association detection) can run between them and feed back into
    the generation prompt. Returns
    ``(chunk_analyses, analysis, file_blocks, incremental_associations)``.
    """
    # Cached: chunk analysis already complete. Stage-completion is the single
    # source of truth in stages.json (stage_2_3_done); chunk_analyses presence
    # in the artifact store guards against a missing artifact.
    _h = file_sha256(raw_file)
    if (progress and "chunk_analyses" in progress
            and is_stage_done(config, _h, "stage_2_3_done")):
        # Restore file_blocks DIRECTLY from the artifact store. The retired
        # design re-parsed ``raw_response`` (= "\n".join(block BODIES), bodies
        # without the ---FILE:...--- wrappers), so parse_file_blocks() returned
        # [] and silently dropped every concept/entity page on resume.
        #
        # The ``file_blocks`` key being PRESENT (even as []) is an authoritative
        # restore: [] is the legitimate "every concept already overlaps an
        # existing wiki page" outcome. The key being ABSENT means an old/partial
        # cache that predates file_blocks persistence \u2014 there is no safe way to
        # recover it, so rather than proceed with 0 blocks (re-introducing the
        # silent loss) we invalidate the stage marker and fall through to
        # re-run the chunk pipeline.
        persisted_blocks = progress.get("file_blocks")
        if persisted_blocks is None:
            print("  [stage 2.2] \u26a0\ufe0f  stage_2_3_done set but no persisted "
                  "file_blocks artifact \u2014 invalidating marker and re-running "
                  "chunk pipeline (prevents silent concept/entity loss).")
            unmark_stage_done(config, _h, "stage_2_3_done")
        else:
            chunk_analyses = progress["chunk_analyses"]
            print(f"  [stage 2.2] (cached) Chunk Analysis \u2014 {len(chunk_analyses)} chunks")
            _verify_stage_2_2_chunks(chunk_analyses, extracted_text)
            analysis = progress.get("analysis", {})
            incremental_associations = progress.get("incremental_associations", {})
            return chunk_analyses, analysis, persisted_blocks, incremental_associations

    chunks = _stage_2_1_chunk_text(extracted_text, config.target_chars, config.chunk_overlap,
                                   target_tokens=config.target_tokens)
    chunk_total = len(chunks)

    est_sec = chunk_total * 75
    print(f"  [stage 2.2] Analyze \u2014 {chunk_total} chunk(s), "
          f"target {config.target_chars:,} chars/chunk (est. {est_sec/60:.0f} min)")
    _stage_begin("Stage 2.2: Chunk Analysis")
    t_start = time.time()
    accumulated_digest = json.dumps(
        {k: global_digest[k] for k in
         ("book_meta", "outline", "key_entities", "key_concepts")
         if k in global_digest},
        ensure_ascii=False, indent=2)

    chunk_meta: list[tuple[int, str, str, str]] = []
    for i in range(chunk_total):
        chunk = chunks[i]
        overlap_before = chunks[i - 1][-config.chunk_overlap:] if i > 0 else ""
        chunk_pos = extracted_text.find(chunk)
        if chunk_pos == -1:
            chunk_pos = i * config.target_chars
        heading_path = _stage_2_2_resolve_chunk_heading_path(
            extracted_text, chunk_pos, chunk_pos + len(chunk))
        chunk_meta.append((i, chunk, overlap_before, heading_path))

    # \u2500\u2500 Stage 2.2: analyze all chunks \u2500\u2500
    chunk_analyses = _analyze_all_chunks(
        chunk_meta, global_digest, accumulated_digest, raw_file, config,
        template_content, chunk_total, t_start, verbose)

    # \u2500\u2500 Stage 2.3: incremental association detection (existing-wiki overlap) \u2500\u2500
    from _stage_2_3_incremental import (
        stage_2_3_detect_incremental_associations,
        stage_2_3_resolve_proposed_connections,
    )
    incremental_associations = stage_2_3_detect_incremental_associations(
        config.wiki_dir, chunk_analyses)
    if incremental_associations:
        print(f"  [stage 2.3] {len(incremental_associations)} new concept(s) "
              f"match existing wiki pages \u2192 fed into generation prompt")
    else:
        print(f"  [stage 2.3] No existing-wiki associations (first source or no overlap)")

    related_pages = stage_2_3_resolve_proposed_connections(config.wiki_dir, chunk_analyses)
    if related_pages:
        print(f"  [stage 2.3] {len(related_pages)} proposed connection(s) to "
              f"existing wiki resolved \u2192 fed into generation prompt")

    # \u2500\u2500 Stage 2.4: generate all chunks (associations fed into prompt) \u2500\u2500
    _stage_begin("Stage 2.4: Chunk Generation")
    all_file_blocks, generated_slugs = _generate_all_chunks(
        chunk_meta, chunk_analyses, incremental_associations, raw_file, config,
        template_content, chunk_total, t_start, verbose,
        related_pages=related_pages)

    # Build combined analysis
    unique_concepts, _ = _stage_2_4_extract_names(chunk_analyses)
    concept_blocks = [b for b in all_file_blocks if "concepts/" in b[0]]
    entity_blocks = [b for b in all_file_blocks if "entities/" in b[0]]
    analysis = {
        "book_meta": global_digest.get("book_meta", {}),
        "outline": global_digest.get("outline", []),
        "concepts_identified": len(unique_concepts),
        "concepts_generated": len(concept_blocks),
        "entities_generated": len(entity_blocks),
        "coverage_pct": round(len(concept_blocks) / max(len(unique_concepts), 1), 2),
        "total_chunks": chunk_total,
        "method": "analyze\u2192associate\u2192generate",
    }
    file_blocks = all_file_blocks

    # \u2500\u2500 Fallback: per-concept generation \u2500\u2500
    # 0 blocks generated is the CORRECT outcome (not a failure) when every
    # concept in this source already overlaps an existing wiki page \u2014
    # Stage 2.4 is instructed to skip and wikilink those (see existing_refs
    # in _stage_2_4_build_prompt). Only fall back for concepts genuinely
    # missing from the wiki; otherwise a conversation-mode replay that
    # resumes past Stage 3.1 (write) re-derives chunk_analyses, sees its own
    # already-written pages as "existing", correctly emits 0 blocks, and
    # then this fallback would burn 20+ wasted LLM calls re-generating pages
    # that are already on disk (confirmed live on the Plett BMS Vol.2 ingest).
    truly_missing = [n for n in unique_concepts if n not in incremental_associations]
    if not concept_blocks and truly_missing and chunk_analyses:
        n_missed = len(truly_missing)
        print(f"  [stage 2.4] \u26a0\ufe0f  0/{n_missed} concepts generated "
              f"\u2014 falling back to per-concept generation "
              f"(pre_existing_slugs={len(generated_slugs)})")
        _fa_analysis, _fa_raw, fa_blocks = _stage_2_4_per_concept_fallback(
            chunk_analyses, global_digest, raw_file, config,
            template_content, verbose=verbose,
            pre_existing_slugs=generated_slugs,
        )
        fa_concept_entity = [(p, c) for p, c in fa_blocks
                             if not p.startswith("sources/")]
        all_file_blocks = fa_concept_entity
        file_blocks = all_file_blocks
        concept_blocks = [b for b in all_file_blocks if "concepts/" in b[0]]
        entity_blocks = [b for b in all_file_blocks if "entities/" in b[0]]
        analysis["concepts_generated"] = len(concept_blocks)
        analysis["entities_generated"] = len(entity_blocks)
        analysis["coverage_pct"] = round(
            len(concept_blocks) / max(len(unique_concepts), 1), 2)
        analysis["method"] = "analyze\u2192associate\u2192generate+fallback"
        for path, _ in fa_concept_entity:
            s = Path(path).stem.lower().replace(" ", "-").replace("/", "-")
            if s not in generated_slugs:
                generated_slugs.append(s)

    _verify_stage_2_2_chunks(chunk_analyses, extracted_text)
    return chunk_analyses, analysis, file_blocks, incremental_associations
