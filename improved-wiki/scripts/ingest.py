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
                            one VLM call per image (NashSU parity)
                            CAPTION_MAX_WORKERS=12 parallel caption concurrency
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
    CAPTION_MAX_WORKERS,
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
from _stage_3_7_embed import stage_3_7_embed_new_pages
from _stage_4_1_validate import stage_4_1_validate_ingest
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

# ── Ingest orchestration helpers (refactored 2026-06-24: extracted from ingest.py) ──
from _ingest_skip import _should_stop_after
from _ingest_prepare import _do_prepare
from _ingest_write import _do_write, _run_post_ingest_graph, cleanup_resolved_reviews

# ═════════════════════════════════════════════════════════
# Main pipeline — ingest_one, batch, queue, CLI
# ═════════════════════════════════════════════════════════

def ingest_one(
    raw_file: Path,
    config: Config,
    template_override: str | None = None,
    verbose: bool = False,
) -> dict:
    """Process one file end-to-end (NashSU-style multi-stage pipeline with checkpoint/resume)."""
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
    parser = argparse.ArgumentParser(description="Ingest source files into the wiki (NashSU-style multi-stage)")
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

