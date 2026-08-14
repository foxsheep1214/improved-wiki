"""Batch Phase-1 prefetch supervisor and serial write-spine coordinator."""
from __future__ import annotations

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

from _config import Config
from _exit_codes import BATCH_PAUSED, ERROR, OK
from _core import BATCH_MAX_CONCURRENT, ConversationPending, PrepareStopAfter
from _progress import ProjectLock, file_sha256, is_stage_done
from _paths import atomic_write
from _batch_worker_status import BatchWorkerReporter, worker_lease_path
from _batch_coordination import (
    SpineReservationConflict,
    batch_coordinator_slot,
    is_prefetch_paused,
    load_spine_reservation,
    refresh_spine_reservation,
    release_spine_reservation,
    reserve_spine,
)
from _ingest_prepare import _do_prepare
from _ingest_write import _do_write
from _ingest_runner import _finalize_book, ingest_one
from _context_probe import resolve_context

_script_dir = Path(__file__).resolve().parent


def _probe_and_apply_context(config: Config) -> None:
    config.apply_context(resolve_context(config))


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
            return BATCH_PAUSED
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
        code = OK if result.get("status") in ("ok", "skipped") else ERROR
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
        return ERROR
    except BaseException as exc:
        message = f"{type(exc).__name__}: {exc}"
        reporter.finish("failed", 1, message)
        print(f"ERROR: background extract failed: {message}", file=sys.stderr, flush=True)
        traceback.print_exc()
        return ERROR
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

            # A resumed batch retains the logical Stage 2.3+ reservation for
            # the first unfinished book.  Do not let an already-finalized book
            # ahead of that owner enter the write-spine path: reserve_spine()
            # correctly rejects it as a different owner, but that used to make
            # every resume stop at the first completed source.  The authoritative
            # completion marker is sufficient here; _do_prepare() already
            # performs the deeper stale-marker/source-page reconciliation when
            # a source is not finalized.
            if is_stage_done(config, h, "ingested"):
                print(f"[batch] {i}/{total_books} skipped "
                      f"(already complete) — {f.name}", flush=True)
                results.append({"status": "skipped", "raw_file": str(f)})
                continue

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

            # Stage 2.2 freezes read-only slug/index context once, then remains
            # snapshot-stable and runs without ProjectLock.
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
