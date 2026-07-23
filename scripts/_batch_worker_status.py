"""Heartbeat/status reporting for detached batch extraction workers.

The batch coordinator survives conversation-mode handoffs by launching Phase
0/1 workers in detached sessions. A PID alone is not a sufficient identity:
``kill(pid, 0)`` can be denied by a sandbox, and a stale PID can be reused.
Each worker therefore owns an atomic status file containing a random token,
source hash, process-group identity, phase, heartbeat, and terminal result.

This module is deliberately small and wiki-independent so extraction stages can
publish phase transitions without importing the batch coordinator.
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from pathlib import Path

from _paths import atomic_write


_ACTIVE_REPORTER: "BatchWorkerReporter | None" = None
_ACTIVE_LOCK = threading.Lock()


def worker_lease_path(status_path: Path) -> Path:
    """Return the process-owned lease paired with one worker status file."""
    return Path(f"{status_path}.lease")


class BatchWorkerReporter:
    """Own one detached worker's status file and heartbeat thread."""

    def __init__(
        self,
        status_path: Path,
        token: str,
        source_path: Path,
        source_hash: str,
        heartbeat_interval: float = 5.0,
    ) -> None:
        self.status_path = Path(status_path)
        self.heartbeat_interval = max(0.2, float(heartbeat_interval))
        now = time.time()
        self._data = {
            "schema": 1,
            "token": token,
            "source_hash": source_hash,
            "source_path": str(source_path),
            "pid": os.getpid(),
            "pgid": os.getpgrp(),
            "status": "starting",
            "phase": "starting",
            "started_at": now,
            "heartbeat_at": now,
            "exit_code": None,
            "error": "",
        }
        self._data_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lease_path = worker_lease_path(self.status_path)
        self._lease_fd: int | None = None

    def _acquire_lease(self) -> None:
        """Hold an exclusive flock for the exact lifetime of this worker.

        A persisted token/PID only describes the historical worker; it cannot
        prove that a currently-live PID was not reused.  The random-token lease
        filename plus kernel-owned flock provides that missing live identity.
        """
        self.lease_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lease_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(fd, 0)
            os.write(
                fd,
                (
                    f"token={self._data['token']} "
                    f"source_hash={self._data['source_hash']} "
                    f"pid={self._data['pid']} pgid={self._data['pgid']}\n"
                ).encode(),
            )
        except Exception:
            os.close(fd)
            raise
        self._lease_fd = fd

    def _release_lease(self) -> None:
        fd = self._lease_fd
        self._lease_fd = None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _persist_locked(self) -> None:
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(
            self.status_path,
            json.dumps(self._data, ensure_ascii=False, sort_keys=True),
        )

    def start(self) -> None:
        global _ACTIVE_REPORTER
        self._acquire_lease()
        try:
            with _ACTIVE_LOCK:
                _ACTIVE_REPORTER = self
            self.update(status="running", phase="starting")
            self._thread = threading.Thread(
                target=self._heartbeat_loop,
                name="improved-wiki-batch-heartbeat",
                daemon=True,
            )
            self._thread.start()
        except Exception:
            with _ACTIVE_LOCK:
                if _ACTIVE_REPORTER is self:
                    _ACTIVE_REPORTER = None
            self._release_lease()
            raise

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval):
            try:
                self.update()
            except OSError as exc:
                # A transient rename/fsync failure must not permanently kill the
                # heartbeat thread and make a healthy OCR worker look stalled.
                print(
                    f"⚠️  [batch-worker] heartbeat write failed: {exc}",
                    flush=True,
                )

    def update(
        self,
        *,
        status: str | None = None,
        phase: str | None = None,
        exit_code: int | None = None,
        error: str | None = None,
    ) -> None:
        with self._data_lock:
            if status is not None:
                self._data["status"] = status
            if phase is not None:
                self._data["phase"] = phase
            if exit_code is not None:
                self._data["exit_code"] = int(exit_code)
            if error is not None:
                self._data["error"] = str(error)
            self._data["heartbeat_at"] = time.time()
            self._persist_locked()

    def finish(self, status: str, exit_code: int, error: str = "") -> None:
        global _ACTIVE_REPORTER
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(1.0, self.heartbeat_interval * 2))
        try:
            self.update(
                status=status,
                phase=status,
                exit_code=exit_code,
                error=error,
            )
        finally:
            with _ACTIVE_LOCK:
                if _ACTIVE_REPORTER is self:
                    _ACTIVE_REPORTER = None
            self._release_lease()


def update_worker_phase(phase: str) -> None:
    """Publish a phase transition when running inside a detached worker."""
    with _ACTIVE_LOCK:
        reporter = _ACTIVE_REPORTER
    if reporter is not None:
        try:
            reporter.update(phase=phase)
        except OSError as exc:
            # The heartbeat thread will retry. Extraction must not lose already
            # completed OCR chunks solely because one advisory phase write
            # collided with a transient filesystem error.
            print(f"⚠️  [batch-worker] status phase update failed: {exc}", flush=True)
