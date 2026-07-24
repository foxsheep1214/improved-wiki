#!/usr/bin/env python3
"""Tests for LanceDB post-rebuild maintenance."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_embeddings as embeddings  # noqa: E402


class _FakeTable:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def optimize(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error


class LanceDbMaintenanceTests(unittest.TestCase):
    def test_collect_pages_includes_methodology(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki = Path(tmp) / "wiki"
            page = wiki / "methodology" / "calibration.md"
            page.parent.mkdir(parents=True)
            page.write_text(
                "---\n"
                "type: methodology\n"
                'title: "Calibration Method"\n'
                "---\n\n"
                "# Calibration Method\n\nReusable procedure.\n",
                encoding="utf-8",
            )
            had_wiki = hasattr(embeddings, "WIKI")
            old_wiki = getattr(embeddings, "WIKI", None)
            embeddings.WIKI = str(wiki)
            try:
                pages = embeddings.collect_pages()
            finally:
                if had_wiki:
                    embeddings.WIKI = old_wiki
                else:
                    del embeddings.WIKI

            self.assertEqual(len(pages), 1)
            self.assertEqual(pages[0]["page_id"], "methodology/calibration")
            self.assertEqual(pages[0]["path"], "methodology/calibration.md")

    def test_compact_prunes_all_verified_old_versions(self):
        table = _FakeTable()

        embeddings._compact_and_prune_table(table)

        self.assertEqual(
            table.calls,
            [{
                "cleanup_older_than": timedelta(seconds=0),
                "delete_unverified": False,
            }],
        )

    def test_post_rebuild_maintenance_is_best_effort(self):
        table = _FakeTable(RuntimeError("maintenance unavailable"))
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            result = embeddings._best_effort_compact_and_prune(table)

        self.assertFalse(result)
        self.assertIn("current index remains usable", stdout.getvalue())

    def test_real_lancedb_compact_preserves_rows_and_prunes_snapshots(self):
        import lancedb

        with tempfile.TemporaryDirectory() as tmp:
            db = lancedb.connect(tmp)
            data = [
                {
                    "id": f"row-{i}",
                    "revision": 0,
                    "vector": [float(i), 1.0, 0.0, -1.0],
                }
                for i in range(64)
            ]
            table = None
            for revision in range(4):
                revision_data = [
                    {**row, "revision": revision}
                    for row in data
                ]
                table = db.create_table(
                    "wiki_chunks", revision_data, mode="overwrite"
                )

            table_dir = Path(tmp) / "wiki_chunks.lance"
            data_dir = table_dir / "data"
            files_before = len(list(data_dir.glob("*.lance")))
            self.assertGreater(files_before, 1)

            embeddings._compact_and_prune_table(table)

            reopened = db.open_table("wiki_chunks")
            self.assertEqual(reopened.count_rows(), len(data))
            files_after = len(list(data_dir.glob("*.lance")))
            self.assertLess(files_after, files_before)


if __name__ == "__main__":
    unittest.main()
