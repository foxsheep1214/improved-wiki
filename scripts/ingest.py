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
  ~/.agents/config.json   provider and caption config
  LLM_PROVIDER            override provider name (env var)
  LLM_API_KEY             override API key (env var)
  LLM_BASE_URL            override base URL (env var)
  LLM_MODEL               override model name (env var)
  LLM_CHUNK_RETRIES       extra attempts per failed chunk (default 2 → 3 total)
  Text LLM:               config.json default provider (DeepSeek V4 Pro via OpenAI protocol)
  Image caption:          config.json caption_provider
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
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

# ── Imports from split stage modules (refactored 2026-06-18) ──
from _core import (
    Config, ConversationPending, PrepareStopAfter,
    set_current_file as _set_current_file,
    get_current_file as _get_current_file,
    file_tag as _file_tag,
    stage_begin as _stage_begin,
    stage_end as _stage_end,
    heartbeat as _heartbeat,
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
from _source_filter import is_sensitive_config_source_file
from _stage_3_write import (
    stage_3_1_write_wiki_file, stage_3_5_aggregate_repair,
    _stage_3_1_canonicalize_sources_field, _stage_3_1_stamp_frontmatter_dates,
    _stage_3_1_auto_correct_wiki_path, _stage_3_1_contains_cjk, _stage_3_1_make_cjk_slug,
    _stage_3_1_backup_existing_page,
)
from _stage_3_2_inject_images import stage_3_2_inject_images
from _stage_3_7_embed import stage_3_7_embed_new_pages
from _watch import ingest_watch
from _stage_validators import (verify_stage_0, StageValidationError, _verify_or_die, _verify_stage_1_1_text, _verify_stage_2_1_digest, _verify_stage_2_2_chunks, _verify_stage_2_4_file_blocks, validate_stage_outputs)


# Use shared runtime detection (matches all other scripts)
_script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_script_dir))
from _paths import detect_runtime_dir  # noqa: E402
from _conversation_router import (  # noqa: E402,F401  (import side-effect: registers conversation router)
    call_anthropic_protocol,
    _load_task_manifest,
)
from _context_probe import resolve_context  # noqa: E402  (live context-window probe)

# ── Ingest orchestration helpers (refactored 2026-06-24: extracted from ingest.py) ──
from _ingest_skip import _should_stop_after
from _ingest_prepare import _do_prepare
from _ingest_write import _do_write

# ═════════════════════════════════════════════════════════
# Main pipeline — ingest_one, batch, queue, CLI
# ═════════════════════════════════════════════════════════

def _bridge_wiki_queries_to_raw(rf: Path, config: Config) -> Path:
    """Accept a ``wiki/queries/<page>`` deep-research page as an ingest source.

    NashSU ``deep-research.ts`` writes the research page to ``wiki/queries/``
    and hands its absolute path straight to ``autoIngest``, which is
    path-agnostic (``sourceIdentityForPath`` falls back to the filename for
    non-raw paths). The improved-wiki pipeline derives source identity from a
    ``raw/`` path in ~20 places (``relative_to(config.raw_root)``), so a pure
    gate-relax would crash on the first stage. This bridge copies the research
    page into ``raw/queries/<same-rel-path>`` and returns the copy — the rest
    of the pipeline then sees a normal raw source.

    The original ``wiki/queries/`` page stays as the human-readable research
    artifact (NashSU keeps it too); ``raw/queries/<name>.md`` is the source of
    record for this ingest. Idempotent: a same-name copy is overwritten so a
    re-ingest is a clean redo. No-op for paths not under ``wiki/queries/``.
    """
    queries_dir = config.wiki_dir / "queries"
    try:
        rel = rf.relative_to(queries_dir)
    except ValueError:
        return rf
    dest_dir = config.raw_root / "queries"
    dest = dest_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(rf, dest)
    print(f"[ingest] deep-research bridge: wiki/queries/{rel} -> raw/queries/{rel}")
    return dest


def _finalize_book(raw_file: Path, config: Config,
                   files_written: list, source_hash: str) -> None:
    """Per-book post-write finalization shared by the single-book and batch paths.

    Runs Stage 3.7 (embeddings) → sets the ``ingested`` completion marker.

    The dedicated post-ingest validation audit (formerly "Stage 4.1", running
    validate_ingest.py) was REMOVED for NashSU alignment: NashSU has no
    post-ingest verification stage. NashSU's only ingest-time check is schema
    routing (``validateWikiPageRouting``), which improved-wiki already performs
    where NashSU does — at WRITE time in Stage 3.1
    (``_stage_3_1_auto_correct_wiki_path``) — so it is preserved automatically.
    The completion marker is named ``ingested`` (renamed from the legacy
    ``stage_4_1`` key on 2026-07-08: the old name implied a Stage 4.1 that no
    longer exists; existing stages.json files were migrated in lockstep so
    already-ingested books stay recognized as complete). ``_stage_0_2_should_skip``
    reads this marker as the single completeness signal. ``validate_ingest.py``
    remains as a standalone manual tool; it is just no longer auto-run by ingest.

    This finalization used to live ONLY in ingest_one, so batch_ingest — and the
    ``--watch`` queue daemon, which routes through batch_ingest — silently
    skipped embeddings and never set the completion marker, leaving every
    batch-ingested book perpetually "mid-flight" in _stage_0_2_should_skip.

    Embeddings stay mandatory / no-fallback here too: a missing Ollama stack
    raises (pauses this book, and in batch propagates to abort the run) rather
    than silently degrading to keyword-only retrieval (policy 2026-06-24).
    Graph rebuild is intentionally NOT here and never triggered by ingest —
    the graph is a separate explicit command (NashSU-aligned: NashSU has no
    post-ingest graph rebuild). Run ``python3 scripts/graph.py`` manually.
    """
    stage_3_7_embed_new_pages(config, files_written)
    mark_stage_done(config, source_hash, "ingested")


def ingest_one(
    raw_file: Path,
    config: Config,
    template_override: str | None = None,
    verbose: bool = False,
) -> dict:
    """Process one file end-to-end (NashSU-style multi-stage pipeline with checkpoint/resume)."""
    _set_current_file(raw_file.name)
    print(f"\n=== Ingest: {raw_file} ===")

    # NashSU parity: resolved review pages are KEPT (never auto-deleted) so the
    # content-stable review_id + resolved-wins dedup keeps them resolved across
    # re-ingest. (Previously cleanup_resolved_reviews() deleted them here, which
    # destroyed the resolved twins that dedup relies on.)

    # 1. Dedup + Stage 0-2 (delegated to shared implementation)
    h = file_sha256(raw_file)
    config.conversation_prefix = h[-8:]  # per-source conversation file isolation
    task_manifest = _load_task_manifest(config)
    pending_tasks = task_manifest.get("pending", [])
    if pending_tasks:
        print(f"[conversation] {len(pending_tasks)} pending task(s) — resuming pipeline")

    # Stage-completion markers (Option A) drive resume semantics: the skip-check
    # only short-circuits once the ``ingested`` marker is set, so a mid-flight resume (pages
    # written but post-review stages pending) is never dropped.  _do_write in
    # turn skips the non-idempotent 3.1 write loop when `write_phase` is marked.
    try:
        prepared = _do_prepare(raw_file, config, template_override, verbose)
    except PrepareStopAfter as stop:
        # A Stage-0..2 boundary matched --stop-after-stage inside _do_prepare.
        # Convert the control-flow signal to a clean ok return; the caller
        # (main) exits 0. Extraction/digest/generation artifacts are already
        # persisted, so re-running without the flag resumes from the completed
        # stage.
        print(f"\n[stop-after-stage] Stage {stop.stage} complete — "
              f"clean exit (--stop-after-stage={stop.stage})")
        return {"status": "ok", "stopped_after": stop.stage}
    if prepared is None:
        return {"status": "skipped", "reason": "source-page-exists"}

    # Unpack prepared state from Stage 0-2
    method = prepared["method"]
    extracted_text = prepared["extracted_text"]
    global_digest = prepared["global_digest"]
    chunk_analyses = prepared["chunk_analyses"]
    analysis = prepared["analysis"]
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
        "file_blocks": file_blocks,
        "stage_1_2_result": stage_1_2_result, "stage_1_3_result": stage_1_3_result,
        "template_name": template_name,
        "enrich_enabled": getattr(config, "enrich_enabled", True),
    }
    result = _do_write(prepared, verbose=verbose)
    if result["status"] != "ok":
        return result

    files_written = result["files_written"]

    # Embeddings + completion marker (shared with batch path).
    _finalize_book(raw_file, config, files_written, h)

    return {"status": "ok", "files_written": files_written}

# ═══════════════════════════════════════════════════════════════
# Batch ingest: parallel Stage 0-2, serial Stage 3+
# ═══════════════════════════════════════════════════════════════
def _bg_state_path(config: Config) -> Path:
    return config.runtime_dir / "batch-bg.json"


def _load_bg_state(config: Config) -> dict:
    try:
        return json.loads(_bg_state_path(config).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_bg_state(config: Config, state: dict) -> None:
    try:
        _bg_state_path(config).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    """True if `pid` is an alive process (os.kill probe)."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _launch_bg_extract(file: Path, config: Config, state: dict) -> None:
    """Launch a DETACHED background subprocess to run Phase 0/1 (minerU + caption)
    for ``file``. start_new_session makes it survive the batch's ConversationPending
    exits, so the slow non-LLM extraction of book N+1 overlaps with the current
    book's LLM spine. ``--no-project-lock`` avoids deadlocking on the lock the
    batch already holds (Phase 0/1 never touches wiki/)."""
    h = file_sha256(file)
    if h in state and _pid_alive(state[h].get("pid", 0)):
        return  # already running
    # stale entry (dead pid) or new — (re)launch
    log_path = config.runtime_dir / f"bg-extract-{h[:8]}.log"
    cmd = [sys.executable, str(_script_dir / "ingest.py"),
           "--stop-after-stage", "1", "--no-project-lock", str(file)]
    try:
        log = open(log_path, "w", encoding="utf-8")
    except OSError:
        log = subprocess.DEVNULL
    proc = subprocess.Popen(cmd, cwd=str(config.wiki_root),
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    state[h] = {"pid": proc.pid, "file": file.name}
    _save_bg_state(config, state)
    print(f"[batch] bg extract launched (pid {proc.pid}) — {file.name}", flush=True)


def _wait_extract_done(config: Config, h: str, timeout: int = 7200) -> bool:
    """Block until Phase 0/1 (stage_1_3_done) is cached for this book. The bg
    subprocess does the extraction; this just polls the stage marker."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_stage_done(config, h, "stage_1_3_done"):
            return True
        time.sleep(5)
    return is_stage_done(config, h, "stage_1_3_done")


def batch_ingest(
    raw_files: list[Path],
    config: Config,
    max_concurrent: int = BATCH_MAX_CONCURRENT,
    template_override: str | None = None,
    verbose: bool = False,
) -> list[dict]:
    """Pipeline batch ingest: book N's LLM work (2.1/2.2 + spine) overlaps with
    book N+1's minerU extraction (Phase 0/1).

    Design (pipeline, not barrier):
      - The slow non-LLM part of EVERY book (Phase 0/1 = minerU + caption) runs in
        a detached background subprocess: the spine head (book 1) is launched at
        batch start so it wins the flock; each later book is launched right after
        the prior book's extraction (pipelined), so they extract in spine order
        and overlap the prior book's LLM spine instead of racing for the flock.
      - The main conversation drives books ONE AT A TIME: wait for book N's
        Phase 0/1 (bg) → 2.1/2.2 (LLM handoffs) → 2.3+ spine (LLM handoffs).
        So minerU[N+1] runs while spine[N] is being answered.

    Cross-book PARALLELISM of wiki-dependent stages (2.3+) is NOT allowed: each
    book's spine runs fully before the next book's 2.3, so dedup/linking sees
    prior pages. Only the wiki-independent minerU overlaps (safe — never touches
    wiki/). (The prior "barrier all prefetch, then all spine" design made book 1's
    spine wait for book N's minerU — fixed 2026-06-28.)

    Conversation mode: each LLM handoff re-raises ConversationPending (exit 101);
    the bg subprocesses are detached so they keep running across re-invokes, and
    per-book stage-progress cache makes the loop resume cleanly.
    """
    total_books = len(raw_files)
    print(f"\n{'='*60}")
    print(f"Batch ingest (pipeline): {total_books} books — minerU[N+1] ∥ spine[N]")
    print(f"{'='*60}")

    lock = ProjectLock(config, owner_id="batch")
    if not lock.acquire():
        raise RuntimeError("Could not acquire project lock for batch write phase")

    bg_state = _load_bg_state(config)
    # Launch ONLY the spine head's (book 1) bg extract upfront so it wins the
    # minerU flock and the spine can start ASAP. Later books are launched
    # PIPELINED — each right after the prior book's extraction completes (see the
    # loop) — so they extract in spine order. Launching all upfront let them race
    # for the flock: a non-head book could grab it first and delay book 1's spine
    # (observed 2026-06-29). _launch_bg_extract is idempotent (skips alive PIDs),
    # so re-invocations after a handoff don't relaunch.
    if raw_files and not is_stage_done(config, file_sha256(raw_files[0]), "stage_1_3_done"):
        _launch_bg_extract(raw_files[0], config, bg_state)

    results: list[dict] = []
    try:
        for i, f in enumerate(raw_files, 1):
            h = file_sha256(f)
            print(f"\n[batch] book {i}/{total_books} — {f.name}", flush=True)

            # Wait for this book's Phase 0/1 (bg extract). For book 1 this is the
            # initial minerU wait; for later books it should already be done
            # (overlapped with the prior book's spine).
            if not is_stage_done(config, h, "stage_1_3_done"):
                print(f"[batch] waiting for bg extract (Phase 0/1) — {f.name}", flush=True)
                if not _wait_extract_done(config, h):
                    print(f"[batch] bg extract timed out — falling back to sync — {f.name}", flush=True)

            # Pipeline: this book's Phase 0/1 is done — launch the NEXT book's bg
            # extract now so it runs (flock-serialized, in spine order) during this
            # book's LLM spine. Launched AFTER this book's extraction so ordering is
            # strict (no flock race). Idempotent: skips if already done/alive.
            if i < total_books:
                nxt = raw_files[i]  # i is 1-based → raw_files[i] is book i+1
                if not is_stage_done(config, file_sha256(nxt), "stage_1_3_done"):
                    _launch_bg_extract(nxt, config, bg_state)

            # 2.1/2.2 (Phase 0/1 cached). Raises ConversationPending on an LLM
            # handoff (chunk prompt); PrepareStopAfter at the 2.2/2.3 boundary.
            try:
                _do_prepare(f, config, template_override, verbose, True)
            except PrepareStopAfter:
                pass  # reached 2.2/2.3 boundary — 2.2 cached, ready for spine
            except ConversationPending:
                raise  # LLM handoff in 2.1/2.2 — agent answers + re-invokes

            # Spine: 2.3+ (Phase 0/1/2.1/2.2 cached). Wiki-dependent, strictly serial.
            prepared = _do_prepare(f, config, template_override, verbose)
            if prepared is None:
                print(f"[batch] {i}/{total_books} skipped (already complete) — {f.name}", flush=True)
                continue
            try:
                result = _do_write(prepared, verbose=verbose)
            except ConversationPending:
                raise
            except Exception as e:
                print(f"[batch] {i}/{total_books} FAILED for {f.name}: {e}", flush=True)
                traceback.print_exc()
                continue
            results.append(result)
            # Per-book finalization (embeddings + completion marker).
            if result.get("status") == "ok":
                _finalize_book(prepared["raw_file"], config,
                               result.get("files_written", []), prepared["h"])

        # Drop bg-state entries for books whose extraction has finished.
        for f in raw_files:
            if is_stage_done(config, file_sha256(f), "stage_1_3_done"):
                bg_state.pop(file_sha256(f), None)
        _save_bg_state(config, bg_state)
    finally:
        lock.release()

    ok = sum(1 for r in results if r.get("status") == "ok")
    print(f"\n{'='*60}")
    print(f"Batch complete: {ok}/{len(results)} books processed successfully")
    print(f"{'='*60}")

    return results

# ---------- CLI ----------

def _probe_and_apply_context(config) -> None:
    """Probe the live conversation model's context window (or reuse cache) and
    apply it to ``config``. Raises ``ConversationPending`` on the first pass
    (normal handoff); the caller returns 101 so the agent answers and re-invokes.
    Delete-only paths never call this."""
    config.apply_context(resolve_context(config))


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
    parser.add_argument(
        "--no-project-lock", action="store_true",
        help="Skip the ProjectLock acquire (for background extract subprocesses that "
             "only do Phase 0/1 — minerU+caption — and never touch wiki/). The batch "
             "coordinator holds the lock; a bg extract must not deadlock on it.",
    )
    parser.add_argument(
        "--reprobe", action="store_true",
        help="Force a fresh context-window probe: clear BOTH cache layers "
             "(probed-context.json + conversation/ctxprobe*) and exit. The next "
             "ingest then probes the live model once. Deleting probed-context.json "
             "alone does NOT re-probe — the conversation router replays the old answer.",
    )
    args = parser.parse_args()

    # ── Force-reprobe: one-shot maintenance action (clear caches, exit) ──
    # Standalone like --delete so the handoff re-invocation never re-clears the
    # in-flight answer (which would loop). The subsequent normal ingest re-probes.
    if args.reprobe:
        from _context_probe import clear_probe_cache
        config = Config.from_env()
        clear_probe_cache(config)
        print("[context-probe] caches cleared (probed-context.json + conversation/ctxprobe*) "
              "— next ingest will probe the live model.")
        return 0

    # ── Watch mode: continuous queue consumer ──
    if args.watch:
        config = Config.from_env()
        config.enrich_enabled = args.enrich_wikilinks and not args.no_enrich
        config.runtime_dir.mkdir(parents=True, exist_ok=True)
        try:
            _probe_and_apply_context(config)
        except ConversationPending:
            return 101
        max_conc = args.parallel if args.parallel > 0 else BATCH_MAX_CONCURRENT
        try:
            ingest_watch(
                config,
                poll_interval=args.poll_interval,
                drain=args.drain,
                max_concurrent=max_conc,
                max_retries=args.max_retries,
                verbose=args.verbose,
            )
        except ConversationPending:
            # A wave paused at an LLM handoff — answer the prompt and re-invoke
            # --watch to resume from cache (same contract as direct ingest).
            return 101
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
            delete_source(rf, config, dry_run=args.dry_run)
        return 0

    config = Config.from_env()
    config.enrich_enabled = args.enrich_wikilinks and not args.no_enrich
    config.stop_after_stage = args.stop_after_stage

    # Validate raw files BEFORE probing context. A wrong cwd / missing file must
    # error immediately instead of triggering a fresh context-probe handoff —
    # otherwise the probe (which runs before this check) caches into the wrong
    # project's .llm-wiki and the actual file-not-found is never reached.
    raw_files = []
    for f in args.file:
        rf = Path(f).expanduser().resolve()
        if not rf.exists():
            print(f"ERROR: {rf} not found", file=sys.stderr)
            return 1
        # NashSU deep-research parity: accept a wiki/queries/<page> research page
        # as an ingest source by bridging it into raw/queries/ (see
        # _bridge_wiki_queries_to_raw). NashSU's autoIngest is path-agnostic; the
        # improved-wiki pipeline derives source identity from a raw/ path in ~20
        # places, so we copy instead of refactoring all of them. No-op for normal
        # raw/ inputs.
        rf = _bridge_wiki_queries_to_raw(rf, config)
        if not rf.is_relative_to(config.raw_root):
            print(f"ERROR: {rf} is not under raw_root ({config.raw_root})", file=sys.stderr)
            return 1
        if is_sensitive_config_source_file(rf):
            print(
                f"ERROR: {rf} is an agent/tool config file (under "
                f".claude/.codex/.cursor/.gemini/.mcp with a config extension) — "
                f"refusing to ingest to avoid leaking secrets. "
                f"Move it out of the config dir or rename to a non-config extension.",
                file=sys.stderr,
            )
            return 1
        raw_files.append(rf)

    try:
        _probe_and_apply_context(config)
    except ConversationPending:
        return 101


    # Batch mode: multiple files or explicit --parallel
    if len(raw_files) > 1 or args.parallel > 1:
        max_conc = args.parallel if args.parallel > 0 else BATCH_MAX_CONCURRENT
        try:
            results = batch_ingest(
                raw_files, config, max_concurrent=max_conc,
                template_override=args.type, verbose=args.verbose,
            )
        except ConversationPending:
            # Prefetch or spine paused at an LLM handoff (prompt written to disk).
            # The agent answers it and re-invokes to resume. Same contract as the
            # single-book path below.
            return 101
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
    if args.no_project_lock:
        # Background extract subprocess (Phase 0/1 only) — the batch coordinator
        # already holds the ProjectLock; this subprocess must not re-acquire it.
        try:
            result = ingest_one(raw_file, config, args.type, verbose=args.verbose)
            print(f"\nResult: {result}")
            return 0 if result["status"] in ("ok", "skipped") else 1
        except ConversationPending:
            return 101
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

