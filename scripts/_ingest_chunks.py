"""_ingest_chunks.py — chunk analysis pipeline 2.2→2.4 (extracted from ingest.py)."""
from __future__ import annotations

import json
import time
from pathlib import Path

from _core import Config, parse_file_blocks, stage_begin as _stage_begin
from _stage_2_analyze import (
    _stage_2_1_chunk_text,
    _stage_2_2_analyze_chunk,
    _stage_2_2_chunk_retries,
    _stage_2_2_resolve_chunk_heading_path,
)
from _stage_2_4_generation import (
    stage_2_4_generate_chunk,
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
) -> tuple[list, list, list]:
    """Stage 2.4: sequential generation across all chunks.

    ``existing_refs`` (Stage 2.3 output: {concept_name: [wiki_slugs]}) is fed
    into each chunk's generation prompt so the LLM wikilinks to existing pages
    instead of regenerating them. ``related_pages`` (Stage 2.3's
    stage_2_3_resolve_proposed_connections output) is fed in so the LLM
    wikilinks to genuinely *related* (not duplicate) existing pages.
    ``generated_slugs`` accumulates across chunks (sequential, both paths).
    """
    all_file_blocks: list = []
    all_responses: list[str] = []
    generated_slugs: list[str] = []

    for i, chunk, _overlap_before, _heading_path in chunk_meta:
        ca = chunk_analyses[i]
        if "error" in ca:
            continue
        blocks = stage_2_4_generate_chunk(
            ca, i, generated_slugs, raw_file, config, template_content,
            verbose=verbose, chunk_text=chunk, existing_refs=existing_refs,
            related_pages=related_pages)
        all_file_blocks.extend(blocks)
        all_responses.extend([b[1] for b in blocks])
        for path, _ in blocks:
            slug = Path(path).stem.lower().replace(" ", "-").replace("/", "-")
            if slug not in generated_slugs:
                generated_slugs.append(slug)
        done = i + 1
        pct = done * 100 // chunk_total
        eta = ((time.time() - t_start) / done) * (chunk_total - done) if done > 0 else 0
        print(f"  [generate] {done}/{chunk_total} [{pct}% ETA {eta:.0f}s]")

    return all_file_blocks, all_responses, generated_slugs

def _run_chunk_pipeline(
    extracted_text: str, global_digest: dict, raw_file: Path, config: Config,
    template_content: str, progress: dict | None, verbose: bool,
) -> tuple[list, dict, str, list, dict]:
    """Stage 2.2 \u2192 2.3 \u2192 2.4: analyze all chunks, detect existing-wiki
    associations, then generate pages with associations fed into each prompt.

    Split (2026-06-21): analysis and generation are separate phases so Stage 2.3
    (incremental association detection) can run between them and feed back into
    the generation prompt. Returns
    ``(chunk_analyses, analysis, raw_response, file_blocks, incremental_associations)``.
    """
    # Cached: chunk analysis already complete
    if progress and progress.get("stage") in ("stage_2_2_done", "stage_2_3_done") and "chunk_analyses" in progress:
        chunk_analyses = progress["chunk_analyses"]
        print(f"  [stage 2.2] (cached) Chunk Analysis \u2014 {len(chunk_analyses)} chunks")
        _verify_stage_2_2_chunks(chunk_analyses, extracted_text)
        analysis = progress.get("analysis", {})
        raw_response = progress.get("raw_response", "")
        file_blocks = parse_file_blocks(raw_response) if raw_response else []
        incremental_associations = progress.get("incremental_associations", {})
        return chunk_analyses, analysis, raw_response, file_blocks, incremental_associations

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
    all_file_blocks, all_responses, generated_slugs = _generate_all_chunks(
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
    raw_response = "\n".join(all_responses)
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
        fa_analysis, fa_raw, fa_blocks = _stage_2_4_per_concept_fallback(
            chunk_analyses, global_digest, raw_file, config,
            template_content, verbose=verbose,
            pre_existing_slugs=generated_slugs,
        )
        fa_concept_entity = [(p, c) for p, c in fa_blocks
                             if not p.startswith("sources/")]
        all_file_blocks = fa_concept_entity
        if fa_concept_entity:
            all_responses.append(fa_raw)
            raw_response = "\n".join(all_responses)
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
    return chunk_analyses, analysis, raw_response, file_blocks, incremental_associations
