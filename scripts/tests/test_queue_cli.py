from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import queue_cli  # noqa: E402
from _queue_store import merge_entry_updates  # noqa: E402


class QueueCliTests(unittest.TestCase):
    def test_scan_skips_cache_confirmed_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw"
            runtime = root / ".llm-wiki"
            raw.mkdir()
            runtime.mkdir()
            source = raw / "Book" / "a.pdf"
            source.parent.mkdir()
            source.write_bytes(b"same")
            source_hash = queue_cli._sha256(source)
            (runtime / "ingest-cache.json").write_text(
                json.dumps(
                    {
                        "entries": {
                            "Book/a.pdf": {"hash": source_hash},
                        }
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(queue_cli.scan_raw(raw, runtime), [])

    def test_changed_completed_entry_is_revived(self):
        queue = [
            {
                "sourcePath": "raw/Book/a.pdf",
                "status": "done",
                "hash": "old",
                "retryCount": 3,
                "error": "old failure",
            }
        ]
        changed = [
            {
                "rel": "Book/a.pdf",
                "hash": "new",
                "size": 12,
                "addedAt": 123,
            }
        ]
        merged, added, refreshed = queue_cli.merge_changed(queue, changed)
        self.assertEqual((added, refreshed), (0, 1))
        self.assertEqual(merged[0]["status"], "pending")
        self.assertEqual(merged[0]["hash"], "new")
        self.assertEqual(merged[0]["retryCount"], 0)
        self.assertIsNone(merged[0]["error"])

    def test_same_pending_entry_is_not_duplicated(self):
        queue = [
            {
                "sourcePath": "raw/Book/a.pdf",
                "status": "pending",
                "hash": "same",
            }
        ]
        changed = [
            {
                "rel": "Book/a.pdf",
                "hash": "same",
                "size": 12,
                "addedAt": 123,
            }
        ]
        merged, added, refreshed = queue_cli.merge_changed(queue, changed)
        self.assertEqual((len(merged), added, refreshed), (1, 0, 0))

    def test_priority_requires_file_under_raw_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw"
            raw.mkdir()
            outside = root / "outside.pdf"
            outside.write_bytes(b"x")
            with self.assertRaisesRegex(RuntimeError, "must be under"):
                queue_cli._priority_item(outside, raw)

    def test_corrupt_queue_is_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            queue_path = Path(directory) / "ingest-queue.json"
            queue_path.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "not overwritten"):
                queue_cli._load_queue(queue_path)
            self.assertEqual(
                queue_path.read_text(encoding="utf-8"),
                "{broken",
            )

    def test_status_update_preserves_concurrently_appended_entry(self):
        current = [
            {"sourcePath": "raw/a.pdf", "status": "processing"},
            {"sourcePath": "raw/new.pdf", "status": "pending"},
        ]
        updates = [{"sourcePath": "raw/a.pdf", "status": "done"}]
        merged = merge_entry_updates(current, updates)
        self.assertEqual(
            [(entry["sourcePath"], entry["status"]) for entry in merged],
            [
                ("raw/a.pdf", "done"),
                ("raw/new.pdf", "pending"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
