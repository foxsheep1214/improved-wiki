"""Command-line interface for improved-wiki ingestion."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _config import Config
from _exit_codes import (
    BATCH_PAUSED,
    COORDINATOR_BUSY,
    ERROR,
    HANDOFF_PENDING,
    OK,
    PREFETCH_PAUSED,
    SPINE_CONFLICT,
    USAGE,
)
from _core import (
    BATCH_MAX_CONCURRENT,
    ConversationPending,
    PrepareStopAfter,
    detect_template_type,
)
from _progress import ProjectLock, file_sha256, is_stage_done
from _batch_coordination import (
    BatchCoordinatorBusy,
    SpineReservationConflict,
    clear_prefetch_pause_marker,
    load_spine_reservation,
    refresh_spine_reservation,
    release_spine_reservation,
    reserve_spine,
    write_prefetch_pause_marker,
)
from _batch_status import _print_batch_status
from _batch_supervisor import (
    BatchPaused,
    BatchPrefetchPaused,
    _batch_pause_path,
    _clear_batch_pause_marker,
    _load_bg_state,
    _pause_batch_workers,
    _run_background_extract_worker,
    batch_ingest,
)
from _context_probe import resolve_context
from _ingest_prepare import _do_prepare
from _ingest_runner import _is_ingestable_source_path, ingest_one
from _source_filter import is_sensitive_config_source_file
from _stage_1_extract import _stage_1_1_detect_pdf_type
from _stage_1_1_scanned import MINERU_CHUNK_SIZE
from _watch import ingest_watch

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
                        help=f"Pipeline concurrency ceiling (default: {BATCH_MAX_CONCURRENT}). "
                             "The OS prefetch pipeline uses at most 2 workers "
                             "(1 minerU + 1 caption stage). Stage 2.4 is one "
                             "consolidated whole-source handoff.")
    parser.add_argument("--dry-run", action="store_true", help="Don't write anything")
    parser.add_argument("--delete", action="store_true",
                        help="Delete source: remove source page, cache entry, and cleanup orphans (NashSU source-lifecycle parity)")
    parser.add_argument("--keep-media", action="store_true",
                        help="With --delete: keep wiki/media/<slug>/ (images+captions) instead of removing it. "
                             "Use for an analysis-only re-ingest that reuses existing OCR/images/captions — "
                             "ask the user before choosing this vs. a full redo (see references/re-ingest-comparison.md).")
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
        choices=["0", "1.5", "2", "2.0"],
        help="Stop pipeline after completing the named stage (clean exit, cache saved). "
             "Use for chunked runs to avoid Bash timeout. "
             "Stops: 0=Phase 1 done (extract+images+captions), 1.5=Stage 2.2 chunk "
             "analysis done (prefetch boundary), 2/2.0=generation done (before write). "
             "(Legacy ids 0.5/0.6/1/2.3/2.5/3/… retired — their check sites are gone.)",
    )
    parser.add_argument(
        "--no-project-lock", action="store_true",
        help="Skip ProjectLock for an explicit single-source read-only prefetch. "
             "Internal detached workers use a dedicated hidden mode instead.",
    )
    parser.add_argument(
        "--pause-batch", action="store_true",
        help="Create .llm-wiki/batch.pause and stop verified detached extraction "
             "process groups. Progress remains cached.",
    )
    parser.add_argument(
        "--resume-batch", action="store_true",
        help="With a multi-source batch, clear full/prefetch pause markers and "
             "resume from cache.",
    )
    parser.add_argument(
        "--pause-prefetch", "--pause-batch-ocr",
        dest="pause_prefetch",
        action="store_true",
        help="Pause only detached OCR/caption prefetch and stop its verified "
             "workers. Already-extracted books may still advance.",
    )
    parser.add_argument(
        "--resume-prefetch", "--resume-batch-ocr",
        dest="resume_prefetch",
        action="store_true",
        help="Clear only the OCR/caption prefetch pause marker. May be used "
             "standalone or with --watch/a source list.",
    )
    parser.add_argument(
        "--batch-status", action="store_true",
        help="Print pause, worker, serial-spine, unfinished-source, and handoff "
             "state without starting ingestion.",
    )
    parser.add_argument(
        "--abandon-spine",
        metavar="HASH_OR_SUFFIX",
        help="Explicitly release a failed/stopped serial-spine reservation. "
             "The value must match the current owner's full hash or shown "
             "8-character suffix; inspect --batch-status first.",
    )
    parser.add_argument(
        "--batch-extract-worker", action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--batch-worker-token",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--batch-worker-status",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--reprobe", action="store_true",
        help="Force a fresh context-window probe: clear BOTH cache layers "
             "(probed-context.json + conversation/ctxprobe*) and exit. The next "
             "ingest then probes the live model once. Deleting probed-context.json "
             "alone does NOT re-probe — the conversation router replays the old answer.",
    )
    args = parser.parse_args()

    if args.parallel < 0:
        parser.error("--parallel must be >= 0")
    if args.batch_status and args.file:
        parser.error("--batch-status is standalone; omit source files")
    if args.abandon_spine and args.file:
        parser.error("--abandon-spine is standalone; omit source files")

    # ── First-class batch controls: no source list/context probe required ──
    if args.batch_status:
        _print_batch_status(Config.from_env())
        return OK

    if args.abandon_spine:
        config = Config.from_env()
        maintenance_lock = ProjectLock(
            config, owner_id="maintenance:abandon-spine")
        if not maintenance_lock.acquire():
            print(
                "ERROR: cannot abandon the spine while an active writer holds "
                ".llm-wiki/ingest.lock.",
                file=sys.stderr,
            )
            return ERROR
        try:
            try:
                reservation = load_spine_reservation(config)
            except RuntimeError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return ERROR
            if not reservation:
                print("[batch] serial spine is already free")
                return OK
            owner = str(reservation.get("source_hash") or "")
            supplied = args.abandon_spine.strip()
            if not owner or supplied not in {owner, owner[-8:]}:
                print(
                    "ERROR: reservation owner does not match "
                    f"{supplied!r}; current owner suffix is {owner[-8:]!r}.",
                    file=sys.stderr,
                )
                return USAGE
            release_spine_reservation(config, owner)
            print(
                f"[batch] abandoned serial-spine reservation {owner[-8:]}. "
                "Only continue with another source after checking any partial "
                "wiki writes from the abandoned source."
            )
            return OK
        finally:
            maintenance_lock.release()

    if args.pause_batch:
        config = Config.from_env()
        config.runtime_dir.mkdir(parents=True, exist_ok=True)
        bg_state = _load_bg_state(config)
        stopped = _pause_batch_workers(
            config,
            bg_state,
            "paused by --pause-batch",
        )
        print(f"[batch] paused; signalled {stopped} verified background "
              f"worker group(s). Re-run the full file list with --resume-batch.")
        return OK

    if args.pause_prefetch:
        config = Config.from_env()
        config.runtime_dir.mkdir(parents=True, exist_ok=True)
        write_prefetch_pause_marker(
            config, "paused by --pause-prefetch")
        bg_state = _load_bg_state(config)
        stopped = _pause_batch_workers(
            config,
            bg_state,
            "paused by --pause-prefetch",
            write_marker=False,
        )
        print(
            f"[batch] OCR/caption prefetch paused; signalled {stopped} "
            "verified background worker group(s). Already-extracted books "
            "remain eligible for the serial spine."
        )
        return OK

    if args.resume_prefetch:
        config = Config.from_env()
        clear_prefetch_pause_marker(config)
        print("[batch] OCR/caption prefetch pause marker cleared")
        if not args.file and not args.watch:
            return OK

    # ── Force-reprobe: one-shot maintenance action (clear caches, exit) ──
    # Standalone like --delete so the handoff re-invocation never re-clears the
    # in-flight answer (which would loop). The subsequent normal ingest re-probes.
    if args.reprobe:
        from _context_probe import clear_probe_cache
        config = Config.from_env()
        clear_probe_cache(config)
        print("[context-probe] caches cleared (probed-context.json + conversation/ctxprobe*) "
              "— next ingest will probe the live model.")
        return OK

    # ── Watch mode: continuous queue consumer ──
    if args.watch:
        config = Config.from_env()
        config.enrich_enabled = args.enrich_wikilinks and not args.no_enrich
        config.runtime_dir.mkdir(parents=True, exist_ok=True)
        if args.resume_batch:
            _clear_batch_pause_marker(config)
            clear_prefetch_pause_marker(config)
            print("[watch] full/prefetch pause markers cleared — resuming queue")
        elif _batch_pause_path(config).exists():
            print("ERROR: watch batch is paused; restart with "
                  "--watch --resume-batch.", file=sys.stderr)
            return BATCH_PAUSED
        try:
            _probe_and_apply_context(config)
        except ConversationPending:
            return HANDOFF_PENDING
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
            return HANDOFF_PENDING
        except BatchPaused as pause:
            print(f"[watch] batch paused: {pause}", file=sys.stderr)
            return BATCH_PAUSED
        except BatchPrefetchPaused as pause:
            print(f"[watch] OCR/caption prefetch paused: {pause}",
                  file=sys.stderr)
            return PREFETCH_PAUSED
        except SpineReservationConflict as conflict:
            print(f"[watch] {conflict}", file=sys.stderr)
            return SPINE_CONFLICT
        except BatchCoordinatorBusy as busy:
            print(f"[watch] {busy}", file=sys.stderr)
            return COORDINATOR_BUSY
        return OK

    if not args.file:
        parser.print_help()
        print("\nTip: use --watch to process the queue, or pass file(s) for direct ingest.", file=sys.stderr)
        return ERROR

    # ── Source lifecycle: delete ──
    if args.delete:
        config = Config.from_env()
        from _source_lifecycle import delete_source
        for f in args.file:
            rf = Path(f).expanduser().resolve()
            delete_source(rf, config, dry_run=args.dry_run, keep_media=args.keep_media)
        return OK

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
            return ERROR
        # NashSU deep-research parity (2026-07-16): accept a wiki/queries/<page>
        # research page as an ingest source directly, no raw/queries/ copy step
        # (see _is_ingestable_source_path / is_query_bridge_source).
        if not _is_ingestable_source_path(rf, config):
            print(f"ERROR: {rf} is not under raw_root ({config.raw_root}) "
                  f"or wiki/queries/ ({config.wiki_dir / 'queries'})", file=sys.stderr)
            return ERROR
        if is_sensitive_config_source_file(rf):
            print(
                f"ERROR: {rf} is an agent/tool config file (under "
                f".claude/.codex/.cursor/.gemini/.mcp with a config extension) — "
                f"refusing to ingest to avoid leaking secrets. "
                f"Move it out of the config dir or rename to a non-config extension.",
                file=sys.stderr,
            )
            return ERROR
        raw_files.append(rf)

    # Internal detached Phase-1 worker. The random token and status path are
    # generated by the coordinator; users should never invoke this directly.
    if args.batch_extract_worker:
        if len(raw_files) != 1 or not args.batch_worker_token or not args.batch_worker_status:
            print("ERROR: invalid internal batch worker invocation", file=sys.stderr)
            return USAGE
        status_path = Path(args.batch_worker_status).expanduser().resolve()
        worker_root = (config.runtime_dir / "batch-workers").resolve()
        if not status_path.is_relative_to(worker_root):
            print(f"ERROR: worker status path must be under {worker_root}",
                  file=sys.stderr)
            return USAGE
        return _run_background_extract_worker(
            raw_files[0],
            config,
            status_path,
            args.batch_worker_token,
            args.type,
            args.verbose,
        )

    is_batch = len(raw_files) > 1
    if is_batch and args.stop_after_stage is not None:
        print("ERROR: --stop-after-stage is single-source only. Batch Phase-1 "
              "prefetch is automatic; use normal batch mode, or run one explicit "
              "source for diagnostic staging.", file=sys.stderr)
        return USAGE
    if is_batch and args.no_project_lock:
        print("ERROR: --no-project-lock is single-source only; batch mode manages "
              "its own Phase-1 workers and spine locks.", file=sys.stderr)
        return USAGE
    if (args.no_project_lock
            and args.stop_after_stage not in {"0", "1.5"}):
        print(
            "ERROR: --no-project-lock is limited to read-only prefetch "
            "(--stop-after-stage 0 or 1.5).",
            file=sys.stderr,
        )
        return USAGE
    if args.resume_batch and not is_batch:
        print("ERROR: --resume-batch requires the complete multi-source file list.",
              file=sys.stderr)
        return USAGE
    if is_batch and args.resume_batch:
        _clear_batch_pause_marker(config)
        clear_prefetch_pause_marker(config)
        print("[batch] full/prefetch pause markers cleared — "
              "resuming from cached progress")
    elif _batch_pause_path(config).exists() and not args.dry_run:
        scope = "batch" if is_batch else "project ingest"
        print(
            f"ERROR: {scope} is paused by .llm-wiki/batch.pause. "
            "A single-source command cannot bypass a full pause. "
            "Resume the confirmed batch with --resume-batch.",
            file=sys.stderr,
        )
        return BATCH_PAUSED

    if args.no_project_lock:
        # Explicit single-source read-only prefetch: do not emit a context probe
        # handoff that an unattended caller may never answer.
        from _context_probe import load_cached
        if load_cached(config) is None:
            print("ERROR: context-probe cache miss with --no-project-lock; "
                  "run a normal foreground ingest once first.", file=sys.stderr)
            return ERROR

    try:
        _probe_and_apply_context(config)
    except ConversationPending:
        return HANDOFF_PENDING


    # Batch mode: multiple files.
    if is_batch:
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
            return HANDOFF_PENDING
        except BatchPaused as pause:
            print(f"[batch] paused: {pause}", file=sys.stderr)
            return BATCH_PAUSED
        except BatchPrefetchPaused as pause:
            print(f"[batch] OCR/caption prefetch paused: {pause}",
                  file=sys.stderr)
            return PREFETCH_PAUSED
        except SpineReservationConflict as conflict:
            print(f"[batch] {conflict}", file=sys.stderr)
            return SPINE_CONFLICT
        except BatchCoordinatorBusy as busy:
            print(f"[batch] {busy}", file=sys.stderr)
            return COORDINATOR_BUSY
        ok = sum(1 for r in results if r.get("status") in ("ok", "skipped"))
        return OK if ok == len(raw_files) else ERROR

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
                mineru_chunks = max(1, (pages + MINERU_CHUNK_SIZE - 1) // MINERU_CHUNK_SIZE)
                print(f"  PDF: {pages} pages, avg {avg_chars:.0f} chars/page (sampled)")
                print(f"  minerU extraction: ~{mineru_chunks} chunk(s) ({MINERU_CHUNK_SIZE} pages/chunk, hybrid-engine)")
                est_chars = int(max(avg_chars, 200)) * pages  # floor at 200 chars/page
                chunks_est = max(1, (est_chars + config.target_chars - 1) // config.target_chars)
                print(f"  Estimated text: ~{est_chars:,} chars ({pages} pages × {max(avg_chars, 200):.0f} chars/page)")
                print(
                    "  Estimated text-generation calls: "
                    f"{chunks_est} (Stage 2.2 chunks) + up to 1 "
                    "(Stage 2.4 consolidated) + 1 (Stage 2.6 source page), "
                    "plus optional dedup/review/merge handoffs"
                )
            except Exception:
                pass
        print(f"  Stages: text-extract -> image-extract+caption -> chunk+analyze -> generate -> review -> inject -> write -> cache")
        return OK

    h = file_sha256(raw_file)
    if args.no_project_lock:
        # Explicit single-source read-only prefetch. Internal detached workers
        # use --batch-extract-worker and never route through this branch.
        try:
            result = ingest_one(raw_file, config, args.type, verbose=args.verbose)
            print(f"\nResult: {result}")
            return OK if result["status"] in ("ok", "skipped") else ERROR
        except ConversationPending:
            return HANDOFF_PENDING

    # Single-source work uses the same lock boundary as batch mode: Phase 1 and
    # Stage 2.2 are source-local and may yield handoffs without monopolizing the
    # project lock. Only the wiki-dependent Stage 2.3+ spine is serialized.
    config.conversation_prefix = h[-8:]
    try:
        prefetched = _do_prepare(
            raw_file,
            config,
            args.type,
            args.verbose,
            True,
        )
    except PrepareStopAfter as stop:
        if args.stop_after_stage in {"0", "1.5"}:
            result = {"status": "ok", "stopped_after": stop.stage}
            print(f"\nResult: {result}")
            return OK
    except ConversationPending:
        return HANDOFF_PENDING
    else:
        if prefetched is None:
            result = {"status": "skipped", "reason": "source-page-exists"}
            print(f"\nResult: {result}")
            return OK
        if args.stop_after_stage in {"0", "1.5"}:
            result = {
                "status": "ok",
                "stopped_after": args.stop_after_stage,
            }
            print(f"\nResult: {result}")
            return OK

    lock = ProjectLock(config, owner_id=h[-8:])
    if not lock.acquire():
        print("ERROR: Could not acquire project lock — another ingest may be running", file=sys.stderr)
        return ERROR
    spine_reserved = False
    try:
        reserve_spine(
            config,
            h,
            raw_file,
            phase="stage_2_3_plus",
        )
        spine_reserved = True
        result = ingest_one(raw_file, config, args.type, verbose=args.verbose)
        completed = (
            result.get("status") == "skipped"
            or is_stage_done(config, h, "ingested")
        )
        if completed:
            release_spine_reservation(config, h)
        elif result.get("stopped_after"):
            refresh_spine_reservation(
                config,
                h,
                phase=f"stopped_after_{result['stopped_after']}",
            )
        else:
            refresh_spine_reservation(config, h, phase="failed")
        print(f"\nResult: {result}")
        return OK if result["status"] in ("ok", "skipped") else ERROR
    except ConversationPending:
        if spine_reserved:
            refresh_spine_reservation(
                config, h, phase="waiting_handoff")
        return HANDOFF_PENDING
    except SpineReservationConflict as conflict:
        print(f"ERROR: {conflict}", file=sys.stderr)
        return SPINE_CONFLICT
    except Exception:
        if spine_reserved:
            refresh_spine_reservation(config, h, phase="failed")
        raise
    finally:
        lock.release()
