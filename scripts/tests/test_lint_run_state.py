"""Tests for durable logical-run checkpoints used by ``wiki-lint.sh``."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _lint_run_state as state  # noqa: E402


class TestLintRunState(unittest.TestCase):
    def test_begin_resumes_until_finish(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "lint-run-state.json"
            first = state.begin(path)
            state.mark_done(path, "semantic")
            resumed = state.begin(path)

            self.assertEqual(resumed["run_id"], first["run_id"])
            self.assertTrue(state.is_done(path, "semantic"))
            self.assertFalse(state.is_done(path, "sweep"))

            state.finish(path)
            self.assertFalse(path.exists())
            fresh = state.begin(path)
            self.assertNotEqual(fresh["run_id"], first["run_id"])

    def test_mark_done_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "lint-run-state.json"
            state.begin(path)
            state.mark_done(path, "semantic")
            state.mark_done(path, "semantic")
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["completed_stages"], ["semantic"])

    def test_corrupt_state_starts_fresh(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "lint-run-state.json"
            path.write_text("{broken", encoding="utf-8")
            recovered = state.begin(path)
            self.assertTrue(recovered["run_id"])
            self.assertEqual(recovered["completed_stages"], [])

    def test_mark_requires_an_active_run(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "missing.json"
            with self.assertRaises(RuntimeError):
                state.mark_done(path, "semantic")

    def test_cli_reset_removes_related_sweep_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lint_state = root / "lint-run-state.json"
            sweep_state = root / "review-sweep-run.json"
            state.begin(lint_state)
            sweep_state.write_text("{}", encoding="utf-8")

            rc = state.main([
                "reset",
                str(lint_state),
                "--related-state",
                str(sweep_state),
            ])
            self.assertEqual(rc, 0)
            self.assertFalse(lint_state.exists())
            self.assertFalse(sweep_state.exists())

    def test_idle_run_is_discarded_with_related_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lint_state = root / "lint-run-state.json"
            sweep_state = root / "review-sweep-run.json"
            old = state.begin(lint_state)
            state.mark_done(lint_state, "semantic")
            payload = json.loads(lint_state.read_text(encoding="utf-8"))
            payload["updated_at"] = "2026-01-01T00:00:00+00:00"
            lint_state.write_text(json.dumps(payload), encoding="utf-8")
            sweep_state.write_text("{}", encoding="utf-8")

            fresh = state.begin(lint_state, related=(sweep_state,))
            self.assertNotEqual(fresh["run_id"], old["run_id"])
            self.assertFalse(state.is_done(lint_state, "semantic"))
            self.assertFalse(sweep_state.exists())

    def test_long_run_that_keeps_resuming_is_not_discarded(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "lint-run-state.json"
            first = state.begin(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["started_at"] = "2026-01-01T00:00:00+00:00"
            path.write_text(json.dumps(payload), encoding="utf-8")
            resumed = state.begin(path)
            self.assertEqual(resumed["run_id"], first["run_id"])


if __name__ == "__main__":
    unittest.main()
