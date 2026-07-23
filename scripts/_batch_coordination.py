"""Durable coordination primitives for conversation-driven ingestion.

``ProjectLock`` protects the physical write window, but conversation-mode LLM
handoffs intentionally terminate ``ingest.py`` with exit 101.  The kernel flock
therefore disappears while a source is still logically inside its Stage 2.3+
spine.  This module adds two small, separate protections:

* a transient coordinator flock, preventing two batch schedulers from editing
  ``batch-bg.json`` at the same time;
* a durable source reservation, preventing another source from entering the
  wiki-dependent spine while the current source is waiting on a handoff.

The durable reservation is source-bound, so re-invoking the same source after
exit 101 is allowed.  It is cleared only after a clean stop/skip/completion (or
an explicit maintenance action), never merely because a process exited.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from _paths import atomic_write


class SpineReservationConflict(RuntimeError):
    """A different source already owns the logical Stage 2.3+ spine."""


class BatchCoordinatorBusy(RuntimeError):
    """Another live scheduler already owns the transient coordinator slot."""


def _coordinator_lock_path(config) -> Path:
    return config.runtime_dir / "batch-coordinator.lock"


@contextmanager
def batch_coordinator_slot(config):
    """Allow only one live batch/watch coordinator invocation per project.

    The lock is intentionally transient: exit 101 releases it so the calling
    agent can re-invoke the same batch after answering a prompt.  Logical
    cross-handoff serialization is handled separately by the spine reservation.
    """
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    path = _coordinator_lock_path(config)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BatchCoordinatorBusy(
                "Another batch/watch coordinator invocation is already active "
                f"for this project ({path}). Wait for that invocation to yield "
                "or finish; do not start a duplicate scheduler."
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()}\n".encode())
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def spine_reservation_path(config) -> Path:
    return config.runtime_dir / "spine-reservation.json"


def load_spine_reservation(config) -> dict | None:
    path = spine_reservation_path(config)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"Spine reservation is unreadable: {path}: "
            f"{type(exc).__name__}: {exc}. Refusing to guess ownership."
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeError(
            f"Spine reservation is not a JSON object: {path}")
    return value


def reserve_spine(
    config,
    source_hash: str,
    source_path: str | Path,
    *,
    phase: str = "stage_2_3_plus",
) -> dict:
    """Reserve the logical wiki-dependent spine for ``source_hash``.

    Callers must hold ``ProjectLock`` while invoking this function.  Re-entry
    for the same source is allowed and refreshes diagnostics.  A different
    source is rejected even if the former process exited, because exit 101 is a
    normal handoff rather than completion.
    """
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    existing = load_spine_reservation(config)
    now = time.time()
    if existing:
        owner = str(existing.get("source_hash") or "")
        if owner and owner != source_hash:
            # A process may have completed finalization and then died before
            # removing its advisory reservation. The authoritative ``ingested``
            # marker makes that one stale case safe to reconcile automatically.
            from _core import load_stages
            if load_stages(config, owner).get("ingested"):
                spine_reservation_path(config).unlink(missing_ok=True)
                existing = None
                owner = ""
        if not existing:
            created_at = now
        elif owner and owner != source_hash:
            owner_path = existing.get("source_path") or "unknown source"
            owner_phase = existing.get("phase") or "unknown phase"
            raise SpineReservationConflict(
                "The wiki-dependent Stage 2.3+ spine is reserved by "
                f"{owner_path} ({owner[-8:]}, {owner_phase}). Resume or "
                "explicitly abandon that source before advancing another one."
            )
        else:
            created_at = existing.get("created_at", now)
    else:
        created_at = now

    value = {
        "schema": 1,
        "source_hash": source_hash,
        "source_path": str(source_path),
        "phase": phase,
        "pid": os.getpid(),
        "created_at": created_at,
        "updated_at": now,
    }
    atomic_write(
        spine_reservation_path(config),
        json.dumps(value, ensure_ascii=False, sort_keys=True),
    )
    return value


def refresh_spine_reservation(
    config,
    source_hash: str,
    *,
    phase: str,
) -> None:
    """Refresh diagnostics only when ``source_hash`` still owns the spine."""
    existing = load_spine_reservation(config)
    if not existing or existing.get("source_hash") != source_hash:
        return
    existing["phase"] = phase
    existing["pid"] = os.getpid()
    existing["updated_at"] = time.time()
    atomic_write(
        spine_reservation_path(config),
        json.dumps(existing, ensure_ascii=False, sort_keys=True),
    )


def release_spine_reservation(config, source_hash: str) -> bool:
    """Clear a reservation only when it belongs to ``source_hash``."""
    existing = load_spine_reservation(config)
    if not existing:
        return False
    if existing.get("source_hash") != source_hash:
        return False
    spine_reservation_path(config).unlink(missing_ok=True)
    return True


def prefetch_pause_path(config) -> Path:
    return config.runtime_dir / "batch-prefetch.pause"


def is_prefetch_paused(config) -> bool:
    return prefetch_pause_path(config).exists()


def write_prefetch_pause_marker(config, reason: str) -> None:
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(
        prefetch_pause_path(config),
        json.dumps(
            {"paused_at": time.time(), "reason": reason},
            ensure_ascii=False,
            sort_keys=True,
        ),
    )


def clear_prefetch_pause_marker(config) -> None:
    prefetch_pause_path(config).unlink(missing_ok=True)
