"""Regression tests for the batch worker supervisor and lock boundaries."""
from __future__ import annotations

import json
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _core  # noqa: E402
import ingest  # noqa: E402
from _batch_coordination import (  # noqa: E402
    SpineReservationConflict,
    batch_coordinator_slot,
    release_spine_reservation,
    reserve_spine,
    write_prefetch_pause_marker,
)
from _batch_worker_status import (  # noqa: E402
    BatchWorkerReporter,
    worker_lease_path,
)


def _make_config(tmp: Path) -> _core.Config:
    tmp = tmp.resolve()
    config = _core.Config(
        wiki_root=tmp, raw_root=tmp / "raw",
        wiki_dir=tmp / "wiki", runtime_dir=tmp / "rt",
        cache_path=tmp / "rt" / "ingest-cache.json",
        progress_dir=tmp / "rt" / "ingest-progress",
        extract_tmp_dir=tmp / "rt" / "extract-tmp",
        llm_model="m",
        caption_api_key="", caption_base_url="x",
        caption_model="c", chunk_overlap=3000,
        source_budget=100000, target_chars=60000, target_tokens=30000,
        max_tokens=8192, conversation_prefix="",
    )
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    return config


def _write_status(
    path: Path,
    token: str,
    source_hash: str,
    *,
    status: str = "running",
    phase: str = "starting",
    heartbeat_at: float | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "token": token,
        "source_hash": source_hash,
        "status": status,
        "phase": phase,
        "heartbeat_at": time.time() if heartbeat_at is None else heartbeat_at,
    }), encoding="utf-8")


class WorkerHealthTests(unittest.TestCase):
    def test_fresh_heartbeat_overrides_eperm_probe(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            status_path = tmp / "worker.json"
            _write_status(status_path, "tok", "hash")
            entry = {
                "pid": 123, "token": "tok", "source_hash": "hash",
                "status_file": str(status_path), "launched_at": time.time(),
            }
            with patch.object(ingest, "_pid_probe", return_value="unknown"):
                self.assertEqual(ingest._worker_entry_state(entry), "running")

    def test_stale_heartbeat_plus_eperm_is_stalled_not_alive_forever(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            status_path = tmp / "worker.json"
            _write_status(
                status_path, "tok", "hash",
                heartbeat_at=time.time() - ingest._BG_HEARTBEAT_STALE_SECONDS - 1,
            )
            entry = {
                "pid": 123, "token": "tok", "source_hash": "hash",
                "status_file": str(status_path), "launched_at": time.time() - 120,
            }
            with patch.object(ingest, "_pid_probe", return_value="unknown"):
                self.assertEqual(ingest._worker_entry_state(entry), "stalled")

    def test_malformed_worker_identity_is_stale_not_a_crash(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            status_path = tmp / "worker.json"
            _write_status(
                status_path, "tok", "hash",
                heartbeat_at=time.time() - 120,
            )
            data = json.loads(status_path.read_text(encoding="utf-8"))
            data["pid"] = "not-a-pid"
            status_path.write_text(json.dumps(data), encoding="utf-8")
            entry = {
                "pid": 123, "token": "tok", "source_hash": "hash",
                "status_file": str(status_path), "launched_at": "also-invalid",
            }
            with patch.object(ingest, "_pid_probe", return_value="unknown"):
                self.assertEqual(ingest._worker_entry_state(entry), "stalled")

    def test_dead_pid_overrides_recent_heartbeat(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            status_path = tmp / "worker.json"
            _write_status(status_path, "tok", "hash")
            entry = {
                "pid": 123, "token": "tok", "source_hash": "hash",
                "status_file": str(status_path), "launched_at": time.time(),
            }
            with patch.object(ingest, "_pid_probe", return_value="dead"):
                self.assertEqual(ingest._worker_entry_state(entry), "dead")

    def test_terminal_failure_is_reported_without_sleeping(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            status_path = tmp / "worker.json"
            _write_status(
                status_path, "tok", "hash",
                status="failed", phase="failed",
            )
            entry = {
                "pid": 123, "token": "tok", "source_hash": "hash",
                "status_file": str(status_path),
            }
            with (
                patch.object(ingest, "is_stage_done", return_value=False),
                patch.object(ingest.time, "sleep") as sleep,
            ):
                self.assertFalse(
                    ingest._wait_extract_done(cfg, "hash", bg_entry=entry))
                sleep.assert_not_called()

    def test_verified_worker_termination_targets_process_group(self):
        entry = {
            "pid": 12345,
            "pgid": 12345,
            "token": "tok",
            "launched_at": time.time(),
            "file": "book.pdf",
        }
        with (
            patch.object(ingest, "_worker_lease_state", return_value="held"),
            patch.object(
                ingest,
                "_read_worker_status",
                return_value={"status": "running"},
            ),
            patch.object(ingest.os, "killpg") as killpg,
        ):
            self.assertTrue(
                ingest._terminate_bg_worker(entry, grace_seconds=0))
        killpg.assert_called_once_with(12345, signal.SIGTERM)

    def test_free_worker_lease_blocks_signal_to_reused_pid(self):
        entry = {
            "pid": 12345,
            "pgid": 12345,
            "token": "tok",
            "lease_file": "/tmp/old-worker.lease",
            "file": "book.pdf",
        }
        with (
            patch.object(ingest, "_worker_lease_state", return_value="free"),
            patch.object(ingest.os, "killpg") as killpg,
        ):
            self.assertFalse(
                ingest._terminate_bg_worker(entry, grace_seconds=0))
        killpg.assert_not_called()

    def test_held_lease_without_matching_status_still_blocks_signal(self):
        entry = {
            "pid": 12345,
            "pgid": 12345,
            "token": "tok",
            "lease_file": "/tmp/current-worker.lease",
            "file": "book.pdf",
        }
        with (
            patch.object(ingest, "_worker_lease_state", return_value="held"),
            patch.object(ingest, "_read_worker_status", return_value=None),
            patch.object(ingest.os, "killpg") as killpg,
        ):
            self.assertFalse(
                ingest._terminate_bg_worker(entry, grace_seconds=0))
        killpg.assert_not_called()

    def test_free_worker_lease_overrides_live_reused_pid(self):
        entry = {
            "pid": 12345,
            "token": "tok",
            "lease_file": "/tmp/old-worker.lease",
        }
        with (
            patch.object(ingest, "_worker_lease_state", return_value="free"),
            patch.object(ingest, "_pid_probe", return_value="alive"),
        ):
            self.assertEqual(ingest._worker_entry_state(entry), "dead")


class WorkerReporterTests(unittest.TestCase):
    def test_reporter_writes_identity_phase_and_terminal_status(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            status_path = tmp / "status.json"
            reporter = BatchWorkerReporter(
                status_path, "tok", tmp / "book.pdf", "hash",
                heartbeat_interval=0.01,
            )
            reporter.start()
            entry = {
                "pid": reporter._data["pid"],
                "token": "tok",
                "source_hash": "hash",
                "status_file": str(status_path),
                "lease_file": str(worker_lease_path(status_path)),
            }
            self.assertEqual(ingest._worker_lease_state(entry), "held")
            reporter.update(phase="mineru")
            reporter.finish("completed", 0)
            self.assertEqual(ingest._worker_lease_state(entry), "free")
            data = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(data["token"], "tok")
            self.assertEqual(data["source_hash"], "hash")
            self.assertEqual(data["status"], "completed")
            self.assertEqual(data["phase"], "completed")
            self.assertEqual(data["exit_code"], 0)


class PrefetchSchedulerTests(unittest.TestCase):
    def test_prefetch_pause_starts_no_new_worker_and_wait_yields(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            raw = tmp / "book.pdf"
            write_prefetch_pause_marker(cfg, "test")
            with (
                patch.object(ingest, "_launch_bg_extract") as launch,
                patch.object(ingest, "is_stage_done", return_value=False),
            ):
                self.assertEqual(
                    ingest._fill_prefetch_slots(
                        [raw], 0, cfg, {}, 4),
                    [],
                )
                launch.assert_not_called()
                with self.assertRaises(ingest.BatchPrefetchPaused):
                    ingest._wait_extract_done(
                        cfg,
                        "hash",
                        bg_entry={"pid": 123},
                    )

    def test_two_stage_slots_preserve_source_order(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            files = [tmp / f"book-{i}.pdf" for i in range(3)]
            launched: list[Path] = []
            completed: set[str] = set()

            def fake_launch(path, _cfg, state):
                source_hash = str(path)
                token = f"tok-{len(launched)}"
                status_path = tmp / f"status-{len(launched)}.json"
                _write_status(status_path, token, source_hash, phase="starting")
                state[source_hash] = {
                    "pid": 1000 + len(launched),
                    "pgid": 1000 + len(launched),
                    "token": token,
                    "source_hash": source_hash,
                    "status_file": str(status_path),
                    "launched_at": time.time(),
                    "file": path.name,
                }
                launched.append(path)
                return True

            with (
                patch.object(ingest, "file_sha256", side_effect=lambda p: str(p)),
                patch.object(
                    ingest, "is_stage_done",
                    side_effect=lambda _c, h, stage:
                    stage == "stage_1_3_done" and h in completed,
                ),
                patch.object(ingest, "_launch_bg_extract", side_effect=fake_launch),
                patch.object(ingest, "_pid_probe", return_value="alive"),
            ):
                ingest._fill_prefetch_slots(files, 0, cfg, {}, 4)
                self.assertEqual(launched, [files[0]])

                state = {}
                launched.clear()
                fake_launch(files[0], cfg, state)
                first = state[str(files[0])]
                _write_status(
                    Path(first["status_file"]), first["token"], str(files[0]),
                    phase="mineru",
                )
                ingest._fill_prefetch_slots(files, 0, cfg, state, 4)
                self.assertEqual(launched, [files[0], files[1]])

                second = state[str(files[1])]
                _write_status(
                    Path(second["status_file"]), second["token"], str(files[1]),
                    phase="mineru",
                )
                completed.add(str(files[0]))
                ingest._fill_prefetch_slots(files, 0, cfg, state, 4)
                self.assertEqual(launched, [files[0], files[1], files[2]])


class SpineLockScopeTests(unittest.TestCase):
    def test_project_lock_is_not_held_during_stage22_prefetch(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            raw = tmp / "raw" / "Book" / "a.pdf"
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_bytes(b"%PDF fake")
            held = {"value": False}
            calls: list[str] = []

            class FakeLock:
                def __init__(self, *_args, **_kwargs):
                    pass

                def acquire(self):
                    self.assert_not_held()
                    held["value"] = True
                    calls.append("acquire")
                    return True

                @staticmethod
                def assert_not_held():
                    if held["value"]:
                        raise AssertionError("lock already held")

                def release(self):
                    held["value"] = False
                    calls.append("release")

            def fake_prepare(_f, _cfg, _type, _verbose, *rest):
                if rest:
                    self.assertFalse(held["value"])
                    calls.append("stage22")
                    raise ingest.PrepareStopAfter("1.5")
                self.assertTrue(held["value"])
                calls.append("spine")
                return None

            with (
                patch.object(
                    ingest, "is_stage_done",
                    side_effect=lambda _c, _h, stage: stage == "stage_1_3_done",
                ),
                patch.object(ingest, "_do_prepare", side_effect=fake_prepare),
                patch.object(ingest, "ProjectLock", FakeLock),
            ):
                ingest.batch_ingest([raw], cfg)

            self.assertEqual(calls, ["stage22", "acquire", "spine", "release"])

    def test_single_source_prefetch_runs_before_project_lock(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            raw = tmp / "raw" / "Book" / "a.pdf"
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_bytes(b"%PDF fake")
            held = {"value": False}
            calls: list[str] = []

            class FakeLock:
                def __init__(self, *_args, **_kwargs):
                    pass

                def acquire(self):
                    self.assert_not_held()
                    held["value"] = True
                    calls.append("acquire")
                    return True

                @staticmethod
                def assert_not_held():
                    if held["value"]:
                        raise AssertionError("lock already held")

                def release(self):
                    held["value"] = False
                    calls.append("release")

            def fake_prefetch(*_args, **_kwargs):
                self.assertFalse(held["value"])
                calls.append("prefetch")
                raise ingest.PrepareStopAfter("1.5")

            def fake_ingest(*_args, **_kwargs):
                self.assertTrue(held["value"])
                calls.append("spine")
                return {"status": "skipped"}

            argv = ["ingest.py", str(raw)]
            with (
                patch.object(sys, "argv", argv),
                patch.object(ingest.Config, "from_env", return_value=cfg),
                patch.object(ingest, "_probe_and_apply_context"),
                patch.object(ingest, "_do_prepare", side_effect=fake_prefetch),
                patch.object(ingest, "ingest_one", side_effect=fake_ingest),
                patch.object(ingest, "ProjectLock", FakeLock),
            ):
                self.assertEqual(ingest.main(), 0)

            self.assertEqual(
                calls, ["prefetch", "acquire", "spine", "release"])

    def test_spine_failure_aborts_before_later_book(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            files = [
                tmp / "raw" / "Book" / "a.pdf",
                tmp / "raw" / "Book" / "b.pdf",
            ]
            files[0].parent.mkdir(parents=True, exist_ok=True)
            files[0].write_bytes(b"a")
            files[1].write_bytes(b"b")
            spine_calls: list[Path] = []

            def fake_prepare(path, _cfg, _type, _verbose, *rest):
                if rest:
                    raise ingest.PrepareStopAfter("1.5")
                spine_calls.append(path)
                return {
                    "raw_file": path,
                    "config": cfg,
                    "h": ingest._batch_source_hash(path),
                }

            with (
                patch.object(
                    ingest, "is_stage_done",
                    side_effect=lambda _c, _h, stage:
                    stage == "stage_1_3_done",
                ),
                patch.object(ingest, "_do_prepare", side_effect=fake_prepare),
                patch.object(
                    ingest,
                    "_do_write",
                    side_effect=RuntimeError("partial mutation"),
                ),
            ):
                results = ingest.batch_ingest(files, cfg)

            self.assertEqual(spine_calls, [files[0]])
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["status"], "failed")
            self.assertIn("partial mutation", results[0]["error"])


class CoordinationTests(unittest.TestCase):
    def test_only_one_live_batch_coordinator(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            with batch_coordinator_slot(cfg):
                with self.assertRaisesRegex(
                        RuntimeError, "Another batch/watch coordinator"):
                    with batch_coordinator_slot(cfg):
                        pass

    def test_spine_reservation_reenters_same_source_but_blocks_other(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            first = "a" * 64
            second = "b" * 64
            reserve_spine(cfg, first, "first.pdf")
            reserve_spine(cfg, first, "first.pdf", phase="waiting_handoff")
            with self.assertRaises(SpineReservationConflict):
                reserve_spine(cfg, second, "second.pdf")
            self.assertTrue(release_spine_reservation(cfg, first))
            reserve_spine(cfg, second, "second.pdf")
            self.assertTrue(release_spine_reservation(cfg, second))

    def test_reserved_source_must_be_first_unfinished_batch_book(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            first = tmp / "first.pdf"
            owner_file = tmp / "owner.pdf"
            first.write_bytes(b"first")
            owner_file.write_bytes(b"owner")
            owner_hash = ingest._batch_source_hash(owner_file)
            reserve_spine(cfg, owner_hash, owner_file)
            try:
                with self.assertRaisesRegex(
                        SpineReservationConflict, "first unfinished"):
                    ingest._assert_batch_resume_order(
                        [first, owner_file], cfg)
                ingest._assert_batch_resume_order(
                    [owner_file, first], cfg)
                with self.assertRaisesRegex(
                        SpineReservationConflict, "not present"):
                    ingest._assert_batch_resume_order([first], cfg)
            finally:
                release_spine_reservation(cfg, owner_hash)


class BatchCliGuardTests(unittest.TestCase):
    def test_multi_source_stop_after_is_rejected_before_context_probe(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            files = [
                tmp / "raw" / "Book" / "a.pdf",
                tmp / "raw" / "Book" / "b.pdf",
            ]
            files[0].parent.mkdir(parents=True, exist_ok=True)
            for path in files:
                path.write_bytes(b"%PDF fake")
            argv = [
                "ingest.py", "--stop-after-stage", "0",
                str(files[0]), str(files[1]),
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(ingest.Config, "from_env", return_value=cfg),
                patch.object(ingest, "_probe_and_apply_context") as probe,
            ):
                self.assertEqual(ingest.main(), 2)
                probe.assert_not_called()

    def test_pause_prefetch_is_first_class_and_does_not_full_pause(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            argv = ["ingest.py", "--pause-prefetch"]
            with (
                patch.object(sys, "argv", argv),
                patch.object(ingest.Config, "from_env", return_value=cfg),
                patch.object(
                    ingest, "_pause_batch_workers", return_value=0),
                patch.object(ingest, "_probe_and_apply_context") as probe,
            ):
                self.assertEqual(ingest.main(), 0)
                probe.assert_not_called()
            self.assertTrue(
                (cfg.runtime_dir / "batch-prefetch.pause").exists())
            self.assertFalse((cfg.runtime_dir / "batch.pause").exists())

    def test_batch_status_is_read_only_first_class_action(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            argv = ["ingest.py", "--batch-status"]
            with (
                patch.object(sys, "argv", argv),
                patch.object(ingest.Config, "from_env", return_value=cfg),
                patch.object(ingest, "_probe_and_apply_context") as probe,
            ):
                self.assertEqual(ingest.main(), 0)
                probe.assert_not_called()

    def test_full_pause_blocks_single_source_continuation(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            raw = tmp / "raw" / "Book" / "a.pdf"
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_bytes(b"%PDF fake")
            ingest._write_batch_pause_marker(cfg, "test full pause")
            argv = ["ingest.py", str(raw)]
            with (
                patch.object(sys, "argv", argv),
                patch.object(ingest.Config, "from_env", return_value=cfg),
                patch.object(ingest, "_probe_and_apply_context") as probe,
                patch.object(ingest, "_do_prepare") as prepare,
            ):
                self.assertEqual(ingest.main(), 75)
                probe.assert_not_called()
                prepare.assert_not_called()

    def test_no_project_lock_requires_prefetch_stop_boundary(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            raw = tmp / "raw" / "Book" / "a.pdf"
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_bytes(b"%PDF fake")
            argv = ["ingest.py", "--no-project-lock", str(raw)]
            with (
                patch.object(sys, "argv", argv),
                patch.object(ingest.Config, "from_env", return_value=cfg),
                patch.object(ingest, "_probe_and_apply_context") as probe,
            ):
                self.assertEqual(ingest.main(), 2)
                probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
