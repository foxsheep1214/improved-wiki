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
  ingest.py f1.pdf f2.pdf ...              # batch: Phase-1 prefetch + serial spine
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
                            CAPTION_MAX_WORKERS=4 parallel caption concurrency
  Embeddings:             local Ollama (EMBEDDING_BASE_URL / EMBEDDING_MODEL)

This script is idempotent: if the source page exists for a file, it's skipped.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

# ── Imports from split stage modules (refactored 2026-06-18) ──
from _core import (
    Config, ConversationPending, PrepareStopAfter,
    set_current_file as _set_current_file,
    detect_template_type,
    file_sha256,
    mark_stage_done, is_stage_done,
    ProjectLock,
    BATCH_MAX_CONCURRENT,
)
from _paths import atomic_write
from _batch_worker_status import BatchWorkerReporter, worker_lease_path
from _batch_coordination import (
    BatchCoordinatorBusy,
    SpineReservationConflict,
    batch_coordinator_slot,
    clear_prefetch_pause_marker,
    is_prefetch_paused,
    load_spine_reservation,
    prefetch_pause_path,
    refresh_spine_reservation,
    release_spine_reservation,
    reserve_spine,
    write_prefetch_pause_marker,
)
from _stage_1_extract import _stage_1_1_detect_pdf_type
from _stage_1_1_scanned import MINERU_CHUNK_SIZE
from _source_filter import is_sensitive_config_source_file
from _stage_3_write import (
    _stage_3_1_auto_correct_wiki_path,  # noqa: F401  (referenced in _finalize_book docstring)
)
from _stage_3_7_embed import stage_3_7_embed_new_pages
from _media_integrity import assert_cached_media_complete
from _task_manifest import assert_task_ready_for_completion
from _watch import ingest_watch


_script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_script_dir))
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
    }
    result = _do_write(prepared, verbose=verbose)
    if result["status"] != "ok":
        return result

    files_written = result["files_written"]

    # Embeddings + completion marker (shared with batch path).
    _finalize_book(raw_file, config, files_written, h)

    return {"status": "ok", "files_written": files_written}

# ═══════════════════════════════════════════════════════════════
# Batch ingest: two-stage Phase-1 prefetch, serial Stage 2.3+ spine
# ═══════════════════════════════════════════════════════════════
def _bg_state_path(config: Config) -> Path:
    return config.runtime_dir / "batch-bg.json"


def _load_bg_state(config: Config) -> dict:
    p = _bg_state_path(config)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("top-level value must be a JSON object")
        return data
    except Exception as e:
        # Corrupted state is not a silent reset — warn loudly so a re-launched
        # bg extract (stale pid tracking lost) is explainable (policy 2026-06-24).
        print(f"⚠️  [batch] {p} corrupted ({type(e).__name__}: {e}) "
              f"— resetting bg-extract state.", flush=True)
        return {}


def _save_bg_state(config: Config, state: dict) -> None:
    try:
        config.runtime_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(_bg_state_path(config), json.dumps(state, ensure_ascii=False))
    except OSError as e:
        print(f"⚠️  [batch] failed to write bg-extract state {_bg_state_path(config)} "
              f"({type(e).__name__}: {e}) — bg pid tracking may be stale on resume.", flush=True)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _safe_int(value, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


_BG_HEARTBEAT_STALE_SECONDS = _env_float(
    "IMPROVED_WIKI_BG_HEARTBEAT_STALE_SECONDS", 60.0)
_BG_WAIT_POLL_SECONDS = _env_float(
    "IMPROVED_WIKI_BG_WAIT_POLL_SECONDS", 5.0)
_BG_MAX_WALL_SECONDS = _env_float(
    "IMPROVED_WIKI_BG_EXTRACT_MAX_SECONDS", 0.0)
_BATCH_PREFETCH_PROCESS_LIMIT = 2
_WORKER_TERMINAL_STATES = {"completed", "failed", "stopped"}
_WORKER_HAS_MINERU_TURN = {
    "mineru", "post_mineru", "waiting_caption", "captioning", "completed",
}
_BATCH_SOURCE_HASH_CACHE: dict[tuple[str, int, int], str] = {}


def _batch_source_hash(file: Path) -> str:
    """Hash a batch source once per process/file version, not on every poll."""
    try:
        stat = file.stat()
        key = (str(file), int(stat.st_size), int(stat.st_mtime_ns))
    except OSError:
        key = (str(file), -1, -1)
    cached = _BATCH_SOURCE_HASH_CACHE.get(key)
    if cached is None:
        cached = file_sha256(file)
        _BATCH_SOURCE_HASH_CACHE[key] = cached
    return cached


class BatchPaused(BaseException):
    """Intentional batch pause; subclasses BaseException to bypass retry blocks."""


class BatchPrefetchPaused(BaseException):
    """Background OCR/caption is paused; ready books may still finish their spine."""


class _BackgroundWorkerInterrupted(BaseException):
    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


def _batch_pause_path(config: Config) -> Path:
    return config.runtime_dir / "batch.pause"


def _write_batch_pause_marker(config: Config, reason: str) -> None:
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(
        _batch_pause_path(config),
        json.dumps(
            {"paused_at": time.time(), "reason": reason},
            ensure_ascii=False,
            sort_keys=True,
        ),
    )


def _clear_batch_pause_marker(config: Config) -> None:
    _batch_pause_path(config).unlink(missing_ok=True)


def _raise_if_batch_paused(config: Config) -> None:
    marker = _batch_pause_path(config)
    if marker.exists():
        reason = ""
        try:
            reason = json.loads(marker.read_text(encoding="utf-8")).get("reason", "")
        except Exception:
            pass
        suffix = f" ({reason})" if reason else ""
        raise BatchPaused(
            f"Batch is paused{suffix}. Re-run the same batch with --resume-batch.")


def _pid_probe(pid: int) -> str:
    """Return ``alive``, ``dead``, or ``unknown`` for a PID probe."""
    if not pid:
        return "dead"
    try:
        os.kill(pid, 0)
        return "alive"
    except PermissionError:
        return "unknown"
    except ProcessLookupError:
        return "dead"
    except OSError:
        return "dead"


def _pid_alive(pid: int) -> bool:
    """Compatibility helper; EPERM remains indeterminate rather than dead."""
    return _pid_probe(pid) != "dead"


def _read_worker_status(entry: dict) -> dict | None:
    status_file = entry.get("status_file")
    if not status_file:
        return None
    path = Path(status_file)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        print(f"⚠️  [batch] worker status unreadable ({path}): {exc}", flush=True)
        return None
    if not isinstance(data, dict):
        return None
    token = entry.get("token")
    if token and data.get("token") != token:
        print(f"⚠️  [batch] worker identity mismatch for pid "
              f"{entry.get('pid', 0)} — treating state as stale", flush=True)
        return None
    source_hash = entry.get("source_hash")
    if source_hash and data.get("source_hash") != source_hash:
        print(f"⚠️  [batch] worker source mismatch for pid "
              f"{entry.get('pid', 0)} — treating state as stale", flush=True)
        return None
    if data.get("pid") is not None:
        worker_pid = _safe_int(data.get("pid"))
        entry_pid = _safe_int(entry.get("pid"), 0)
        if worker_pid is None or worker_pid != entry_pid:
            print(f"⚠️  [batch] worker PID identity mismatch for "
                  f"{entry.get('file', '?')} — treating state as stale", flush=True)
            return None
    if data.get("pgid") is not None:
        worker_pgid = _safe_int(data.get("pgid"))
        entry_pgid = _safe_int(entry.get("pgid") or entry.get("pid"), 0)
        if worker_pgid is None or worker_pgid != entry_pgid:
            print(f"⚠️  [batch] worker process-group identity mismatch for "
                  f"{entry.get('file', '?')} — treating state as stale", flush=True)
            return None
    return data


def _worker_lease_state(entry: dict) -> str:
    """Return ``held``, ``free``, ``missing``, or ``unknown`` for a worker lease.

    ``held`` proves that the original token-qualified worker process still owns
    its kernel flock.  ``free`` proves that historical PID/PGID metadata must
    not be used for signalling, even if that numeric PID has since been reused.
    """
    lease_file = entry.get("lease_file")
    if not lease_file and entry.get("status_file") and entry.get("token"):
        lease_file = str(worker_lease_path(Path(entry["status_file"])))
    if not lease_file:
        return "missing"
    path = Path(lease_file)
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unknown"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "held"
        except OSError:
            return "unknown"
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return "free"
    finally:
        os.close(fd)


def _worker_entry_state(entry: dict, now: float | None = None) -> str:
    """Classify a persisted worker using identity + heartbeat + PID evidence."""
    now = time.time() if now is None else now
    status = _read_worker_status(entry)
    if status and status.get("status") in _WORKER_TERMINAL_STATES:
        return str(status["status"])

    lease_state = _worker_lease_state(entry)
    launched_at = _safe_float(entry.get("launched_at"))
    recently_launched = (
        bool(launched_at)
        and now - launched_at <= _BG_HEARTBEAT_STALE_SECONDS
    )
    if lease_state == "free":
        # The unique lease exists but nobody holds it: the original worker is
        # gone. A numerically-live PID can only be a reused/unrelated process.
        return "dead"

    probe = _pid_probe(_safe_int(entry.get("pid"), 0) or 0)
    if probe == "dead":
        return "dead"

    heartbeat_at = _safe_float((status or {}).get("heartbeat_at"))
    heartbeat_fresh = (
        bool(heartbeat_at)
        and now - heartbeat_at <= _BG_HEARTBEAT_STALE_SECONDS
    )
    if lease_state == "held" and heartbeat_fresh:
        return "running"
    if lease_state == "held":
        return "stalled"

    if not status and recently_launched:
        return "starting"
    if heartbeat_fresh:
        # Backward compatibility for schema-2 workers launched before leases
        # were introduced. Fresh token-bound heartbeat remains useful evidence.
        return "running"

    # Legacy v1 entries have no token/status file. A definite live PID can
    # finish naturally; an EPERM-only probe is not enough to wait forever.
    if not entry.get("token") and probe == "alive":
        return "legacy-running"
    return "stalled"


def _worker_entry_phase(entry: dict) -> str:
    status = _read_worker_status(entry)
    return str((status or {}).get("phase") or "")


def _terminate_bg_worker(entry: dict, grace_seconds: float = 5.0) -> bool:
    """Terminate one verified detached process group, then escalate if needed."""
    pid = _safe_int(entry.get("pid"), 0) or 0
    if not pid:
        return False
    status = _read_worker_status(entry)
    lease_state = _worker_lease_state(entry)
    if lease_state == "free":
        print(f"[batch] worker lease is free; pid {pid} is historical/reused — "
              "no signal sent", flush=True)
        return False
    verified = (
        bool(entry.get("token"))
        and lease_state == "held"
        and status is not None
        and status.get("status") not in _WORKER_TERMINAL_STATES
    )
    if not verified:
        print(f"⚠️  [batch] refusing to signal unverified legacy/stale pid {pid} "
              f"(lease={lease_state}); remove its state entry and verify the "
              "process manually", flush=True)
        return False

    pgid = _safe_int(entry.get("pgid"), pid) or pid
    if pgid == os.getpgrp():
        print(f"⚠️  [batch] refusing to signal current process group {pgid}", flush=True)
        return False

    def _signal_group(sig: int) -> bool:
        try:
            os.killpg(pgid, sig)
            return True
        except ProcessLookupError:
            return True
        except OSError as exc:
            print(f"⚠️  [batch] could not signal worker pgid {pgid}: {exc}", flush=True)
            return False

    print(f"[batch] stopping bg worker pgid {pgid} — "
          f"{entry.get('file', '?')}", flush=True)
    if not _signal_group(signal.SIGTERM):
        return False

    deadline = time.time() + max(0.0, grace_seconds)
    while time.time() < deadline:
        current = _read_worker_status(entry)
        if current and current.get("status") in _WORKER_TERMINAL_STATES:
            return True
        if _worker_lease_state(entry) == "free":
            return True
        if _pid_probe(pid) == "dead":
            return True
        time.sleep(0.1)
    if grace_seconds > 0 and _pid_probe(pid) != "dead":
        _signal_group(signal.SIGKILL)
    return True


def _prune_bg_state(config: Config, state: dict) -> None:
    changed = False
    for source_hash, entry in list(state.items()):
        if is_stage_done(config, source_hash, "stage_1_3_done"):
            state.pop(source_hash, None)
            changed = True
            continue
        worker_state = _worker_entry_state(entry)
        if worker_state == "stalled":
            _terminate_bg_worker(entry)
            state.pop(source_hash, None)
            changed = True
        elif worker_state in _WORKER_TERMINAL_STATES or worker_state == "dead":
            status = _read_worker_status(entry) or {}
            if worker_state == "failed" and status.get("error"):
                print(f"⚠️  [batch] previous bg worker failed for "
                      f"{entry.get('file', '?')}: {status['error']}", flush=True)
            state.pop(source_hash, None)
            changed = True
    if changed:
        _save_bg_state(config, state)


def _launch_bg_extract(file: Path, config: Config, state: dict) -> bool:
    """Launch one identity-tracked detached Phase 0/1 worker."""
    h = _batch_source_hash(file)
    existing = state.get(h)
    if existing and _worker_entry_state(existing) in {
        "running", "starting", "legacy-running",
    }:
        return False
    if existing:
        if _worker_entry_state(existing) == "stalled":
            _terminate_bg_worker(existing)
        state.pop(h, None)

    token = uuid.uuid4().hex
    worker_dir = config.runtime_dir / "batch-workers"
    worker_dir.mkdir(parents=True, exist_ok=True)
    status_path = worker_dir / f"{h[:16]}-{token[:8]}.json"
    log_path = config.runtime_dir / f"bg-extract-{h[:8]}-{token[:8]}.log"
    cmd = [
        sys.executable,
        str(_script_dir / "ingest.py"),
        "--batch-extract-worker",
        "--batch-worker-token", token,
        "--batch-worker-status", str(status_path),
        str(file),
    ]
    try:
        log = open(log_path, "w", encoding="utf-8")
    except OSError as exc:
        print(f"⚠️  [batch] could not open bg-extract log {log_path} "
              f"({type(exc).__name__}: {exc}) — bg output discarded.", flush=True)
        log = subprocess.DEVNULL
    proc = subprocess.Popen(
        cmd,
        cwd=str(config.wiki_root),
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    if log is not subprocess.DEVNULL:
        log.close()
    state[h] = {
        "schema": 2,
        "pid": proc.pid,
        "pgid": proc.pid,
        "token": token,
        "source_hash": h,
        "source_path": str(file),
        "file": file.name,
        "launched_at": time.time(),
        "status_file": str(status_path),
        "lease_file": str(worker_lease_path(status_path)),
        "log_file": str(log_path),
    }
    _save_bg_state(config, state)
    print(f"[batch] bg extract launched (pid {proc.pid}) — {file.name}", flush=True)
    return True


def _launch_next_pending_extract(
    raw_files: list[Path],
    start_index: int,
    config: Config,
    state: dict,
) -> Path | None:
    """Launch the first not-yet-extracted source at or after ``start_index``."""
    for file in raw_files[start_index:]:
        h = _batch_source_hash(file)
        if is_stage_done(config, h, "stage_1_3_done"):
            continue
        _launch_bg_extract(file, config, state)
        return file
    return None


def _fill_prefetch_slots(
    raw_files: list[Path],
    start_index: int,
    config: Config,
    state: dict,
    max_concurrent: int,
) -> list[Path]:
    """Fill the two-stage OCR/caption pipeline without reordering minerU work."""
    _raise_if_batch_paused(config)
    if is_prefetch_paused(config):
        return []
    _prune_bg_state(config, state)
    limit = max(1, min(int(max_concurrent), _BATCH_PREFETCH_PROCESS_LIMIT))
    launched: list[Path] = []

    while True:
        active = {
            h: entry for h, entry in state.items()
            if _worker_entry_state(entry) in {
                "running", "starting", "legacy-running",
            }
        }
        if len(active) >= limit:
            break
        # Start source N+1 only after every earlier active worker has acquired
        # (or released) minerU at least once. This prevents a later source from
        # winning the global flock and delaying the current book.
        if active and any(
            _worker_entry_phase(entry) not in _WORKER_HAS_MINERU_TURN
            for entry in active.values()
        ):
            break

        candidate = None
        for file in raw_files[start_index:]:
            h = _batch_source_hash(file)
            if is_stage_done(config, h, "stage_1_3_done") or h in active:
                continue
            candidate = file
            break
        if candidate is None:
            break
        if _launch_bg_extract(candidate, config, state):
            launched.append(candidate)
        else:
            break
    return launched


def _wait_extract_done(
    config: Config,
    h: str,
    bg_pid: int = 0,
    timeout: float | None = None,
    *,
    bg_entry: dict | None = None,
    on_poll=None,
) -> bool:
    """Wait on a healthy heartbeat, not a fixed two-hour PID-only guess."""
    max_wall = _BG_MAX_WALL_SECONDS if timeout is None else max(0.0, timeout)
    deadline = time.time() + max_wall if max_wall else None
    if bg_entry is None and not bg_pid:
        return is_stage_done(config, h, "stage_1_3_done")

    while True:
        _raise_if_batch_paused(config)
        if is_stage_done(config, h, "stage_1_3_done"):
            return True
        if is_prefetch_paused(config):
            raise BatchPrefetchPaused(
                "Background OCR/caption prefetch is paused. Already-extracted "
                "books may finish, but this source still needs Phase 1.")
        if on_poll is not None:
            on_poll()

        if bg_entry is not None:
            worker_state = _worker_entry_state(bg_entry)
            if worker_state in _WORKER_TERMINAL_STATES or worker_state in {
                "dead", "stalled",
            }:
                status = _read_worker_status(bg_entry) or {}
                detail = f": {status.get('error')}" if status.get("error") else ""
                print(f"[batch] bg extract {worker_state} before completing "
                      f"Phase 0/1{detail}", flush=True)
                return False
        elif bg_pid and _pid_probe(bg_pid) == "dead":
            print(f"[batch] legacy bg extract (pid {bg_pid}) died before "
                  f"completing Phase 0/1", flush=True)
            return False

        if deadline is not None and time.time() >= deadline:
            print(f"[batch] bg extract exceeded configured max wall time "
                  f"({max_wall:.0f}s)", flush=True)
            return False
        time.sleep(max(0.1, _BG_WAIT_POLL_SECONDS))


def _pause_batch_workers(
    config: Config,
    state: dict,
    reason: str,
    *,
    write_marker: bool = True,
    grace_seconds: float = 5.0,
) -> int:
    if write_marker:
        _write_batch_pause_marker(config, reason)
    stopped = 0
    for entry in list(state.values()):
        if _worker_entry_state(entry) in {
            "running", "starting", "legacy-running", "stalled",
        } and _terminate_bg_worker(entry, grace_seconds=grace_seconds):
            stopped += 1
    _save_bg_state(config, state)
    return stopped


def _run_background_extract_worker(
    raw_file: Path,
    config: Config,
    status_path: Path,
    token: str,
    template_override: str | None,
    verbose: bool,
) -> int:
    """Run the internal Phase 0/1 worker with heartbeat and signal cleanup."""
    h = file_sha256(raw_file)
    reporter = BatchWorkerReporter(status_path, token, raw_file, h)
    reporter.start()
    previous_handlers: dict[int, object] = {}

    def _handle_worker_signal(signum, _frame):
        reporter.update(status="stopping")
        raise _BackgroundWorkerInterrupted(signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[sig] = signal.getsignal(sig)
        signal.signal(sig, _handle_worker_signal)

    try:
        if _batch_pause_path(config).exists() or is_prefetch_paused(config):
            reporter.finish(
                "stopped",
                75,
                "batch/prefetch pause marker present at worker startup",
            )
            return 75
        from _context_probe import load_cached
        if load_cached(config) is None:
            raise RuntimeError(
                "context-probe cache miss in detached extract worker; "
                "foreground coordinator must populate it first")
        config.stop_after_stage = "0"
        _probe_and_apply_context(config)
        result = ingest_one(
            raw_file,
            config,
            template_override,
            verbose=verbose,
        )
        code = 0 if result.get("status") in ("ok", "skipped") else 1
        reporter.finish("completed" if code == 0 else "failed", code)
        return code
    except _BackgroundWorkerInterrupted as exc:
        code = 128 + int(exc.signum)
        reporter.finish("stopped", code, f"signal {exc.signum}")
        return code
    except ConversationPending:
        message = "detached extract worker emitted an unanswerable LLM handoff"
        reporter.finish("failed", 1, message)
        print(f"ERROR: {message}", file=sys.stderr, flush=True)
        return 1
    except BaseException as exc:
        message = f"{type(exc).__name__}: {exc}"
        reporter.finish("failed", 1, message)
        print(f"ERROR: background extract failed: {message}", file=sys.stderr, flush=True)
        traceback.print_exc()
        return 1
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def batch_ingest(
    raw_files: list[Path],
    config: Config,
    max_concurrent: int = BATCH_MAX_CONCURRENT,
    template_override: str | None = None,
    verbose: bool = False,
) -> list[dict]:
    """Run one batch coordinator invocation under a transient project flock."""
    config.handoff_parallel_limit = max(1, int(max_concurrent))
    with batch_coordinator_slot(config):
        return _batch_ingest_under_coordinator(
            raw_files,
            config,
            max_concurrent=max_concurrent,
            template_override=template_override,
            verbose=verbose,
        )


def _assert_batch_resume_order(
    raw_files: list[Path],
    config: Config,
) -> None:
    """Require a reserved source to be the first unfinished book in the list."""
    reservation = load_spine_reservation(config)
    if not reservation:
        return
    owner = str(reservation.get("source_hash") or "")
    if not owner or is_stage_done(config, owner, "ingested"):
        return

    owner_index = None
    hashes: list[str] = []
    for index, raw_file in enumerate(raw_files):
        source_hash = _batch_source_hash(raw_file)
        hashes.append(source_hash)
        if source_hash == owner:
            owner_index = index
            break
    if owner_index is None:
        raise SpineReservationConflict(
            "The active serial-spine owner is not present in this batch file "
            f"list: {reservation.get('source_path', '?')} ({owner[-8:]}). "
            "Resume with the confirmed original list or that source alone.")
    for index in range(owner_index):
        if not is_stage_done(config, hashes[index], "ingested"):
            raise SpineReservationConflict(
                "The active serial-spine owner is not the first unfinished "
                "source in this batch order. Resume the owner first: "
                f"{reservation.get('source_path', '?')} ({owner[-8:]})."
            )


def _batch_ingest_under_coordinator(
    raw_files: list[Path],
    config: Config,
    max_concurrent: int = BATCH_MAX_CONCURRENT,
    template_override: str | None = None,
    verbose: bool = False,
) -> list[dict]:
    """Two-stage Phase-1 prefetch plus a strictly serial wiki spine.

    At most two detached workers are useful: one owns the global minerU slot
    while the other may own the globally rate-limited caption slot. The
    ``max_concurrent`` argument now actively bounds those workers (with a hard
    process cap of two); minerU itself remains one-at-a-time. Stage 2.3+ stays
    serial and holds ProjectLock only for the current book's wiki-dependent
    prepare/write/finalize segment.
    """
    total_books = len(raw_files)
    max_concurrent = max(1, int(max_concurrent))
    prefetch_slots = min(max_concurrent, _BATCH_PREFETCH_PROCESS_LIMIT)
    print(f"\n{'='*60}")
    print(f"Batch ingest (pipeline): {total_books} books — "
          f"{prefetch_slots} Phase-1 worker(s), 1 minerU, 1 serial spine")
    print(f"{'='*60}")

    _raise_if_batch_paused(config)
    _assert_batch_resume_order(raw_files, config)
    bg_state = _load_bg_state(config)
    _prune_bg_state(config, bg_state)
    results: list[dict] = []
    previous_handlers: dict[int, object] = {}

    def _handle_batch_signal(signum, _frame):
        reason = f"coordinator received signal {signum}"
        _write_batch_pause_marker(config, reason)
        _pause_batch_workers(
            config,
            bg_state,
            reason,
            write_marker=False,
            grace_seconds=0,
        )
        raise BatchPaused(reason)

    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, _handle_batch_signal)

    try:
        _fill_prefetch_slots(
            raw_files, 0, config, bg_state, max_concurrent)

        for i, f in enumerate(raw_files, 1):
            _raise_if_batch_paused(config)
            h = _batch_source_hash(f)
            config.conversation_prefix = h[-8:]
            print(f"\n[batch] book {i}/{total_books} — {f.name}", flush=True)

            if not is_stage_done(config, h, "stage_1_3_done"):
                print(f"[batch] waiting for bg extract (Phase 0/1) — {f.name}", flush=True)
                entry = bg_state.get(h)
                if entry is None:
                    _fill_prefetch_slots(
                        raw_files, i - 1, config, bg_state, max_concurrent)
                    entry = bg_state.get(h)
                if not _wait_extract_done(
                    config,
                    h,
                    bg_pid=int((entry or {}).get("pid") or 0),
                    bg_entry=entry,
                    on_poll=lambda: _fill_prefetch_slots(
                        raw_files, i - 1, config, bg_state, max_concurrent),
                ):
                    if entry is not None:
                        _terminate_bg_worker(entry)
                        bg_state.pop(h, None)
                        _save_bg_state(config, bg_state)
                    _raise_if_batch_paused(config)
                    print(f"[batch] bg extract unavailable — falling back to "
                          f"foreground extraction — {f.name}", flush=True)

            # Once this book has passed Phase 1, refill the two-stage pipeline.
            _fill_prefetch_slots(
                raw_files, i, config, bg_state, max_concurrent)

            # Wiki-independent Stage 2.2 runs without ProjectLock.
            try:
                _do_prepare(f, config, template_override, verbose, True)
            except PrepareStopAfter:
                pass
            except ConversationPending:
                raise

            _raise_if_batch_paused(config)
            spine_lock = ProjectLock(config, owner_id=f"batch:{h[-8:]}")
            if not spine_lock.acquire():
                raise RuntimeError(
                    f"Could not acquire project lock for {f.name} write spine")
            spine_reserved = False
            abort_batch = False
            try:
                reserve_spine(
                    config,
                    h,
                    f,
                    phase="stage_2_3_plus",
                )
                spine_reserved = True
                try:
                    prepared = _do_prepare(
                        f, config, template_override, verbose)
                except PrepareStopAfter as stop:
                    print(f"[batch] {i}/{total_books} stopped after stage "
                          f"{stop.stage} — {f.name}", flush=True)
                    results.append({
                        "status": "skipped",
                        "raw_file": str(f),
                        "stopped_after": stop.stage,
                    })
                    release_spine_reservation(config, h)
                    continue
                if prepared is None:
                    print(f"[batch] {i}/{total_books} skipped "
                          f"(already complete) — {f.name}", flush=True)
                    results.append({"status": "skipped", "raw_file": str(f)})
                    release_spine_reservation(config, h)
                    continue
                refresh_spine_reservation(
                    config, h, phase="write_and_finalize")
                result = _do_write(prepared, verbose=verbose)
                if result.get("status") != "ok":
                    raise RuntimeError(
                        f"Serial spine returned {result.get('status')!r} for "
                        f"{f.name}; later books were not advanced.")
                _finalize_book(
                    prepared["raw_file"],
                    config,
                    result.get("files_written", []),
                    prepared["h"],
                )
                result["raw_file"] = str(f)
                results.append(result)
                release_spine_reservation(config, h)
            except ConversationPending:
                if spine_reserved:
                    refresh_spine_reservation(
                        config, h, phase="waiting_handoff")
                raise
            except BatchPaused:
                if spine_reserved:
                    refresh_spine_reservation(config, h, phase="paused")
                raise
            except SpineReservationConflict:
                raise
            except Exception as exc:
                if spine_reserved:
                    refresh_spine_reservation(config, h, phase="failed")
                print(f"[batch] {i}/{total_books} FAILED for "
                      f"{f.name}: {exc}", flush=True)
                traceback.print_exc()
                results.append({
                    "status": "failed",
                    "raw_file": str(f),
                    "error": str(exc),
                })
                abort_batch = True
            finally:
                spine_lock.release()
            if abort_batch:
                print(
                    f"[batch] serial spine stopped at {f.name}; "
                    f"{total_books - i} later book(s) were not advanced.",
                    flush=True,
                )
                break

        _prune_bg_state(config, bg_state)
        _save_bg_state(config, bg_state)
    finally:
        _prune_bg_state(config, bg_state)
        _save_bg_state(config, bg_state)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)

    ok = sum(1 for r in results if r.get("status") in ("ok", "skipped"))
    failed = sum(1 for r in results if r.get("status") == "failed")
    print(f"\n{'='*60}")
    if failed or len(results) < total_books:
        print(
            f"Batch stopped: {ok}/{total_books} successful, "
            f"{failed} failed, {total_books - len(results)} not advanced"
        )
    else:
        print(f"Batch complete: {ok}/{total_books} books processed successfully")
    print(f"{'='*60}")

    return results


def _read_json_object(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        return {
            "_read_error": f"{type(exc).__name__}: {exc}",
            "_path": str(path),
        }
    return value if isinstance(value, dict) else {
        "_read_error": f"expected JSON object, got {type(value).__name__}",
        "_path": str(path),
    }


def _batch_status_snapshot(config: Config) -> dict:
    """Build a read-only project snapshot for pause/resume troubleshooting."""
    workers = []
    for source_hash, entry in _load_bg_state(config).items():
        workers.append({
            "source_hash": source_hash,
            "file": entry.get("file") or entry.get("source_path") or "?",
            "pid": _safe_int(entry.get("pid"), 0) or 0,
            "state": _worker_entry_state(entry),
            "phase": _worker_entry_phase(entry) or "unknown",
            "lease": _worker_lease_state(entry),
        })

    handoffs = []
    conversation_root = config.runtime_dir / "conversation"
    if conversation_root.exists():
        for manifest_path in sorted(conversation_root.glob("*/tasks.json")):
            manifest = _read_json_object(manifest_path) or {}
            tasks = manifest.get("tasks", {})
            if not isinstance(tasks, dict):
                tasks = {}
            pending = [
                task for task in tasks.values()
                if isinstance(task, dict) and task.get("status") == "pending"
            ]
            ready = 0
            for task in pending:
                result_name = task.get("result_file")
                if not result_name:
                    continue
                result_path = manifest_path.parent / str(result_name)
                try:
                    if result_path.stat().st_size > 0:
                        ready += 1
                except OSError:
                    pass
            if pending:
                handoffs.append({
                    "prefix": manifest_path.parent.name,
                    "pending": len(pending),
                    "answer_ready": ready,
                    "needs_answer": len(pending) - ready,
                })

    sources = []
    if config.progress_dir.exists():
        for task_path in sorted(config.progress_dir.glob("*.task.json")):
            manifest = _read_json_object(task_path) or {}
            source = manifest.get("source", {})
            if not isinstance(source, dict):
                source = {}
            source_hash = str(source.get("sha256") or "")
            if not source_hash:
                continue
            stage_path = (
                config.progress_dir / f"{source_hash[:16]}.stages.json")
            stages = _read_json_object(stage_path) or {}
            if stages.get("ingested"):
                continue
            markers = sorted(
                key for key, value in stages.items()
                if not key.startswith("_")
                and not key.endswith("__payload")
                and bool(value)
            )
            latest_marker = "none"
            if markers:
                latest_marker = max(
                    markers,
                    key=lambda key: _safe_float(stages.get(key)),
                )
            sources.append({
                "identity": source.get("identity") or task_path.stem,
                "source_hash": source_hash,
                "task_status": manifest.get("status") or "unknown",
                "markers": markers,
                "latest_marker": latest_marker,
                "updated_at": _safe_int(manifest.get("updated_at"), 0) or 0,
            })
    sources.sort(key=lambda item: item["updated_at"], reverse=True)

    try:
        reservation = load_spine_reservation(config)
    except RuntimeError as exc:
        reservation = {"_read_error": str(exc)}

    return {
        "batch_pause": _read_json_object(_batch_pause_path(config)),
        "prefetch_pause": _read_json_object(prefetch_pause_path(config)),
        "spine_reservation": reservation,
        "workers": workers,
        "handoffs": handoffs,
        "unfinished_sources": sources,
    }


def _print_batch_status(config: Config) -> None:
    snapshot = _batch_status_snapshot(config)
    full_pause = snapshot["batch_pause"]
    prefetch_pause = snapshot["prefetch_pause"]
    print("[batch-status]")
    print("  full batch: " + (
        f"PAUSED — {full_pause.get('reason', 'no reason recorded')}"
        if full_pause else "running/not paused"))
    print("  OCR/caption prefetch: " + (
        f"PAUSED — {prefetch_pause.get('reason', 'no reason recorded')}"
        if prefetch_pause else "running/not paused"))

    reservation = snapshot["spine_reservation"]
    if reservation and reservation.get("_read_error"):
        print(f"  serial spine: ERROR — {reservation['_read_error']}")
    elif reservation:
        print(
            "  serial spine: reserved — "
            f"{reservation.get('source_path', '?')} "
            f"({str(reservation.get('source_hash', ''))[-8:]}, "
            f"{reservation.get('phase', 'unknown')})"
        )
    else:
        print("  serial spine: free")

    workers = snapshot["workers"]
    active_workers = sum(
        worker["state"] in {"running", "starting", "legacy-running"}
        for worker in workers
    )
    stalled_workers = sum(
        worker["state"] == "stalled" for worker in workers)
    historical_workers = len(workers) - active_workers - stalled_workers
    print(
        "  Phase-1 worker records: "
        f"{active_workers} active, {stalled_workers} stalled, "
        f"{historical_workers} historical/terminal"
    )
    for worker in workers:
        print(
            f"    - {worker['file']}: {worker['state']}/"
            f"{worker['phase']}, pid={worker['pid']}, "
            f"lease={worker['lease']}"
        )

    handoffs = snapshot["handoffs"]
    total_pending = sum(item["pending"] for item in handoffs)
    total_ready = sum(item["answer_ready"] for item in handoffs)
    print(
        f"  handoffs: {total_pending} pending "
        f"({total_ready} answer-ready, "
        f"{total_pending - total_ready} need answers)"
    )
    for item in handoffs[:20]:
        print(
            f"    - {item['prefix']}: {item['pending']} pending, "
            f"{item['answer_ready']} answer-ready"
        )

    sources = snapshot["unfinished_sources"]
    print(f"  unfinished sources: {len(sources)}")
    for source in sources[:20]:
        print(
            f"    - {source['identity']} "
            f"({source['source_hash'][-8:]}): "
            f"{source['task_status']}, "
            f"latest-marker={source['latest_marker']}"
        )
    if len(sources) > 20:
        print(f"    ... {len(sources) - 20} more")


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
                        help=f"Pipeline concurrency ceiling (default: {BATCH_MAX_CONCURRENT}). "
                             "The OS prefetch pipeline uses at most 2 workers "
                             "(1 minerU + 1 caption stage); the same value caps "
                             "each Stage 2.4 parallel handoff wave.")
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
        return 0

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
            return 1
        try:
            try:
                reservation = load_spine_reservation(config)
            except RuntimeError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            if not reservation:
                print("[batch] serial spine is already free")
                return 0
            owner = str(reservation.get("source_hash") or "")
            supplied = args.abandon_spine.strip()
            if not owner or supplied not in {owner, owner[-8:]}:
                print(
                    "ERROR: reservation owner does not match "
                    f"{supplied!r}; current owner suffix is {owner[-8:]!r}.",
                    file=sys.stderr,
                )
                return 2
            release_spine_reservation(config, owner)
            print(
                f"[batch] abandoned serial-spine reservation {owner[-8:]}. "
                "Only continue with another source after checking any partial "
                "wiki writes from the abandoned source."
            )
            return 0
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
        return 0

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
        return 0

    if args.resume_prefetch:
        config = Config.from_env()
        clear_prefetch_pause_marker(config)
        print("[batch] OCR/caption prefetch pause marker cleared")
        if not args.file and not args.watch:
            return 0

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
        config.handoff_parallel_limit = (
            args.parallel if args.parallel > 0 else BATCH_MAX_CONCURRENT)
        if args.resume_batch:
            _clear_batch_pause_marker(config)
            clear_prefetch_pause_marker(config)
            print("[watch] full/prefetch pause markers cleared — resuming queue")
        elif _batch_pause_path(config).exists():
            print("ERROR: watch batch is paused; restart with "
                  "--watch --resume-batch.", file=sys.stderr)
            return 75
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
        except BatchPaused as pause:
            print(f"[watch] batch paused: {pause}", file=sys.stderr)
            return 75
        except BatchPrefetchPaused as pause:
            print(f"[watch] OCR/caption prefetch paused: {pause}",
                  file=sys.stderr)
            return 76
        except SpineReservationConflict as conflict:
            print(f"[watch] {conflict}", file=sys.stderr)
            return 77
        except BatchCoordinatorBusy as busy:
            print(f"[watch] {busy}", file=sys.stderr)
            return 78
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
            delete_source(rf, config, dry_run=args.dry_run, keep_media=args.keep_media)
        return 0

    config = Config.from_env()
    config.enrich_enabled = args.enrich_wikilinks and not args.no_enrich
    config.stop_after_stage = args.stop_after_stage
    config.handoff_parallel_limit = (
        args.parallel if args.parallel > 0 else BATCH_MAX_CONCURRENT)

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
        # NashSU deep-research parity (2026-07-16): accept a wiki/queries/<page>
        # research page as an ingest source directly, no raw/queries/ copy step
        # (see _is_ingestable_source_path / is_query_bridge_source).
        if not _is_ingestable_source_path(rf, config):
            print(f"ERROR: {rf} is not under raw_root ({config.raw_root}) "
                  f"or wiki/queries/ ({config.wiki_dir / 'queries'})", file=sys.stderr)
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

    # Internal detached Phase-1 worker. The random token and status path are
    # generated by the coordinator; users should never invoke this directly.
    if args.batch_extract_worker:
        if len(raw_files) != 1 or not args.batch_worker_token or not args.batch_worker_status:
            print("ERROR: invalid internal batch worker invocation", file=sys.stderr)
            return 2
        status_path = Path(args.batch_worker_status).expanduser().resolve()
        worker_root = (config.runtime_dir / "batch-workers").resolve()
        if not status_path.is_relative_to(worker_root):
            print(f"ERROR: worker status path must be under {worker_root}",
                  file=sys.stderr)
            return 2
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
        return 2
    if is_batch and args.no_project_lock:
        print("ERROR: --no-project-lock is single-source only; batch mode manages "
              "its own Phase-1 workers and spine locks.", file=sys.stderr)
        return 2
    if (args.no_project_lock
            and args.stop_after_stage not in {"0", "1.5"}):
        print(
            "ERROR: --no-project-lock is limited to read-only prefetch "
            "(--stop-after-stage 0 or 1.5).",
            file=sys.stderr,
        )
        return 2
    if args.resume_batch and not is_batch:
        print("ERROR: --resume-batch requires the complete multi-source file list.",
              file=sys.stderr)
        return 2
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
        return 75

    if args.no_project_lock:
        # Explicit single-source read-only prefetch: do not emit a context probe
        # handoff that an unattended caller may never answer.
        from _context_probe import load_cached
        if load_cached(config) is None:
            print("ERROR: context-probe cache miss with --no-project-lock; "
                  "run a normal foreground ingest once first.", file=sys.stderr)
            return 1

    try:
        _probe_and_apply_context(config)
    except ConversationPending:
        return 101


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
            return 101
        except BatchPaused as pause:
            print(f"[batch] paused: {pause}", file=sys.stderr)
            return 75
        except BatchPrefetchPaused as pause:
            print(f"[batch] OCR/caption prefetch paused: {pause}",
                  file=sys.stderr)
            return 76
        except SpineReservationConflict as conflict:
            print(f"[batch] {conflict}", file=sys.stderr)
            return 77
        except BatchCoordinatorBusy as busy:
            print(f"[batch] {busy}", file=sys.stderr)
            return 78
        ok = sum(1 for r in results if r.get("status") in ("ok", "skipped"))
        return 0 if ok == len(raw_files) else 1

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
                print(f"  Estimated API calls: {chunks_est} (Stage 2.2 chunks) + 1-3 (Stage 2.4)")
            except Exception:
                pass
        print(f"  Stages: text-extract -> image-extract+caption -> chunk+analyze -> generate -> review -> inject -> write -> cache")
        return 0

    h = file_sha256(raw_file)
    if args.no_project_lock:
        # Explicit single-source read-only prefetch. Internal detached workers
        # use --batch-extract-worker and never route through this branch.
        try:
            result = ingest_one(raw_file, config, args.type, verbose=args.verbose)
            print(f"\nResult: {result}")
            return 0 if result["status"] in ("ok", "skipped") else 1
        except ConversationPending:
            return 101

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
            return 0
    except ConversationPending:
        return 101
    else:
        if prefetched is None:
            result = {"status": "skipped", "reason": "source-page-exists"}
            print(f"\nResult: {result}")
            return 0
        if args.stop_after_stage in {"0", "1.5"}:
            result = {
                "status": "ok",
                "stopped_after": args.stop_after_stage,
            }
            print(f"\nResult: {result}")
            return 0

    lock = ProjectLock(config, owner_id=h[-8:])
    if not lock.acquire():
        print("ERROR: Could not acquire project lock — another ingest may be running", file=sys.stderr)
        return 1
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
        return 0 if result["status"] in ("ok", "skipped") else 1
    except ConversationPending:
        if spine_reserved:
            refresh_spine_reservation(
                config, h, phase="waiting_handoff")
        return 101
    except SpineReservationConflict as conflict:
        print(f"ERROR: {conflict}", file=sys.stderr)
        return 77
    except Exception:
        if spine_reserved:
            refresh_spine_reservation(config, h, phase="failed")
        raise
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
