"""Single-source ingest runner and completion gate."""
from __future__ import annotations

from pathlib import Path

from _config import Config
from _core import PrepareStopAfter, set_current_file as _set_current_file
from _progress import file_sha256, mark_stage_done
from _conversation_router import _load_task_manifest
from _ingest_skip import _should_stop_after
from _ingest_prepare import _do_prepare
from _ingest_write import _do_write
from _stage_3_7_embed import stage_3_7_embed_new_pages
from _media_integrity import assert_cached_media_complete
from _task_manifest import assert_task_ready_for_completion

def _is_ingestable_source_path(rf: Path, config: Config) -> bool:
    """True for a normal ``raw/`` source, or a deep-research page under
    ``wiki/queries/`` (2026-07-16: ingested directly — see
    ``is_query_bridge_source``/deep-research.md; there is no longer a
    ``raw/queries/`` copy step, NashSU ``autoIngest`` path-agnostic parity)."""
    return rf.is_relative_to(config.raw_root) or rf.is_relative_to(config.wiki_dir / "queries")


def _finalize_book(raw_file: Path, config: Config,
                   files_written: list, source_hash: str) -> None:
    """Per-book post-write finalization shared by the single-book and batch paths.

    Runs Stage 3.7 (embeddings) → sets the ``ingested`` completion marker.

    The dedicated post-ingest validation audit (formerly "Stage 4.1", running
    validate_ingest.py) was REMOVED for NashSU alignment: NashSU has no
    post-ingest verification stage. NashSU's only ingest-time check is schema
    routing (``validateWikiPageRouting``), which improved-wiki already performs
    where NashSU does — at WRITE time in Stage 3.1
    (``_stage_3_2_auto_correct_wiki_path``) — so it is preserved automatically.
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
    # A cached counter is not completion evidence. Verify manifest, every
    # image hash, every required caption, and source-page injection immediately
    # before the authoritative marker is allowed to exist.
    assert_cached_media_complete(raw_file, config)
    canonical_files = assert_task_ready_for_completion(
        raw_file,
        config,
        files_written,
        source_hash,
    )
    stage_3_7_embed_new_pages(config, canonical_files)
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
    for stage_check in ("0", "1.5", "2.0", "2"):
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
        # Forward the no-cache signal: _do_write refuses to cache a run whose
        # source page degraded to the deterministic summary after an
        # unrecovered truncation, and its non-ok status keeps _finalize_book
        # (and the `ingested` marker) from running.
        "source_page_truncated": prepared.get("source_page_truncated", False),
    }
    result = _do_write(prepared, verbose=verbose)
    if result["status"] != "ok":
        return result

    files_written = result["files_written"]

    # Embeddings + completion marker (shared with batch path).
    _finalize_book(raw_file, config, files_written, h)

    return {"status": "ok", "files_written": files_written}
