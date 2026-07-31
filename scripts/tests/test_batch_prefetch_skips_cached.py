"""Batch prefetch must scan past already-extracted leading sources.

Live reproduction (HardwareWiki, 2026-07-20): four resumed books had
``stage_1_3_done`` while the fifth source was fresh. The old adjacent-only
check never launched book 5 during book 1's 16-chunk Stage 2.2 spine, wasting
the entire overlap window.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _core  # noqa: E402
import ingest  # noqa: E402


def _make_config(tmp: Path) -> _core.Config:
    return _core.Config(
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


class TestBatchPrefetchSkipsCached(unittest.TestCase):
    def test_launches_first_future_source_without_extract_marker(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            files = [tmp / f"book-{i}.pdf" for i in range(5)]
            completed = {str(path) for path in files[:4]}
            launched = []

            orig_hash = ingest.file_sha256
            orig_done = ingest.is_stage_done
            orig_launch = ingest._launch_bg_extract
            ingest.file_sha256 = lambda path: str(path)
            ingest.is_stage_done = (
                lambda cfg_, source_id, stage:
                stage == "stage_1_3_done" and source_id in completed
            )
            ingest._launch_bg_extract = (
                lambda path, cfg_, state: launched.append(path)
            )
            try:
                selected = ingest._launch_next_pending_extract(
                    files, 0, cfg, {})
            finally:
                ingest.file_sha256 = orig_hash
                ingest.is_stage_done = orig_done
                ingest._launch_bg_extract = orig_launch

            self.assertEqual(selected, files[4])
            self.assertEqual(launched, [files[4]])

    def test_launches_at_most_one_source(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            files = [tmp / f"book-{i}.pdf" for i in range(3)]
            launched = []

            orig_hash = ingest.file_sha256
            orig_done = ingest.is_stage_done
            orig_launch = ingest._launch_bg_extract
            ingest.file_sha256 = lambda path: str(path)
            ingest.is_stage_done = lambda *_args: False
            ingest._launch_bg_extract = (
                lambda path, cfg_, state: launched.append(path)
            )
            try:
                selected = ingest._launch_next_pending_extract(
                    files, 1, cfg, {})
            finally:
                ingest.file_sha256 = orig_hash
                ingest.is_stage_done = orig_done
                ingest._launch_bg_extract = orig_launch

            self.assertEqual(selected, files[1])
            self.assertEqual(launched, [files[1]])


class TestPidAliveSandboxCompatibility(unittest.TestCase):
    def test_permission_denied_probe_is_treated_as_alive(self):
        with patch.object(
            ingest.os, "kill",
            side_effect=PermissionError("operation not permitted"),
        ):
            self.assertTrue(ingest._pid_alive(12345))

    def test_missing_process_is_not_alive(self):
        with patch.object(
            ingest.os, "kill",
            side_effect=ProcessLookupError("no such process"),
        ):
            self.assertFalse(ingest._pid_alive(12345))

    def test_zero_pid_is_not_alive_without_probe(self):
        with patch.object(ingest.os, "kill") as kill:
            self.assertFalse(ingest._pid_alive(0))
            kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
