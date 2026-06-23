"""Queue-based continuous ingestion (--watch daemon mode).

Extracted from ingest.py on 2026-06-23. Reads ingest-queue.json, feeds
pending entries through the batch pipeline, and updates their status.
wiki-monitor.sh adds files to the queue; ``ingest.py --watch`` consumes
them. ``batch_ingest`` is imported lazily because it lives in ingest.py
(which imports this module), breaking the cycle at runtime.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from _core import BATCH_MAX_CONCURRENT, Config, ProjectLock


def _read_queue(config: Config) -> list[dict]:
    """Read ingest-queue.json, returning entries sorted by addedAt (oldest first)."""
    qpath = config.runtime_dir / "ingest-queue.json"
    if not qpath.exists():
        return []
    try:
        queue = json.loads(qpath.read_text(encoding="utf-8"))
        if not isinstance(queue, list):
            return []
        # Sort: priority first, then oldest addedAt
        return sorted(queue, key=lambda e: (
            0 if e.get("priority") else 1,
            e.get("addedAt", 0),
        ))
    except Exception:
        return []


def _write_queue(config: Config, queue: list[dict]) -> None:
    """Atomically write ingest-queue.json."""
    qpath = config.runtime_dir / "ingest-queue.json"
    qpath.parent.mkdir(parents=True, exist_ok=True)
    tmp = qpath.with_suffix(".tmp")
    tmp.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(qpath)


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
      3. Feed them through the batch pipeline (parallel Stage 0-2, serial Stage 3+)
      4. Update queue status for each (done / failed / skipped)
      5. Re-scan for new entries added by wiki-monitor.sh
      6. If --drain: exit when queue is empty; otherwise loop forever

    This is the daemon mode: run it in a tmux/screen session or via nohup.
    wiki-monitor.sh (cron or manual) adds new files to the queue;
    ingest.py --watch picks them up in the next cycle.
    """
    from ingest import batch_ingest  # lazy: breaks ingest <-> _watch import cycle
    lock = ProjectLock(config, owner_id="watch")
    if not lock.acquire(timeout=10):
        raise RuntimeError(
            "Could not acquire project lock for watch mode. "
            "Is another ingest.py --watch or batch running?"
        )

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
                if result and result.get("status") == "ok":
                    entry["status"] = "done"
                    entry["completedAt"] = int(time.time() * 1000)
                    entry["error"] = None
                    total_done += 1
                else:
                    entry["status"] = "failed"
                    retries = entry.get("retryCount", 0) + 1
                    entry["retryCount"] = retries
                    err = result.get("error", "unknown") if result else "no result"
                    entry["error"] = str(err)[:200]
                    entry["failedAt"] = int(time.time() * 1000)
                    if retries >= max_retries:
                        print(f"  [watch] {entry['sourcePath']}: max retries ({max_retries}) reached — giving up")
                    total_failed += 1
                rest.append(entry)
                total_processed += 1

            _write_queue(config, rest)
            print(f"[watch] Cycle {cycle} complete — "
                  f"cumulative: {total_done} done, {total_failed} failed", flush=True)

    except KeyboardInterrupt:
        print(f"\n[watch] Interrupted. "
              f"Processed {total_processed}: {total_done} done, {total_failed} failed.")
        print(f"[watch] Queue preserved at {config.runtime_dir / 'ingest-queue.json'}")
    finally:
        lock.release()
