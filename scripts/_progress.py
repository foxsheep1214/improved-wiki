"""Persistent ingest state and locking.

This module owns cache files, per-source artifact/stage checkpoints, and the
project write-spine lock.  ``_core`` re-exports these names for compatibility;
new code should import them from here so state management has one clear owner.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from _paths import atomic_write

if TYPE_CHECKING:
    from _config import Config


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_cache(config: Config) -> dict:
    if config.cache_path.exists():
        try:
            return json.loads(config.cache_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(
                f"⚠️  [cache] {config.cache_path} corrupted "
                f"({type(e).__name__}: {e}) — discarding cache, "
                "will re-ingest from scratch."
            )
    return {"version": "2", "entries": {}}


def save_cache(config: Config, cache: dict) -> None:
    config.cache_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(config.cache_path, json.dumps(cache, ensure_ascii=False, indent=2))


_progress_thread_lock = threading.Lock()


def progress_path(config: Config, source_hash: str) -> Path:
    config.progress_dir.mkdir(parents=True, exist_ok=True)
    return config.progress_dir / f"{source_hash[:16]}.json"


def load_progress(config: Config, source_hash: str) -> dict | None:
    pp = progress_path(config, source_hash)
    if pp.exists():
        try:
            return json.loads(pp.read_text(encoding="utf-8"))
        except Exception as e:
            print(
                f"⚠️  [progress] {pp} corrupted ({type(e).__name__}: {e}) "
                "— discarding artifact cache, will re-derive."
            )
    return None


@contextmanager
def _progress_lock(config: Config, source_hash: str):
    """Serialize each source's checkpoint read-modify-write sequence.

    The process-local lock prevents self-deadlock between worker threads; flock
    then serializes independent processes.  Atomic rename alone prevents torn
    files but cannot prevent lost updates.
    """
    with _progress_thread_lock:
        config.progress_dir.mkdir(parents=True, exist_ok=True)
        lock_path = config.progress_dir / f"{source_hash[:16]}.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _read_progress_for_update(pp: Path) -> dict:
    if not pp.exists():
        return {}
    try:
        return json.loads(pp.read_text(encoding="utf-8"))
    except Exception as e:
        print(
            f"⚠️  [progress] {pp} corrupted ({type(e).__name__}: {e}) "
            "— discarding artifact cache, will rebuild from empty."
        )
        return {}


def save_progress(config: Config, source_hash: str, data: dict) -> None:
    """Merge-write artifact cache under the per-source lock."""
    with _progress_lock(config, source_hash):
        pp = progress_path(config, source_hash)
        existing = _read_progress_for_update(pp)
        existing.update(data)
        existing["_updated_at"] = int(time.time() * 1000)
        atomic_write(pp, json.dumps(existing, ensure_ascii=False, indent=2))


def delete_progress_keys(
    config: Config, source_hash: str, keys: list[str]
) -> None:
    """Remove artifact keys under the same lock used by merge writes."""
    with _progress_lock(config, source_hash):
        pp = progress_path(config, source_hash)
        if not pp.exists():
            return
        existing = _read_progress_for_update(pp)
        for key in keys:
            existing.pop(key, None)
        existing["_updated_at"] = int(time.time() * 1000)
        atomic_write(pp, json.dumps(existing, ensure_ascii=False, indent=2))


def clear_progress(config: Config, source_hash: str) -> None:
    pp = progress_path(config, source_hash)
    if pp.exists():
        pp.unlink()


def stages_path(config: Config, source_hash: str) -> Path:
    config.progress_dir.mkdir(parents=True, exist_ok=True)
    return config.progress_dir / f"{source_hash[:16]}.stages.json"


def load_stages(config: Config, source_hash: str) -> dict:
    sp = stages_path(config, source_hash)
    if sp.exists():
        try:
            return json.loads(sp.read_text(encoding="utf-8"))
        except Exception as e:
            print(
                f"⚠️  [stages] {sp} corrupted ({type(e).__name__}: {e}) "
                "— discarding stage progress, will re-run from start."
            )
    return {}


def mark_stage_done(
    config: Config,
    source_hash: str,
    stage: str,
    payload: dict | None = None,
) -> None:
    with _progress_lock(config, source_hash):
        stages = load_stages(config, source_hash)
        stages[stage] = int(time.time() * 1000)
        if payload:
            stages[f"{stage}__payload"] = payload
        atomic_write(
            stages_path(config, source_hash),
            json.dumps(stages, ensure_ascii=False, indent=2),
        )
    from _task_manifest import sync_task_manifest

    sync_task_manifest(config, source_hash)


def get_stage_payload(config: Config, source_hash: str, stage: str) -> dict:
    return load_stages(config, source_hash).get(f"{stage}__payload", {}) or {}


def unmark_stage_done(config: Config, source_hash: str, stage: str) -> None:
    with _progress_lock(config, source_hash):
        stages = load_stages(config, source_hash)
        payload_key = f"{stage}__payload"
        if stage not in stages and payload_key not in stages:
            return
        stages.pop(stage, None)
        stages.pop(payload_key, None)
        atomic_write(
            stages_path(config, source_hash),
            json.dumps(stages, ensure_ascii=False, indent=2),
        )
    from _task_manifest import sync_task_manifest

    sync_task_manifest(config, source_hash)


def is_stage_done(config: Config, source_hash: str, stage: str) -> bool:
    return bool(load_stages(config, source_hash).get(stage))


class ProjectLock:
    """Race-free project write-spine lock backed by ``fcntl.flock``."""

    def __init__(self, config: Config, owner_id: str = ""):
        config.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._lock_path = config.runtime_dir / "ingest.lock"
        self._owner = owner_id or str(os.getpid())
        self._fd: int | None = None

    def acquire(self, timeout: float = 0) -> bool:
        if self._fd is not None:
            return True
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if timeout and timeout > 0:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            os.close(fd)
                            return False
                        time.sleep(0.1)
            else:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                    return False
        except Exception:
            os.close(fd)
            raise
        os.ftruncate(fd, 0)
        os.write(
            fd,
            f"owner={self._owner} pid={os.getpid()}\n".encode(),
        )
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(
                "Another ingest is running on this project. "
                "Wait for its write spine to finish; inspect the flock holder "
                f"for {self._lock_path} rather than deleting the lock file."
            )
        return self

    def __exit__(self, *args):
        self.release()


__all__ = [
    "ProjectLock",
    "clear_progress",
    "delete_progress_keys",
    "file_sha256",
    "get_stage_payload",
    "is_stage_done",
    "load_cache",
    "load_progress",
    "load_stages",
    "mark_stage_done",
    "progress_path",
    "save_cache",
    "save_progress",
    "stages_path",
    "unmark_stage_done",
]
