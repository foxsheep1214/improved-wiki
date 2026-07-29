#!/usr/bin/env python3
"""Tests for page-scoped embedding deletion."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _embedding_store import page_id_from_path, remove_page_embeddings  # noqa: E402


class EmbeddingStoreTests(unittest.TestCase):
    def test_page_id_is_wiki_relative_and_path_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                page_id_from_path(root, "concepts/shared.md"),
                "concepts/shared",
            )
            self.assertEqual(
                page_id_from_path(root, "wiki/entities/shared.md"),
                "entities/shared",
            )
            with self.assertRaises(ValueError):
                page_id_from_path(root, "../outside.md")

    def test_delete_removes_only_named_page_rows(self):
        import lancedb

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lance_dir = root / ".llm-wiki" / "lancedb"
            lance_dir.mkdir(parents=True)
            db = lancedb.connect(str(lance_dir))
            rows = [
                {
                    "chunk_id": "concepts/a#0",
                    "page_id": "concepts/a",
                    "vector": [1.0, 0.0, 0.0],
                },
                {
                    "chunk_id": "concepts/a#1",
                    "page_id": "concepts/a",
                    "vector": [0.9, 0.1, 0.0],
                },
                {
                    "chunk_id": "entities/a#0",
                    "page_id": "entities/a",
                    "vector": [0.0, 1.0, 0.0],
                },
            ]
            db.create_table("wiki_chunks", rows)

            result = remove_page_embeddings(
                root, ["concepts/a.md"], strict=True
            )

            table = db.open_table("wiki_chunks")
            self.assertTrue(result["index_present"])
            self.assertEqual(result["matched_pages"], 1)
            self.assertEqual(result["rows_removed"], 2)
            self.assertEqual(table.count_rows("page_id = 'concepts/a'"), 0)
            self.assertEqual(table.count_rows("page_id = 'entities/a'"), 1)

    def test_missing_index_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = remove_page_embeddings(
                Path(tmp), ["concepts/a.md"], strict=True
            )
            self.assertFalse(result["index_present"])
            self.assertEqual(result["rows_removed"], 0)


if __name__ == "__main__":
    unittest.main()
