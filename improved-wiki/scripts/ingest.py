#!/usr/bin/env python3
"""
ingest.py — End-to-end Ingest for one source file (NashSU-style multi-stage pipeline).

Pipeline (aligned with ingest-stages-mandatory.md):
  1. Dedup check          (wiki/sources/ source page → skip)
  2. Extract text          (PyMuPDF first, minerU VLM OCR fallback)
  3. Global digest          (1 LLM call: book-level structural summary)
  4. Chunk + analyze       (N LLM calls: per-chunk structured analysis)
  5. Synthesize            (1 LLM call: combine digest+analyses → page specs + File blocks)
  6. Write files           (sources/ + concepts/ + entities/)
  7. Update cache          (sha256 → filesWritten[])

Usage:
  ingest.py <raw-file-path>                # process one file
  ingest.py f1.pdf f2.pdf ...              # batch mode: parallel Stage 0-2
  ingest.py --dry-run <raw-file-path>      # show what would be done, no writes
  ingest.py --verbose <raw-file-path>      # show LLM responses for debugging
  ingest.py --watch                        # continuous queue consumer (daemon mode)
  ingest.py --watch --drain                # process queue until empty, then exit
  ingest.py --watch --poll-interval 60     # re-scan queue every 60s

Configuration:
  ~/.agents/config.json   provider config (default: deepseek, caption: minimax)
  LLM_PROVIDER            override provider name (env var)
  LLM_API_KEY             override API key (env var)
  LLM_BASE_URL            override base URL (env var)
  LLM_MODEL               override model name (env var)
  LLM_CHUNK_RETRIES       extra attempts per failed chunk (default 2 → 3 total)
  Text LLM:               config.json default provider (DeepSeek V4 Pro via OpenAI protocol)
  Image caption:          config.json caption_provider (MiniMax via Anthropic protocol)
                            CAPTION_BATCH_SIZE=8   images per API call
                            CAPTION_MAX_WORKERS=6  parallel batch concurrency
  Embeddings:             local Ollama (EMBEDDING_BASE_URL / EMBEDDING_MODEL)

This script is idempotent: if the source page exists for a file, it's skipped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

# ── Imports from split stage modules (refactored 2026-06-18) ──
from _core import (
    Config, ConversationPending,
    set_current_file as _set_current_file,
    get_current_file as _get_current_file,
    file_tag as _file_tag,
    stage_begin as _stage_begin,
    stage_end as _stage_end,
    heartbeat as _heartbeat,
    llm_call_progress as _llm_call_progress,
    llm_call_done as _llm_call_done,
    record_rate_limit as _record_rate_limit,
    rate_limit_cooldown_remaining as _rate_limit_cooldown_remaining,
    load_provider_config as _load_provider_config,
    load_caption_provider as _load_caption_provider,
    str_distance as _str_distance,
    detect_template_type, load_template,
    file_sha256, load_cache, save_cache,
    progress_path, load_progress, save_progress, clear_progress,
    load_stages, mark_stage_done, is_stage_done, get_stage_payload,
    ProjectLock,
    BATCH_MAX_CONCURRENT,
    detect_domain as _detect_domain,
    list_existing_slugs,
    parse_yaml_block, parse_simple_yaml, parse_file_blocks,
    FOLDER_TO_TEMPLATE,
    is_safe_ingest_path,
)
from _stage_1_extract import (
    stage_1_1_extract_text,
    stage_1_2_extract_images,
    _stage_1_2_extract_from_mineru,
    stage_1_3_caption_images,
    _stage_1_1_check_text_quality,
    _stage_1_1_detect_pdf_type,
    _stage_1_2_media_slug,
    CAPTION_BATCH_SIZE, CAPTION_MAX_WORKERS,
)
from _stage_2_analyze import (
    stage_2_1_global_digest,
    _stage_2_1_chunk_text,
    _stage_2_2_analyze_chunk,
    _stage_2_2_chunk_retries,
    _stage_2_2_resolve_chunk_heading_path,
)
from _stage_2_4_generation import (
    _stage_2_4_build_prompt,
    stage_2_4_generate_chunk,
    _stage_2_4_extract_names,
    _stage_2_4_per_concept_fallback,
)
from _stage_2_6_source_page import stage_2_6_source_page
from _stage_2_7_query_generation import stage_2_7_query_generation, _stage_2_7_build_prompt
from _stage_2_9_comparison import (
    stage_2_9_comparison_generation,
    _stage_2_9_build_prompt_disambiguation,
    _stage_2_9_build_prompt_in_source,
)
from _stage_3_4_review import stage_3_4_review_suggestions
from _stage_3_write import (
    stage_3_1_write_wiki_file, stage_3_5_aggregate_repair,
    _stage_3_1_canonicalize_sources_field, _stage_3_1_stamp_frontmatter_dates,
    _stage_3_1_sanitize_ingested_content,
    _stage_3_1_wiki_path_for_source, _stage_3_1_merge_page_content,
    _stage_3_1_auto_correct_wiki_path, _stage_3_1_contains_cjk, _stage_3_1_make_cjk_slug,
    _stage_3_1_backup_existing_page,
)
from _stage_3_2_inject_images import stage_3_2_inject_images
from _stage_3_7_embed import stage_3_7_embed_new_pages, stage_4_1_validate_ingest
from _enrich_wikilinks import enrich_wikilinks_batch
from _watch import ingest_watch
from _stage_validators import (verify_stage_0, StageValidationError, _verify_or_die, _verify_stage_1_1_text, _verify_stage_2_1_digest, _verify_stage_2_2_chunks, _verify_stage_2_4_file_blocks, validate_stage_outputs)


# Use shared runtime detection (matches all other scripts)
_script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_script_dir))
from _paths import detect_runtime_dir  # noqa: E402
from _llm_api import set_progress_hook  # noqa: E402
from _conversation_router import (  # noqa: E402,F401  (import side-effect: registers conversation router)
    call_anthropic_protocol,
    _load_task_manifest,
)
# Wire up progress hook for LLM API calls
set_progress_hook(_llm_call_progress)

def cleanup_resolved_reviews(config: Config) -> int:
    """Delete review pages whose frontmatter has `resolved: true`.

    Called at the start of each ingest run. Returns count of deleted pages.
    """
    reviews_dir = config.wiki_dir / "REVIEW"
    if not reviews_dir.exists():
        return 0

    removed = 0
    for f in sorted(reviews_dir.rglob("*.md")):
        if not f.suffix == ".md":
            continue
        content = f.read_text(encoding="utf-8")
        # Check frontmatter for resolved: true
        m = re.search(r'^resolved:\s*true\s*$', content, re.MULTILINE)
        if m:
            f.unlink()
            removed += 1
            print(f"[cleanup] Resolved review removed: {f.name}")

    if removed > 0:
        print(f"[cleanup] {removed} resolved review page(s) deleted")

    return removed


# ---------- Stage go/no-go validation ----------

# ═══════════════════════════════════════════════════════════════
# Stage verification gates (superpowers: verification-before-completion)
# ═══════════════════════════════════════════════════════════════


def _should_stop_after(config: Config, stage: str, result: dict) -> bool:
    """Check if we should stop after completing `stage`. Progress already saved before call."""
    if config.stop_after_stage == stage:
        print(f"\n[stop-after-stage] Stage {stage} complete — clean exit (--stop-after-stage={stage})")
        return True
    return False


def _run_post_ingest_graph(config: Config) -> None:
    """Rebuild knowledge graph after ingest (once per session, stale-guarded).

    Controlled by AUTO_BUILD_GRAPH=1. The graph needs the full wiki state,
    but rebuilding after every book in a batch would be wasteful. Uses a
    staleness guard: skips if graph.json was rebuilt < 30 minutes ago.

    Mirrors NashSU's desktop app: the graph auto-refreshes when you view it.
    """
    if os.environ.get("AUTO_BUILD_GRAPH") != "1":
        return
    graph_script = Path(__file__).parent / "graph.py"
    if not graph_script.exists():
        print("[graph] graph.py not found — skipping")
        return

    # Staleness guard: don't rebuild more than once per 30 minutes
    graph_json = config.runtime_dir / "graph.json"
    if graph_json.exists():
        age_min = (time.time() - graph_json.stat().st_mtime) / 60
        if age_min < 30:
            print(f"[graph] Skipped — graph rebuilt {age_min:.0f}m ago (staleness guard)")
            return

    import subprocess
    print("[graph] Rebuilding knowledge graph...")
    try:
        result = subprocess.run(
            [sys.executable, str(graph_script)],
            cwd=config.wiki_root, capture_output=True, text=True, timeout=600,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n")[-3:]:
                print(f"[graph] {line.strip()}")
        else:
            print(f"[graph] Failed ({result.returncode}): {result.stderr[:200]}")
    except Exception as e:
        print(f"[graph] Failed ({e}) — continuing")


# ═════════════════════════════════════════════════════════
# Main pipeline — ingest_one, batch, queue, CLI
# ═════════════════════════════════════════════════════════

def ingest_one(
    raw_file: Path,
    config: Config,
    template_override: str | None = None,
    verbose: bool = False,
) -> dict:
    """Process one file end-to-end (NashSU-style 15-stage pipeline with checkpoint/resume)."""
    _set_current_file(raw_file.name)
    print(f"\n=== Ingest: {raw_file} ===")

    # 0. Clean up resolved review pages
    cleanup_resolved_reviews(config)

    # 1. Dedup + Stage 0-2 (delegated to shared implementation)
    h = file_sha256(raw_file)
    config.conversation_prefix = h[-8:]  # per-source conversation file isolation
    task_manifest = _load_task_manifest(config)
    pending_tasks = task_manifest.get("pending", [])
    if pending_tasks:
        print(f"[conversation] {len(pending_tasks)} pending task(s) — resuming pipeline")

    # Stage-completion markers (Option A) drive resume semantics: the skip-check
    # only short-circuits once stage_4_1 is done, so a mid-flight resume (pages
    # written but post-review stages pending) is never dropped.  _do_write in
    # turn skips the non-idempotent 3.1 write loop when `write_phase` is marked.
    prepared = _do_prepare(raw_file, config, template_override, verbose)
    if prepared is None:
        return {"status": "skipped", "reason": "source-page-exists"}

    # Unpack prepared state from Stage 0-2
    method = prepared["method"]
    extracted_text = prepared["extracted_text"]
    global_digest = prepared["global_digest"]
    chunk_analyses = prepared["chunk_analyses"]
    analysis = prepared["analysis"]
    raw_response = prepared["raw_response"]
    file_blocks = prepared["file_blocks"]
    stage_1_2_result = prepared["stage_1_2_result"]
    stage_1_3_result = prepared["stage_1_3_result"]
    template_name = prepared["template_name"]

    # Check stop-after-stage (best-effort; _do_prepare runs all of Stage 0-2)
    for stage_check in ("0", "0.5", "0.6", "1", "1.5", "2.0", "2", "2.3", "2.5"):
        if _should_stop_after(config, stage_check, {"status": "ok"}):
            return {"status": "ok", "stopped_after": stage_check}

    # Stage 3+: Delegate to _do_write (shared with batch path)
    prepared = {
        "raw_file": raw_file, "config": config, "h": h, "method": method,
        "extracted_text": extracted_text, "global_digest": global_digest,
        "chunk_analyses": chunk_analyses, "analysis": analysis,
        "raw_response": raw_response, "file_blocks": file_blocks,
        "stage_1_2_result": stage_1_2_result, "stage_1_3_result": stage_1_3_result,
        "template_name": template_name,
        "enrich_enabled": getattr(config, "enrich_enabled", True),
    }
    result = _do_write(prepared, verbose=verbose)
    if result["status"] != "ok":
        return result

    files_written = result["files_written"]

    # ── Post-ingest (unique to single-book path) ──
    _run_post_ingest_graph(config)
    stage_3_7_embed_new_pages(config, files_written)
    stage_4_1_validate_ingest(config, raw_file)

    # Mark the ingest fully complete so future re-runs skip cleanly (Option A).
    mark_stage_done(config, h, "stage_4_1")

    return {"status": "ok", "files_written": files_written}


# ═══════════════════════════════════════════════════════════════
# Batch ingest: parallel Stage 0-2, serial Stage 3+
# ═══════════════════════════════════════════════════════════════

def _stage_0_2_should_skip(raw_file: Path, config: Config) -> bool:
    """Return True if the source page already exists and is reasonably complete.

    Stage 0.2: Re-ingest when source page is missing >80% of linked concept/entity
    pages (corrupt / partial prior run); otherwise skip.

    Verification checklist:
    1. Source page file exists
    2. Frontmatter type == "source"
    3. ≥80% of wikilinks point to existing concept/entity pages

    Primary gate (Option A): skip only once the ingest has fully completed
    (stage_4_1 marker set).  This prevents a mid-flight conversation-mode
    resume — where pages are written but post-review stages (3.5-4.1) are
    still pending — from being short-circuited by the "source page exists"
    heuristic below.
    """
    h = file_sha256(raw_file)
    if is_stage_done(config, h, "stage_4_1"):
        if not _stage_3_1_wiki_path_for_source(raw_file, config).exists():
            # Stale marker (source page deleted externally) — clear and re-ingest.
            from _core import stages_path as _sp
            _sp(config, h).unlink(missing_ok=True)
            return False
        print(f"  [skip] Ingest complete (stage_4_1 marker present)")
        return True

    source_page = _stage_3_1_wiki_path_for_source(raw_file, config)
    if not source_page.exists():
        return False

    # Source page exists but stage_4_1 not done → mid-flight resume.  Do NOT
    # skip: post-review stages (3.5-4.1) may still be pending.  The write_phase
    # marker inside _do_write handles skipping the non-idempotent 3.1 loop.
    print(f"  [skip:resume] Source page exists, stage_4_1 not done — resuming")
    return False

    # Verify source page is readable and has valid frontmatter
    try:
        source_text = source_page.read_text(encoding="utf-8", errors="strict")
    except Exception as e:
        print(f"  [skip:error] Source page unreadable ({e}) — re-ingesting")
        return False

    # Verify frontmatter type is "source"
    if not source_text.startswith("---"):
        print(f"  [skip:error] Source page missing frontmatter — re-ingesting")
        return False

    try:
        fm_end = source_text.find("---", 3)
        if fm_end == -1:
            print(f"  [skip:error] Source page frontmatter unclosed — re-ingesting")
            return False
        frontmatter_block = source_text[3:fm_end]
        fm_type = None
        for line in frontmatter_block.split("\n"):
            if line.strip().startswith("type:"):
                fm_type = line.split(":", 1)[1].strip().strip("'\"")
                break
        if fm_type != "source":
            print(f"  [skip:error] Source page type is '{fm_type}', not 'source' — re-ingesting")
            return False
    except Exception as e:
        print(f"  [skip:error] Frontmatter parse error ({e}) — re-ingesting")
        return False

    # Extract wikilinks: [[slug]] or [[slug|display]]
    # Improved regex: match [[ ... ]] with no nested brackets
    refs = re.findall(r'\[\[([^\[\]]+)\]\]', source_text)
    # Wikilinks may be type-prefixed ([[concepts/foo]], per Stage 2.4/2.6 convention)
    # or bare ([[foo]], per the wikilink-enrichment convention) — support both.
    known_type_dirs = ("concepts", "entities", "sources", "queries", "comparisons",
                        "synthesis", "findings", "thesis", "methodology")
    missing = []
    for ref in refs:
        slug = ref.split("|")[0].strip()
        if not slug:
            continue
        prefix, _, rest = slug.partition("/")
        if prefix in known_type_dirs and rest:
            target_path = config.wiki_dir / prefix / f"{rest}.md"
            if not target_path.exists():
                missing.append(slug)
            continue
        concept_path = config.wiki_dir / "concepts" / f"{slug}.md"
        entity_path = config.wiki_dir / "entities" / f"{slug}.md"
        if not concept_path.exists() and not entity_path.exists():
            missing.append(slug)

    if not refs or len(missing) > len(refs) * 0.8:
        ratio_str = f"{len(missing)}/{len(refs)}" if refs else "0/0"
        print(f"  [skip:warn] Source page exists but {ratio_str} linked pages missing — re-ingesting")
        return False

    ratio_found = len(refs) - len(missing)
    print(f"  [skip] Source page exists ({ratio_found}/{len(refs)} linked pages found)")
    return True


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


def _prepare_source_page(
    global_digest: dict, raw_file: Path, config: Config,
    template_content: str, progress: dict | None, file_blocks: list,
    verbose: bool,
) -> list:
    """Stage 2.6: generate the source page (dedicated LLM call) and merge into file_blocks."""
    current_domain = _detect_domain(raw_file, template_content, global_digest)
    if progress and progress.get("stage") in ("stage_2_4_done", "stage_2_3_done") and "source_page_response" in progress:
        source_page_response = progress["source_page_response"]
        print(f"  [stage 2.6] (cached) Source page already generated")
    else:
        source_page_response, _ = stage_2_6_source_page(
            global_digest, raw_file, config,
            template=template_content, current_domain=current_domain, verbose=verbose
        )

    if not source_page_response:
        return file_blocks

    source_blocks = parse_file_blocks(source_page_response)
    if source_blocks:
        file_blocks = source_blocks + list(file_blocks)
        print(f"  [stage 2.6] Source page block merged ({len(file_blocks)} total)")
        return file_blocks

    # LLM didn't use FILE block format — generate placeholder
    source_rel = f"sources/{raw_file.relative_to(config.raw_root).with_suffix('.md')}"
    book_meta = global_digest.get("book_meta", {})
    title = book_meta.get("title", raw_file.stem) if isinstance(book_meta, dict) else raw_file.stem
    stub = f"---\ntype: source\ntitle: \"{title}\"\ndomain: general\n"
    stub += f"created: {time.strftime('%Y-%m-%d')}\nupdated: {time.strftime('%Y-%m-%d')}\n"
    stub += f"tags: []\nrelated: []\nsources: [\"raw/{raw_file.relative_to(config.raw_root)}\"]\n---\n\n"
    stub += f"**Title:** {title}\n**Author:** {raw_file.stem}\n\n"
    stub += f"## Global Digest\n\n```yaml\n{json.dumps(global_digest, ensure_ascii=False, indent=2)[:4000]}\n```\n\n"
    stub += f"## Key Concepts\n\n"
    for path, _ in file_blocks:
        if "concepts/" in path:
            stub += f"- [[{Path(path).stem}]]\n"
    file_blocks.append((source_rel, stub))
    print(f"  [stage 2.6] Placeholder source page generated ({len(file_blocks)} total)")
    return file_blocks


def _do_prepare(
    raw_file: Path, config: Config,
    template_override: str | None = None,
    verbose: bool = False,
) -> dict | None:
    """Stage 0-2 for one book.  Read-only: no shared state writes, no lock needed.

    Returns a dict with all data needed for Stage 3+, or None on skip/failure.
    Suitable for parallel execution across multiple books.
    """
    _set_current_file(raw_file.name)
    print(f"\n=== [prepare] {raw_file.name} ===")
    try:
        # Dedup check — skip only if the ingest is truly complete (stage_4_1
        # marker set) or the source page exists and is reasonably complete.
        if _stage_0_2_should_skip(raw_file, config):
            return None

        h = file_sha256(raw_file)
        progress = load_progress(config, h)

        # Stage 0: Text extraction
        if progress and "extracted_text" in progress:
            extracted_text = progress["extracted_text"]
            method = progress.get("extract_method", "cached")
            print(f"  [extract] (cached) {method}: {len(extracted_text)} chars")
        else:
            extracted_text, method = stage_1_1_extract_text(raw_file, config)
            print(f"  [extract] {method}: {len(extracted_text)} chars")
            _verify_stage_1_1_text(raw_file, extracted_text, method)

            # Stage 0 Validation (Phase 2: per-stage verification)
            if not verify_stage_0(extracted_text):
                print(f"  [validate] ❌ Stage 0 failed: text extraction insufficient")
                raise StageValidationError("Stage 0: text extraction failed")

            qr = _stage_1_1_check_text_quality(extracted_text, raw_file.name)
            if qr["status"] == "severe":
                print(f"  [extract] ❌ Text quality SEVERE — aborting ingest. "
                      f"Re-run with a different PDF or re-download from source.")
                return None
            save_progress(config, h, {
                "stage": "stage_1_1_done", "extracted_text": extracted_text,
                "extract_method": method,
            })

        # Template
        template_name = detect_template_type(raw_file, config.raw_root, template_override)
        template_content = load_template(template_name)
        print(f"  [template] {template_name}")

        # ── Stage 1.2 + 1.3 pipeline (1.2 → 1.3 sequential) ∥ Stage 2.1 (parallel) ──
        # Helper: run 1.2→1.3 together (1.3 depends on 1.2 output)
        def _run_image_pipeline():
            stage_1_2_result: dict = {"count": 0}
            if progress and "stage_1_2" in progress:
                stage_1_2_result = progress["stage_1_2"]
                print(f"  [stage 1.2] (cached) {stage_1_2_result.get('count', 0)} images")
            elif method.startswith("mineru"):
                # Covers "mineru", "mineru-local-ocr", "mineru-local-ocr-low-quality"
                # (low-quality OCR still ran and produced images on disk —
                # excluding it here silently dropped every image from those
                # sources even though minerU already extracted them).
                ocr_out = config.extract_tmp_dir / raw_file.stem
                if ocr_out.exists():
                    stage_1_2_result = _stage_1_2_extract_from_mineru(ocr_out, config, raw_file)
                # Save progress immediately after 1.2 completes
                cp = {"stage": "stage_1_1_done", "extracted_text": extracted_text,
                      "extract_method": method, "stage_1_2": stage_1_2_result}
                save_progress(config, h, cp)
            elif raw_file.suffix.lower() in (".pptx", ".docx"):
                # Covers "zipfile-pptx", "zipfile-docx". PDFs no longer reach
                # here since 2026-06-23: all PDF extraction routes through
                # minerU (pipeline or VLM) and is handled by the branch above.
                # stage_1_2_extract_images() branches on file suffix internally.
                stage_1_2_result = stage_1_2_extract_images(raw_file, config)

            # Stage 1.3: Caption extracted images (runs if 1.2 found images)
            stage_1_3_result = {"captioned": 0}
            needs_caption = (not progress or "stage_1_3" not in progress) and stage_1_2_result.get("count", 0) > 0
            if needs_caption:
                stage_1_3_result = stage_1_3_caption_images(config, stage_1_2_result)
            elif progress and "stage_1_3" in progress:
                stage_1_3_result = progress["stage_1_3"]
                print(f"  [stage 1.3] (cached) {stage_1_3_result.get('captioned', 0)} captions")

            return stage_1_2_result, stage_1_3_result

        # Parallel execution: 1.2→1.3 pipeline vs 2.1 global digest
        needs_digest = (
            not progress or progress.get("stage") not in ("stage_2_1_done", "stage_2_2_done", "stage_2_3_done")
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            fut_images = executor.submit(_run_image_pipeline)
            fut_digest = executor.submit(stage_2_1_global_digest, extracted_text, raw_file, config,
                                        template_content, verbose=verbose) if needs_digest else None

            stage_1_2_result, stage_1_3_result = fut_images.result()

            # Persist Stage 1.2/1.3 immediately, before awaiting the digest future.
            # fut_digest.result() below can raise ConversationPending (conversation-
            # mode cache miss), which propagates out of this function before a
            # later save_progress call would ever be reached — every subsequent
            # conversation-mode round-trip would otherwise re-run
            # _run_image_pipeline() from scratch for this source, forever.
            if not progress or "stage_1_2" not in progress:
                save_progress(config, h, {
                    "stage": "stage_1_1_done", "extracted_text": extracted_text,
                    "extract_method": method,
                    "stage_1_2": stage_1_2_result, "stage_1_3": stage_1_3_result,
                })

            global_digest = fut_digest.result() if fut_digest else progress.get("global_digest", {})

        if needs_digest:
            _verify_stage_2_1_digest(global_digest, raw_file)
        else:
            print(f"  [stage 2.1] (cached) Global Digest — {len(global_digest)} keys")
            _verify_stage_2_1_digest(global_digest, raw_file)

        # Stage 2.2 + 2.4: Chunk Analysis → Generation (barrier-free pipeline)
        chunk_analyses, analysis, raw_response, file_blocks, incremental_associations = _run_chunk_pipeline(
            extracted_text, global_digest, raw_file, config, template_content,
            progress, verbose)

        # Stage 2.5: In-source concept dedup & merge (multi-chunk books only).
        # Runs before the source page so the index lists de-duplicated concepts.
        from _stage_2_5_dedup import stage_2_5_dedup
        _stage_2_5 = stage_2_5_dedup(file_blocks, chunk_analyses, config, verbose=verbose)
        file_blocks = _stage_2_5["file_blocks"]
        dedup_was_run = _stage_2_5["dedup_was_run"]
        concept_count_before = _stage_2_5["concept_count_before"]
        concept_count_after = _stage_2_5["concept_count_after"]

        # Stage 2.6: Source page generation + merge
        file_blocks = _prepare_source_page(
            global_digest, raw_file, config, template_content, progress,
            file_blocks, verbose)
        _verify_stage_2_4_file_blocks(file_blocks, raw_file, incremental_associations)

        # ── Stage 2.7: Query generation ──
        query_blocks, _ = stage_2_7_query_generation(
            global_digest, chunk_analyses, file_blocks, raw_file, config,
            template=template_content, verbose=verbose
        )
        if query_blocks:
            file_blocks = list(file_blocks) + query_blocks

        # ── Stage 2.8: Cross-source query resolution ──
        # LLM judge closes queries already answered elsewhere; defaults to "kept".
        from _stage_2_8_query_resolve import (stage_2_8_resolve_queries,
                                               _stage_2_8_update_file_blocks_after_resolution)
        query_resolutions = stage_2_8_resolve_queries(file_blocks, config.wiki_dir, config)
        if any(r["status"] == "closed" for r in query_resolutions.values()):
            before_q = len(file_blocks)
            file_blocks = _stage_2_8_update_file_blocks_after_resolution(file_blocks, query_resolutions)
            print(f"  [stage 2.8] Removed {before_q - len(file_blocks)} closed query block(s)")

        # ── Stage 2.9: Comparison generation ──
        comp_blocks, _ = stage_2_9_comparison_generation(
            global_digest, chunk_analyses, file_blocks, raw_file, config,
            template=template_content, verbose=verbose
        )
        if comp_blocks:
            file_blocks = list(file_blocks) + comp_blocks

        query_count = len(query_blocks)
        comp_count = len(comp_blocks)

        analysis["__source_hash"] = h
        analysis["__extract_method"] = method

        print(f"  [prepare] ✅ done — {len(file_blocks)} blocks")
        current_domain = _detect_domain(raw_file, template_content, global_digest)
        return {
            "raw_file": raw_file, "config": config,
            "h": h, "method": method, "extracted_text": extracted_text,
            "global_digest": global_digest, "chunk_analyses": chunk_analyses,
            "analysis": analysis, "raw_response": raw_response,
            "file_blocks": file_blocks,
            "stage_1_2_result": stage_1_2_result,
            "stage_1_3_result": stage_1_3_result,
            "template_name": template_name,
            "query_count": query_count,
            "comp_count": comp_count,
            "concept_merge_stats": (concept_count_before, concept_count_after),
            "dedup_was_run": dedup_was_run,
            "current_domain": current_domain,
            "incremental_associations": incremental_associations,
            "query_resolutions": query_resolutions,
            "enrich_enabled": getattr(config, "enrich_enabled", True),
        }
    except Exception as e:
        print(f"  [prepare] ❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
        raise


def _do_write(prepared: dict, verbose: bool = False) -> dict:
    """Stage 3+ for one book.  Writes wiki files, updates cache, runs validation.
    MUST be called serially — modifies shared wiki/ state.
    """
    raw_file = prepared["raw_file"]
    config = prepared["config"]
    h = prepared["h"]
    method = prepared["method"]
    extracted_text = prepared["extracted_text"]
    global_digest = prepared["global_digest"]
    chunk_analyses = prepared["chunk_analyses"]
    analysis = prepared["analysis"]
    raw_response = prepared["raw_response"]
    file_blocks = prepared["file_blocks"]
    stage_1_2_result = prepared["stage_1_2_result"]
    stage_1_3_result = prepared["stage_1_3_result"]
    template_name = prepared["template_name"]
    query_count = prepared.get("query_count", 0)
    comp_count = prepared.get("comp_count", 0)

    print(f"\n=== [write] {raw_file.name} ===")

    # Write wiki files (same logic as ingest_one Stage 3+)
    source_path = _stage_3_1_wiki_path_for_source(raw_file, config)
    files_written_paths: list[str] = []
    hard_failures: list[str] = []
    source_block: tuple[str, str] | None = None

    _VALID_SUBDIRS = {"sources", "concepts", "entities", "queries", "comparisons",
                      "synthesis", "findings", "thesis"}
    _LISTING_PAGES = {"index.md", "log.md", "overview.md", "schema.md"}

    try:
        from _language import detect_language
        expected_lang = detect_language(extracted_text[:5000]) if extracted_text else "unknown"
    except ImportError:
        expected_lang = "unknown"

    canonical_source = f"raw/{raw_file.relative_to(config.raw_root)}"
    today_str = time.strftime("%Y-%m-%d")

    # ── Wikilink enrichment setup (round iv, 2026-06-22) ──
    # After every page in this ingest is written, one batched LLM call (via
    # the conversation router — see _enrich_wikilinks.enrich_wikilinks_batch)
    # suggests [[wikilinks]] for body terms matching existing wiki pages or
    # sibling pages from this same ingest. Gated by --enrich-wikilinks /
    # --no-enrich. existing_slugs is a pre-loop snapshot of the wiki.
    # enrich_candidates collects (rel_path, full_path) for non-listing pages
    # written this ingest; the batch call happens once, after the loop.
    enrich_enabled = prepared.get("enrich_enabled", True)
    existing_slugs = list_existing_slugs(config) if enrich_enabled else []
    enrich_candidates: list[tuple[str, Path]] = []

    # Option A: stage-aware resume.  If the write phase (3.1 write loop +
    # enrichment + 3.2 inject + 3.3 collision) already completed in a prior
    # run, skip it entirely.  Re-running the write loop would spuriously fire
    # page-merge LLM round-trips because post-write steps (enrichment, image
    # injection) have mutated page bodies.  Restore file list from the marker.
    write_phase_done = is_stage_done(config, h, "write_phase")
    if write_phase_done:
        print("  [write] write_phase marker present — skipping 3.1/3.2/3.3")
        _wp = get_stage_payload(config, h, "write_phase")
        files_written_paths = _wp.get("files_written", [])
        source_block = ("source", "")  # source page already written
        hard_failures = []
        stage_3_2_result = {"injected": _wp.get("images_injected", 0)}
        stage_3_3_result = {"items": _wp.get("collision_items", 0)}
    _write_blocks = [] if write_phase_done else file_blocks

    for rel_path, content in _write_blocks:
        if ".." in rel_path or rel_path.startswith("/"):
            continue
        if not is_safe_ingest_path(rel_path):
            continue

        top_dir = rel_path.split("/")[0] if "/" in rel_path else ""
        basename = Path(rel_path).name
        if basename in _LISTING_PAGES:
            pass
        elif top_dir not in _VALID_SUBDIRS:
            corrected = _stage_3_1_auto_correct_wiki_path(rel_path, content, config)
            if corrected:
                print(f"  [write] Auto-corrected: {rel_path} → {corrected}")
                rel_path = corrected
            else:
                print(f"  [write] Dropped — cannot correct path: {rel_path}")
                continue

        if not rel_path.endswith(".md"):
            rel_path = rel_path + ".md"

        # Skip per-block language check for minerU OCR — OCR text from garbled
        # PDFs confuses the detector (e.g. C0 control chars → wrong language).
        # All minerU extraction methods carry a `mineru-*` label, so match by
        # prefix; an explicit list previously missed variants and fired false
        # warnings.
        if not method.startswith("mineru"):
            if expected_lang not in ("unknown", "English"):
                try:
                    from _language import detect_language
                    block_lang = detect_language(content[:2000])
                    if block_lang not in (expected_lang, "English") and block_lang != "unknown":
                        print(f"  [lang] ⚠️  {rel_path}: expected {expected_lang}, got {block_lang}")
                except ImportError:
                    pass

        content = _stage_3_1_canonicalize_sources_field(content, canonical_source)
        content = _stage_3_1_stamp_frontmatter_dates(content, today_str)

        full_path = config.wiki_dir / rel_path
        is_listing = basename in _LISTING_PAGES
        do_merge = full_path.exists() and not is_listing

        try:
            stage_3_1_write_wiki_file(full_path, content, config, merge=do_merge)
        except OSError as e:
            print(f"  [write] HARD ERROR: {rel_path} — {e}")
            hard_failures.append(rel_path)
            continue

        files_written_paths.append(str(full_path.relative_to(config.wiki_root)))
        if full_path == source_path:
            source_block = (rel_path, content)
        action = "[merge]" if do_merge else "[overwrite]" if is_listing and full_path.exists() else "[write]"
        print(f"  {action} {rel_path}")

        if enrich_enabled and not is_listing:
            enrich_candidates.append((rel_path, full_path))

    if enrich_enabled and enrich_candidates and not write_phase_done:
        # Enrich the ACTUAL written content (post-merge) so links target real
        # pages. One batched call for the whole ingest — see
        # _enrich_wikilinks.enrich_wikilinks_batch. Not wrapped in try/except:
        # a malformed response or routing error now fails the ingest visibly,
        # same as any other stage.
        pages_for_enrichment = [
            (rel_path, full_path.read_text(encoding="utf-8"))
            for rel_path, full_path in enrich_candidates
        ]
        enriched_pages = enrich_wikilinks_batch(pages_for_enrichment, existing_slugs, config)
        for rel_path, full_path in enrich_candidates:
            if rel_path in enriched_pages:
                full_path.write_text(enriched_pages[rel_path], encoding="utf-8")
                print(f"  [enrich] {rel_path} (+wikilinks)")

    if not source_block:
        # Build NashSU-quality source page from digest data (no LLM needed)
        book_meta = analysis.get("book_meta", {})
        outline = analysis.get("outline", [])
        key_claims = analysis.get("key_claims", [])
        title = book_meta.get("title", raw_file.stem)
        authors = book_meta.get("authors", [])
        year = book_meta.get("year", "")
        publisher = book_meta.get("publisher", "")

        lines = [
            "---",
            "type: source",
            f'title: "{title}"',
            "domain: general",
            f"created: {today_str}",
            f"updated: {today_str}",
            "tags: []",
            "related: []",
            f'sources: ["{canonical_source}"]',
            "---",
            "",
            f"# {title}",
            "",
        ]
        if authors:
            lines.append(f"**Authors:** {', '.join(str(a) for a in authors[:5])}")
        if year:
            lines.append(f"**Year:** {year}")
        if publisher:
            lines.append(f"**Publisher:** {publisher}")
        lines.append("")

        if outline:
            lines.append("## Table of Contents & Key Concepts")
            lines.append("")
            for ch in outline[:40]:
                if isinstance(ch, dict):
                    ch_title = ch.get("title", "")
                    topics = ch.get("key_topics", [])
                    topics_str = ", ".join(str(t) for t in topics[:4]) if topics else ""
                else:
                    ch_title = str(ch)
                    topics_str = ""
                lines.append(f"1. **{ch_title}**" + (f" — {topics_str}" if topics_str else ""))
            lines.append("")

        if key_claims:
            lines.append("## Key Takeaways")
            lines.append("")
            for claim in key_claims[:10]:
                if isinstance(claim, dict):
                    lines.append(f"- {claim.get('claim', str(claim))}")
                else:
                    lines.append(f"- {str(claim)}")
            lines.append("")

        placeholder_content = "\n".join(lines) + "\n"
        try:
            stage_3_1_write_wiki_file(source_path, placeholder_content, config)
            files_written_paths.append(str(source_path.relative_to(config.wiki_root)))
        except OSError as e:
            hard_failures.append("source-placeholder")

    # Stage 3.2: Image injection
    if not write_phase_done:
        stage_3_2_result: dict = {"injected": 0}
        if source_path.exists():
            stage_3_2_result = stage_3_2_inject_images(config, raw_file, source_path, method)

        # Stage 3.3: Cross-domain slug collision review
        from _stage_3_write import stage_3_3_slug_collision_review
        stage_3_3_result = stage_3_3_slug_collision_review(
            file_blocks, prepared.get("current_domain", "general"), config, verbose=verbose)

        # Mark write phase complete so a post-review resume skips 3.1-3.3
        # (prevents spurious page-merge / re-enrichment / re-injection).
        mark_stage_done(config, h, "write_phase", payload={
            "files_written": files_written_paths,
            "images_injected": stage_3_2_result.get("injected", 0),
            "collision_items": stage_3_3_result.get("items", 0),
        })

    # Stage 3.4: Review (quality review of generated pages)
    stage_3_4_result = stage_3_4_review_suggestions(
        file_blocks, raw_file, config, raw_response=raw_response, verbose=verbose)

    # Go/no-go validation
    go_nogo_warnings = validate_stage_outputs(
        config, raw_file, method, extracted_text,
        stage_1_2_result, stage_1_3_result,
        file_blocks, source_path,
    )


    # Stage 3.5: Aggregate repair
    index_log_files = stage_3_5_aggregate_repair(source_path, raw_file, analysis, h, method, config)

    # Update cache
    try:
        rel = str(raw_file.relative_to(config.raw_root))
    except ValueError:
        rel = str(raw_file)
    cache = load_cache(config)
    cache["entries"][rel] = {
        "hash": h,
        "timestamp": int(time.time() * 1000),
        "filesWritten": files_written_paths + index_log_files,
        "method": method,
        "template": template_name,
        "sourceHash": h,
        "fileBlockCount": len(file_blocks),
        "stages": {
            "global_digest_keys": len(global_digest),
            "chunks_analyzed": len(chunk_analyses),
            "file_blocks_generated": len(file_blocks),
            "concepts_identified": analysis.get("concepts_identified", len(file_blocks)),
            "concepts_core": analysis.get("concepts_core", 0),
            "concepts_supporting": analysis.get("concepts_supporting", 0),
            "concepts_generated": analysis.get("concepts_generated", len(file_blocks)),
            "coverage_core": analysis.get("coverage_core", 1.0),
            "coverage_supporting": analysis.get("coverage_supporting", 1.0),
            "coverage_pct": analysis.get("coverage_pct", 1.0),
            "images_extracted": stage_1_2_result.get("count", 0),
            "images_captioned": stage_1_3_result.get("captioned", 0),
            "images_injected": stage_3_2_result.get("injected", 0),
            "queries_generated": query_count,
            "comparisons_generated": comp_count,
            "review_items": stage_3_3_result.get("items", 0),
        },
    }
    if hard_failures:
        print(f"  [cache] SKIPPED — {len(hard_failures)} hard failure(s)")
        return {"status": "hard-error", "hard_failures": hard_failures,
                "files_written": files_written_paths + index_log_files}
    try:
        save_cache(config, cache)
        clear_progress(config, h)
        print(f"  [cache] saved")
    except OSError as e:
        return {"status": "hard-error", "error": str(e),
                "files_written": files_written_paths + index_log_files}

    # Stage 3.6: Quality scoring card (always runs; flags needs_review < 0.65)
    from _stage_3_6_quality import stage_3_6_quality
    _q_review = stage_3_3_result.get("items", 0)
    _q_stats = prepared.get("concept_merge_stats", (0, 0))
    _q_dedup_ran = prepared.get("dedup_was_run", False)
    quality_result = stage_3_6_quality(
        raw_file, config, extracted_text,
        stage_1_2_result.get("count", 0), stage_1_3_result.get("captioned", 0),
        file_blocks, _q_review, _q_stats, _q_dedup_ran, verbose=verbose)

    # Note: Detailed validation moved to separate 'validate' command (Phase 2 refactor)
    # Ingest now focuses on generation (Stages 0-3.5) with per-stage validation
    # For detailed quality checks, run: python3 validate.py <source_slug>
    # Stage 3.7 (embeddings) runs in the post-ingest section of ingest_one —
    # single entry point, mandatory attempt against local Ollama bge-m3
    # (prints an install reminder instead of silently skipping if unavailable).

    return {"status": "ok", "files_written": cache["entries"][rel]["filesWritten"]}


def batch_ingest(
    raw_files: list[Path],
    config: Config,
    max_concurrent: int = BATCH_MAX_CONCURRENT,
    template_override: str | None = None,
    verbose: bool = False,
) -> list[dict]:
    """Ingest multiple books with parallel Stage 0-2 and serial Stage 3+.

    Why this works:
      - Stage 0-2 (text extraction, digest, chunk analysis, synthesis) are
        read-only LLM calls. No wiki/ files are written, no shared state
        is mutated. Different books' Stage 0-2 can run concurrently.
      - Stage 3+ (file write, cache update, lint, archive, validation)
        modifies shared wiki/ state. MUST be serialized to avoid races.

    Max concurrency: {BATCH_MAX_CONCURRENT} by default.  Increase if your
    LLM API has generous rate limits.  Memory/CPU usage is negligible
    (just API call orchestration).
    """
    if max_concurrent < 1:
        max_concurrent = 1
    max_concurrent = min(max_concurrent, len(raw_files))

    print(f"\n{'='*60}")
    print(f"Batch ingest: {len(raw_files)} books, max {max_concurrent} concurrent")
    print(f"{'='*60}")

    # Pipeline: parallel prepare (Stage 0-2) → serial write (Stage 3+).
    # Books are written as soon as their Stage 2 finishes — no need to wait
    # for all books.  Write order is completion order, not submission order.
    lock = ProjectLock(config, owner_id="batch")
    if not lock.acquire():
        raise RuntimeError("Could not acquire project lock for batch write phase")

    results: list[dict] = []
    prepared_count = 0
    total_books = len(raw_files)

    try:
        with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
            futures: dict[concurrent.futures.Future, Path] = {}
            for f in raw_files:
                futures[executor.submit(
                    _do_prepare, f, config, template_override, verbose
                )] = f

            for future in as_completed(futures):
                # Isolate prepare-phase failures per book, the same way the
                # write phase below is isolated — one bad source (e.g. a
                # corrupt PDF) must not abort the rest of the batch.
                try:
                    prepared = future.result()
                except Exception as e:
                    prepared_count += 1
                    print(f"\n[batch] {prepared_count}/{total_books} prepare FAILED for "
                          f"{futures[future].name}: {e}", flush=True)
                    import traceback
                    traceback.print_exc()
                    continue
                prepared_count += 1
                if prepared is None:
                    print(f"\n[batch] {prepared_count}/{total_books} prepared (skipped)", flush=True)
                    continue

                print(f"\n[batch] {prepared_count}/{total_books} prepared — writing immediately ({prepared['raw_file'].name})", flush=True)
                try:
                    result = _do_write(prepared, verbose=verbose)
                    results.append(result)
                except Exception as e:
                    print(f"[batch] Write failed for {prepared['raw_file'].name}: {e}")
                    import traceback
                    traceback.print_exc()
    finally:
        lock.release()

    ok = sum(1 for r in results if r.get("status") == "ok")
    print(f"\n{'='*60}")
    print(f"Batch complete: {ok}/{len(results)} books processed successfully")
    print(f"{'='*60}")

    # Staleness-guarded: rebuild graph after batch (no-op if <30min since last rebuild)
    if ok > 0:
        _run_post_ingest_graph(config)

    return results


# ---------- CLI ----------

def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest source files into the wiki (NashSU-style 15-stage)")
    parser.add_argument("file", nargs="*", help="Path(s) to raw source file(s). Multiple files enable batch mode. "
                        "Omit with --watch to consume the queue.")
    parser.add_argument("--type", help="Override template type (book/paper/datasheet/...)")
    parser.add_argument("--parallel", type=int, default=0,
                        help=f"Max concurrent books for Stage 0-2 (default: {BATCH_MAX_CONCURRENT} if multiple files, 1 for single)")
    parser.add_argument("--dry-run", action="store_true", help="Don't write anything")
    parser.add_argument("--delete", action="store_true",
                        help="Delete source: remove source page, cache entry, and cleanup orphans (NashSU source-lifecycle parity)")
    parser.add_argument("--enrich-wikilinks", action="store_true", default=True,
                        help="Auto-enrich new pages with [[wikilinks]] after write (NashSU enrich-wikilinks parity)")
    parser.add_argument("--no-enrich", action="store_true",
                        help="Disable wikilink enrichment")
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print LLM responses for debugging",
    )
    parser.add_argument(
        "--watch", action="store_true",
        help="Continuously watch ingest-queue.json and process pending entries. "
             "New entries added by wiki-monitor.sh are picked up automatically.",
    )
    parser.add_argument(
        "--poll-interval", type=int, default=30,
        help="Seconds between queue re-scans in --watch mode (default: 30)",
    )
    parser.add_argument(
        "--drain", action="store_true",
        help="With --watch: exit when the queue is empty instead of looping forever.",
    )
    parser.add_argument(
        "--max-retries", type=int, default=3,
        help="Max attempts per queued entry before giving up (default: 3)",
    )
    parser.add_argument(
        "--stop-after-stage",
        default=None,
        choices=["0", "0.5", "0.6", "1", "1.5", "2", "2.0", "2.3", "2.5", "2.5c", "3", "3.5", "4"],
        help="Stop pipeline after completing the named stage (clean exit, cache saved). "
             "Use for chunked runs to avoid Bash timeout. "
             "Stages: 0=text+image extract, 1=global digest, 1.5=chunk analysis, "
             "2=concept/entity gen, 2.5=review, 3=write+merge+enrich",
    )
    args = parser.parse_args()

    # ── Watch mode: continuous queue consumer ──
    if args.watch:
        config = Config.from_env()
        config.enrich_enabled = args.enrich_wikilinks and not args.no_enrich
        config.runtime_dir.mkdir(parents=True, exist_ok=True)
        max_conc = args.parallel if args.parallel > 0 else BATCH_MAX_CONCURRENT
        ingest_watch(
            config,
            poll_interval=args.poll_interval,
            drain=args.drain,
            max_concurrent=max_conc,
            max_retries=args.max_retries,
            verbose=args.verbose,
        )
        return 0

    if not args.file:
        parser.print_help()
        print("\nTip: use --watch to process the queue, or pass file(s) for direct ingest.", file=sys.stderr)
        return 1

    # ── Source lifecycle: delete ──
    if args.delete:
        config = Config.from_env()
        from _source_lifecycle import delete_source
        for f in args.file:
            rf = Path(f).expanduser().resolve()
            delete_source(rf, config)
        return 0

    config = Config.from_env()
    config.enrich_enabled = args.enrich_wikilinks and not args.no_enrich
    config.stop_after_stage = args.stop_after_stage

    raw_files = []
    for f in args.file:
        rf = Path(f).expanduser().resolve()
        if not rf.exists():
            print(f"ERROR: {rf} not found", file=sys.stderr)
            return 1
        if not rf.is_relative_to(config.raw_root):
            print(f"ERROR: {rf} is not under raw_root ({config.raw_root})", file=sys.stderr)
            return 1
        raw_files.append(rf)

    # Batch mode: multiple files or explicit --parallel
    if len(raw_files) > 1 or args.parallel > 1:
        max_conc = args.parallel if args.parallel > 0 else BATCH_MAX_CONCURRENT
        results = batch_ingest(
            raw_files, config, max_concurrent=max_conc,
            template_override=args.type, verbose=args.verbose,
        )
        ok = sum(1 for r in results if r.get("status") == "ok")
        return 0 if ok == len(results) else 1

    # Single-book mode
    raw_file = raw_files[0]

    if args.dry_run:
        template = detect_template_type(raw_file, config.raw_root, args.type)
        hs = file_sha256(raw_file)
        print(f"DRY RUN: would process {raw_file}")
        print(f"  hash: {hs}")
        print(f"  template: {template}")
        # Estimate cost
        if raw_file.suffix.lower() == ".pdf":
            try:
                import fitz
                doc = fitz.open(raw_file)
                pages = len(doc)
                doc.close()
                _pdf_type, avg_chars = _stage_1_1_detect_pdf_type(raw_file)
                mineru_chunks = max(1, (pages + 49) // 50)  # MINERU_CHUNK_SIZE = 50 pages
                print(f"  PDF: {pages} pages, avg {avg_chars:.0f} chars/page (sampled)")
                print(f"  minerU extraction: ~{mineru_chunks} chunk(s) (50 pages/chunk, hybrid-engine)")
                est_chars = int(max(avg_chars, 200)) * pages  # floor at 200 chars/page
                chunks_est = max(1, (est_chars + config.target_chars - 1) // config.target_chars)
                print(f"  Estimated text: ~{est_chars:,} chars ({pages} pages × {max(avg_chars, 200):.0f} chars/page)")
                print(f"  Estimated API calls: 1 (Stage 2.1) + {chunks_est} (Stage 2.2 chunks) + 1-3 (Stage 2.4)")
            except Exception:
                pass
        print(f"  Stages: text-extract -> image-extract+caption -> digest -> chunk -> generate -> review -> inject -> write -> cache")
        return 0

    h = file_sha256(raw_file)
    lock = ProjectLock(config, owner_id=h[-8:])
    if not lock.acquire():
        print("ERROR: Could not acquire project lock — another ingest may be running", file=sys.stderr)
        return 1
    try:
        result = ingest_one(raw_file, config, args.type, verbose=args.verbose)
        print(f"\nResult: {result}")
        return 0 if result["status"] in ("ok", "skipped") else 1
    except ConversationPending:
        return 101
    except Exception:
        lock.release()
        raise
    else:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())

