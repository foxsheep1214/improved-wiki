"""Queue-based continuous ingestion (--watch daemon mode).

Extracted from ingest.py on 2026-06-23. Reads ingest-queue.json, feeds
pending entries through the batch pipeline, and updates their status.
wiki-monitor.sh adds files to the queue; ``ingest.py --watch`` consumes
them. ``batch_ingest`` is imported lazily because it lives in ingest.py
(which imports this module), breaking the cycle at runtime.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

from _core import BATCH_MAX_CONCURRENT, Config, ConversationPending
from _batch_coordination import (
    BatchCoordinatorBusy,
    SpineReservationConflict,
)
from _paths import atomic_write


def _read_queue(config: Config) -> list[dict]:
    """Read ingest-queue.json, returning entries sorted by addedAt (oldest first).

    A corrupted queue file is preserved (renamed ``.corrupt-<unix-secs>``) with a
    loud warning instead of being silently ignored — the next ``_write_queue``
    would otherwise clobber it, losing whatever entries it still held.
    """
    qpath = config.runtime_dir / "ingest-queue.json"
    if not qpath.exists():
        return []
    try:
        queue = json.loads(qpath.read_text(encoding="utf-8"))
        if not isinstance(queue, list):
            raise ValueError(f"expected a JSON list, got {type(queue).__name__}")
        # Sort: priority first, then oldest addedAt
        return sorted(queue, key=lambda e: (
            0 if e.get("priority") else 1,
            e.get("addedAt", 0),
        ))
    except Exception as e:
        corrupt = qpath.with_name(f"{qpath.name}.corrupt-{int(time.time())}")
        print(f"⚠️  [watch] {qpath} corrupted ({type(e).__name__}: {e}) — "
              f"preserving it as {corrupt.name} and starting with an empty queue.",
              flush=True)
        try:
            qpath.rename(corrupt)
        except OSError as rename_err:
            print(f"⚠️  [watch] could not preserve corrupt queue file: {rename_err}",
                  flush=True)
        return []


def _write_queue(config: Config, queue: list[dict]) -> None:
    """Atomically write ingest-queue.json."""
    qpath = config.runtime_dir / "ingest-queue.json"
    qpath.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(qpath, json.dumps(queue, ensure_ascii=False, indent=2))


def _queue_entry_to_file(entry: dict, config: Config) -> Path | None:
    """Convert a queue entry's sourcePath to an absolute raw file path."""
    sp = entry.get("sourcePath", "")
    if not sp:
        return None
    # sourcePath is like "raw/Book/foo.pdf"
    if sp.startswith("raw/"):
        sp = sp[4:]
    full = (config.raw_root / sp).expanduser().resolve()
    if full.exists():
        return full
    return None


def ingest_watch(
    config: Config,
    poll_interval: int = 120,
    drain: bool = False,
    max_concurrent: int = BATCH_MAX_CONCURRENT,
    max_retries: int = 3,
    resume_from: str | None = None,
    verbose: bool = False,
) -> None:
    """Continuously watch ingest-queue.json and process pending entries.

    Each watch cycle:
      1. Read the queue
      2. Collect pending entries (status=pending, or failed with retryCount < max_retries)
      3. Feed them through the batch pipeline (two-stage Phase-1 prefetch,
         current-book Stage 2.2, serial Stage 2.3+)
      4. Update queue status for each (done / failed / skipped)
      5. Re-scan for new entries added by wiki-monitor.sh
      6. If --drain: exit when queue is empty; otherwise loop forever

    This is the daemon mode: run it in a tmux/screen session or via nohup.
    wiki-monitor.sh (cron or manual) adds new files to the queue;
    ingest.py --watch picks them up in the next cycle.
    """
    from ingest import batch_ingest  # lazy: breaks ingest <-> _watch import cycle
    # A watcher needs singleton protection, but it must not monopolize the
    # wiki write lock while sleeping, extracting, or waiting for handoffs.
    # batch_ingest acquires ProjectLock only around each active 2.3+ call and
    # uses a source-bound durable reservation across exit-101 handoffs.
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    watch_lock_path = config.runtime_dir / "watch.lock"
    watch_lock_fd = os.open(watch_lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(watch_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(watch_lock_fd)
        raise RuntimeError(
            "Could not acquire watch.lock — another ingest.py --watch is running")
    except Exception:
        os.close(watch_lock_fd)
        raise

    cycle = 0
    total_processed = 0
    total_done = 0
    total_failed = 0

    print(f"[watch] Starting queue watcher (poll={poll_interval}s, "
          f"concurrent={max_concurrent}, retries={max_retries}, "
          f"drain={'yes' if drain else 'no'})")
    print(f"[watch] Queue: {config.runtime_dir / 'ingest-queue.json'}")
    if drain:
        print(f"[watch] Mode: drain — will exit when queue is empty")
    else:
        print(f"[watch] Mode: continuous — press Ctrl+C to stop")

    try:
        while True:
            cycle += 1
            queue = _read_queue(config)
            # Separate pending from the rest
            pending: list[dict] = []
            rest: list[dict] = []

            for entry in queue:
                status = entry.get("status", "pending")
                retries = entry.get("retryCount", 0)

                if status == "done":
                    rest.append(entry)
                    continue

                if status == "failed" and retries >= max_retries:
                    rest.append(entry)
                    continue

                # pending, processing, or failed with retries remaining
                if status in ("pending", "failed"):
                    pending.append(entry)
                elif status == "processing":
                    # Stale processing marker — re-queue
                    entry["status"] = "pending"
                    pending.append(entry)
                else:
                    rest.append(entry)

            if not pending:
                if drain:
                    print(f"[watch] Queue empty — draining complete. "
                          f"Total: {total_processed} processed "
                          f"({total_done} done, {total_failed} failed)")
                    break
                else:
                    ts = time.strftime("%H:%M:%S")
                    print(f"[watch] [{ts}] No pending entries. "
                          f"Waiting {poll_interval}s... "
                          f"(processed {total_processed} so far: "
                          f"{total_done} done, {total_failed} failed)", flush=True)
                    time.sleep(poll_interval)
                    continue

            # Process this wave
            wave_size = len(pending)
            print(f"\n[watch] Cycle {cycle} — {wave_size} pending entries")
            for i, e in enumerate(pending):
                sp = e.get("sourcePath", "?")
                retries = e.get("retryCount", 0)
                tag = f" (retry {retries})" if retries > 0 else ""
                print(f"  {i+1}. {sp}{tag}")

            # Convert to file paths (skip entries with missing files)
            wave_files: list[tuple[dict, Path]] = []
            for entry in pending:
                fp = _queue_entry_to_file(entry, config)
                if fp is None:
                    sp = entry.get("sourcePath", "?")
                    print(f"  SKIP: {sp} — file not found in raw/")
                    entry["status"] = "failed"
                    entry["error"] = "file not found in raw/"
                    entry["retryCount"] = entry.get("retryCount", 0) + 1
                    rest.append(entry)
                    continue
                # Mark as processing
                entry["status"] = "processing"
                entry["startedAt"] = int(time.time() * 1000)
                wave_files.append((entry, fp))

            # Write intermediate state so wiki-monitor sees processing entries
            _write_queue(config, [e for e, _ in wave_files] + rest)

            if not wave_files:
                # All entries had missing files — don't re-loop immediately
                time.sleep(poll_interval)
                continue

            # Run batch pipeline on this wave
            raw_paths = [fp for _, fp in wave_files]
            try:
                results = batch_ingest(
                    raw_paths, config,
                    max_concurrent=max_concurrent,
                    verbose=verbose,
                )
            except ConversationPending:
                # The wave paused at an LLM handoff (prefetch or spine). Not a
                # failure — re-raise so main() returns 101; the agent answers the
                # prompt and re-invokes --watch, resuming this wave from cache.
                raise
            except (BatchCoordinatorBusy, SpineReservationConflict):
                # Coordination conflicts are not source failures and must not
                # consume every queued book's retry budget. Surface them to the
                # caller so the owning run/source can be resumed or inspected.
                raise
            except Exception as e:
                print(f"[watch] Batch ingest crashed: {e}")
                import traceback
                traceback.print_exc()
                # Mark all wave entries as failed
                for entry, fp in wave_files:
                    entry["status"] = "failed"
                    entry["error"] = f"batch crash: {e}"
                    entry["retryCount"] = entry.get("retryCount", 0) + 1
                    entry["failedAt"] = int(time.time() * 1000)
                    rest.append(entry)
                _write_queue(config, rest)
                total_failed += len(wave_files)
                total_processed += len(wave_files)
                time.sleep(poll_interval)
                continue

            # Map results back to queue entries by file path
            result_by_path: dict[str, dict] = {}
            for r in results:
                rf = r.get("raw_file", "")
                result_by_path[str(rf)] = r

            for entry, fp in wave_files:
                result = result_by_path.get(str(fp))
                # "skipped" (source page already exists / already complete) is a
                # successful outcome, not a failure — mark it done so the entry
                # doesn't burn retries on every cycle.
                if result and result.get("status") in ("ok", "skipped"):
                    entry["status"] = "done"
                    entry["completedAt"] = int(time.time() * 1000)
                    entry["error"] = None
                    total_done += 1
                elif result:
                    entry["status"] = "failed"
                    retries = entry.get("retryCount", 0) + 1
                    entry["retryCount"] = retries
                    err = result.get("error", "unknown") if result else "no result"
                    entry["error"] = str(err)[:200]
                    entry["failedAt"] = int(time.time() * 1000)
                    if retries >= max_retries:
                        print(f"  [watch] {entry['sourcePath']}: max retries ({max_retries}) reached — giving up")
                    total_failed += 1
                else:
                    # batch_ingest deliberately stops at the first failed
                    # serial-spine source. Missing results are later, unattempted
                    # books — keep them pending without burning a retry.
                    entry["status"] = "pending"
                    entry["error"] = "waiting behind earlier serial-spine failure"
                    entry.pop("startedAt", None)
                rest.append(entry)
                if result:
                    total_processed += 1

            _write_queue(config, rest)
            print(f"[watch] Cycle {cycle} complete — "
                  f"cumulative: {total_done} done, {total_failed} failed", flush=True)

    except KeyboardInterrupt:
        print(f"\n[watch] Interrupted. "
              f"Processed {total_processed}: {total_done} done, {total_failed} failed.")
        print(f"[watch] Queue preserved at {config.runtime_dir / 'ingest-queue.json'}")
    finally:
        fcntl.flock(watch_lock_fd, fcntl.LOCK_UN)
        os.close(watch_lock_fd)
