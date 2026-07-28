"""Read-only batch/prefetch/spine/handoff status reporting."""
from __future__ import annotations

import json
from pathlib import Path

from _config import Config
from _batch_coordination import load_spine_reservation, prefetch_pause_path
from _batch_supervisor import (
    _batch_pause_path,
    _load_bg_state,
    _safe_float,
    _safe_int,
    _worker_entry_phase,
    _worker_entry_state,
    _worker_lease_state,
)

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

    sources = []
    completed_source_prefixes: set[str] = set()
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
                # Conversation directories are keyed by the source hash's
                # final eight characters. Their tasks.json is append-only, so
                # superseded prompts may remain marked pending even after a
                # later answer completed the source. Once the authoritative
                # ingested marker exists, none of that directory's historical
                # tasks is an actionable handoff.
                completed_source_prefixes.add(source_hash[-8:])
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

    handoffs = []
    conversation_root = config.runtime_dir / "conversation"
    if conversation_root.exists():
        for manifest_path in sorted(conversation_root.glob("*/tasks.json")):
            if manifest_path.parent.name in completed_source_prefixes:
                continue
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
