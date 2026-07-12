"""_mineru_stats.json crash-recovery loading (2026-07-12 hardening).

A corrupt stats file is the sanctioned "loud warning + reset" case: chunks
re-run instead of the whole ingest dying on its own recovery file. Older
schemas missing keys are backfilled via setdefault so chunk bookkeeping
(completed_chunks / failed_chunks / images) never KeyErrors.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _stage_1_1_scanned import _stage_1_1_scanned_load_stats  # noqa: E402

_EMPTY = {"completed_chunks": [], "failed_chunks": [], "images": {}}


class TestLoadMineruStats(unittest.TestCase):
    def test_missing_file_inits_empty_schema(self):
        with tempfile.TemporaryDirectory() as d:
            stats, stats_path = _stage_1_1_scanned_load_stats(Path(d))
            self.assertEqual(stats, _EMPTY)
            self.assertEqual(stats_path, Path(d) / "_mineru_stats.json")

    def test_corrupt_json_resets_to_empty(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "_mineru_stats.json").write_text("{not json", encoding="utf-8")
            stats, _ = _stage_1_1_scanned_load_stats(Path(d))
            self.assertEqual(stats, _EMPTY)

    def test_non_dict_json_resets_to_empty(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "_mineru_stats.json").write_text("[1, 2, 3]", encoding="utf-8")
            stats, _ = _stage_1_1_scanned_load_stats(Path(d))
            self.assertEqual(stats, _EMPTY)

    def test_old_schema_missing_keys_backfilled(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "_mineru_stats.json").write_text(
                json.dumps({"completed_chunks": ["0-32"]}), encoding="utf-8")
            stats, _ = _stage_1_1_scanned_load_stats(Path(d))
            self.assertEqual(stats["completed_chunks"], ["0-32"])
            self.assertEqual(stats["failed_chunks"], [])
            self.assertEqual(stats["images"], {})

    def test_valid_stats_pass_through(self):
        with tempfile.TemporaryDirectory() as d:
            original = {"completed_chunks": ["0-32", "32-64"],
                        "failed_chunks": [{"chunk": "64-96", "error": "boom"}],
                        "images": {"0": ["a.jpg"]}}
            (Path(d) / "_mineru_stats.json").write_text(
                json.dumps(original), encoding="utf-8")
            stats, _ = _stage_1_1_scanned_load_stats(Path(d))
            self.assertEqual(stats, original)


if __name__ == "__main__":
    unittest.main()
