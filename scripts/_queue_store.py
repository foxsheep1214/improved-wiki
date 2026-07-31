"""Locked, atomic storage primitives for ``ingest-queue.json``."""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

from _paths import atomic_write


@contextmanager
def queue_lock(runtime_dir: Path, *, wait: bool = False):
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = runtime_dir / "ingest-queue.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    flags = fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB
    try:
        fcntl.flock(fd, flags)
    except BlockingIOError:
        os.close(fd)
        yield None
        return
    try:
        yield fd
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def load_queue(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"{path} is unreadable ({type(exc).__name__}: {exc}); "
            "it was not overwritten"
        ) from exc
    if not isinstance(value, list):
        raise RuntimeError(
            f"{path} must contain a JSON list; it was not overwritten"
        )
    return [entry for entry in value if isinstance(entry, dict)]


def save_queue(path: Path, queue: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(queue, ensure_ascii=False, indent=2))


def merge_entry_updates(
    current: list[dict],
    updates: list[dict],
) -> list[dict]:
    """Apply status updates without dropping entries appended concurrently."""
    def key(entry: dict) -> tuple[str, str]:
        source = str(entry.get("sourcePath") or "")
        if source:
            return ("source", source)
        return ("id", str(entry.get("id") or id(entry)))

    update_map = {key(entry): entry for entry in updates}
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for entry in current:
        entry_key = key(entry)
        merged.append(update_map.get(entry_key, entry))
        seen.add(entry_key)
    for entry in updates:
        entry_key = key(entry)
        if entry_key not in seen:
            merged.append(entry)
            seen.add(entry_key)
    return merged
