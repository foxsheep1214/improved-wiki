"""Regression tests for ingest CLI progress visibility."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _ingest_cli  # noqa: E402


class _ReconfigurableStream:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def reconfigure(self, **kwargs) -> None:
        self.calls.append(kwargs)


class _UnsupportedStream:
    pass


class TestLineBufferedOutput(unittest.TestCase):
    def test_configures_stdout_and_stderr(self):
        stdout = _ReconfigurableStream()
        stderr = _ReconfigurableStream()
        original_stdout, original_stderr = sys.stdout, sys.stderr
        try:
            sys.stdout, sys.stderr = stdout, stderr
            _ingest_cli._configure_line_buffered_output()
        finally:
            sys.stdout, sys.stderr = original_stdout, original_stderr

        self.assertEqual(stdout.calls, [{"line_buffering": True}])
        self.assertEqual(stderr.calls, [{"line_buffering": True}])

    def test_unsupported_test_streams_are_safe(self):
        original_stdout, original_stderr = sys.stdout, sys.stderr
        try:
            sys.stdout = _UnsupportedStream()
            sys.stderr = _UnsupportedStream()
            _ingest_cli._configure_line_buffered_output()
        finally:
            sys.stdout, sys.stderr = original_stdout, original_stderr


if __name__ == "__main__":
    unittest.main()
