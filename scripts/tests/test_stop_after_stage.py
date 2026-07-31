"""Regression tests for --stop-after-stage Stage-0..2 in-prepare halting.

Historical bug (2026-06-25): ``--stop-after-stage 0`` was documented as the
"OCR-only then re-run" split, but the stop check lived AFTER ``_do_prepare``
returned — and ``_do_prepare`` runs all of Stage 0-2 (pausing at the 2.1/2.2/
2.4 LLM handoffs) before that check. So on a fresh run the flag was dead: the
process entered Stage 2.1 and exited 101 (ConversationPending) instead of
halting after extraction. Fix: raise ``PrepareStopAfter`` at the in-prepare
boundaries (0=extract, 1=global digest, 2=generation) so the flag actually
halts. ``ingest_one`` catches it and returns ``{"status":"ok",
"stopped_after":stage}``.

Stdlib unittest only — no pytest, no network, no LLM calls.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _core import PrepareStopAfter  # noqa: E402
from _ingest_prepare import _stage_2_2_only_requested  # noqa: E402
from _ingest_skip import _stop_after_stage  # noqa: E402


class _FakeConfig:
    """Config stand-in: stop_after_stage is set dynamically in real Config."""


class StopAfterStageCheck(unittest.TestCase):
    """_stop_after_stage pure check (exact-match, getattr-safe)."""

    def test_returns_true_on_exact_match(self):
        cfg = _FakeConfig()
        cfg.stop_after_stage = "0"
        self.assertTrue(_stop_after_stage(cfg, "0"))

    def test_returns_false_on_mismatch(self):
        cfg = _FakeConfig()
        cfg.stop_after_stage = "2"
        self.assertFalse(_stop_after_stage(cfg, "0"))
        self.assertFalse(_stop_after_stage(cfg, "1"))

    def test_returns_false_when_attr_absent(self):
        # Config instances built outside ingest.py arg parsing have no
        # stop_after_stage attribute — must not raise.
        cfg = _FakeConfig()
        self.assertFalse(_stop_after_stage(cfg, "0"))
        self.assertFalse(_stop_after_stage(cfg, "1"))

    def test_does_not_print(self):
        # The helper is a pure check; the raise site owns the message so it
        # isn't duplicated. Capture stdout to verify silence.
        import io
        import contextlib
        cfg = _FakeConfig()
        cfg.stop_after_stage = "1"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _stop_after_stage(cfg, "1")
            _stop_after_stage(cfg, "0")
        self.assertEqual(buf.getvalue(), "")


class PrepareStopAfterSignal(unittest.TestCase):
    """PrepareStopAfter carries the stage and subclasses BaseException."""

    def test_carries_stage_attribute(self):
        exc = PrepareStopAfter("0")
        self.assertEqual(exc.stage, "0")

    def test_subclasses_base_exception_not_exception(self):
        # Must subclass BaseException (not Exception) so the broad
        # ``except Exception`` in _do_prepare (which prints FAILED + traceback
        # and re-raises) does not noisy-up a clean intentional stop.
        self.assertTrue(issubclass(PrepareStopAfter, BaseException))
        self.assertFalse(issubclass(PrepareStopAfter, Exception))

    def test_can_be_caught_specifically(self):
        # ingest_one catches PrepareStopAfter specifically; ensure it is not
        # swallowed by a generic ``except Exception`` block.
        with self.assertRaises(PrepareStopAfter):
            try:
                raise PrepareStopAfter("2")
            except Exception:  # noqa: BLE001 — simulates _do_prepare's broad except
                self.fail("PrepareStopAfter must NOT be caught by `except Exception`")


class Stage22OnlyBoundary(unittest.TestCase):
    """Single-book --stop-after-stage=1.5 must not enter Stage 2.3/2.4."""

    def test_batch_prefetch_requests_analysis_only(self):
        cfg = _FakeConfig()
        self.assertTrue(_stage_2_2_only_requested(cfg, True))

    def test_single_book_stop_1_5_requests_analysis_only(self):
        cfg = _FakeConfig()
        cfg.stop_after_stage = "1.5"
        self.assertTrue(_stage_2_2_only_requested(cfg, False))

    def test_normal_ingest_runs_wiki_dependent_tail(self):
        cfg = _FakeConfig()
        cfg.stop_after_stage = None
        self.assertFalse(_stage_2_2_only_requested(cfg, False))


if __name__ == "__main__":
    unittest.main()
