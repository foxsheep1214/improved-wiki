#!/usr/bin/env python3
"""Tests for LanceDB post-rebuild maintenance."""

from __future__ import annotations

import io
from types import SimpleNamespace
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock


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


class EmbeddingResponseContractTests(unittest.TestCase):
    def test_batch_response_is_sorted_by_index(self):
        parsed = embeddings._parse_batch_response(
            {
                "data": [
                    {"index": 1, "embedding": [2.0, 0.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            },
            2,
        )
        self.assertEqual(parsed, [[1.0, 0.0], [2.0, 0.0]])

    def test_batch_response_rejects_partial_duplicate_and_mixed_dimensions(self):
        with self.assertRaisesRegex(RuntimeError, "1 vectors for 2 inputs"):
            embeddings._parse_batch_response(
                {"data": [{"index": 0, "embedding": [1.0]}]},
                2,
            )
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            embeddings._parse_batch_response(
                {
                    "data": [
                        {"index": 0, "embedding": [1.0]},
                        {"index": 0, "embedding": [2.0]},
                    ]
                },
                2,
            )
        with self.assertRaisesRegex(RuntimeError, "inconsistent"):
            embeddings._parse_batch_response(
                {
                    "data": [
                        {"index": 0, "embedding": [1.0]},
                        {"index": 1, "embedding": [2.0, 3.0]},
                    ]
                },
                2,
            )

    def test_batch_failure_falls_back_to_individual_requests(self):
        cfg = embeddings.EmbeddingConfig(
            endpoint="http://localhost:9999/v1",
            model="test",
            batch_size=2,
        )
        with (
            mock.patch.object(
                embeddings, "_fetch_batch", side_effect=RuntimeError("bad batch")
            ),
            mock.patch.object(
                embeddings,
                "_fetch_one",
                side_effect=lambda text, _cfg: [float(len(text)), 1.0],
            ) as fetch_one,
        ):
            vectors = embeddings.embed_with_config(["a", "bb"], cfg)
        self.assertEqual(vectors, [[1.0, 1.0], [2.0, 1.0]])
        self.assertEqual(fetch_one.call_count, 2)

    def test_single_request_auto_halves_only_for_oversize_errors(self):
        cfg = embeddings.EmbeddingConfig(
            endpoint="http://localhost:9999/v1",
            model="test",
        )
        payload_lengths = []

        def request(_endpoint, payload, _headers, _timeout):
            payload_lengths.append(len(payload["input"]))
            if len(payload_lengths) == 1:
                raise embeddings.EmbeddingHttpError(
                    413, "input length exceeds context length"
                )
            return {"data": [{"embedding": [1.0, 2.0]}]}

        with mock.patch.object(embeddings, "_request_json", side_effect=request):
            vector = embeddings._fetch_one("x" * 200, cfg)
        self.assertEqual(vector, [1.0, 2.0])
        self.assertEqual(payload_lengths, [200, 100])

    def test_auto_halving_uses_character_boundary_down_to_one_character(self):
        cfg = embeddings.EmbeddingConfig(
            endpoint="http://localhost:9999/v1/embeddings",
            model="test",
        )
        payload_lengths = []

        def request(_endpoint, payload, _headers, _timeout):
            payload_lengths.append(len(payload["input"]))
            if len(payload["input"]) > 1:
                raise embeddings.EmbeddingHttpError(413, "too long")
            return {"data": [{"embedding": [1.0]}]}

        with mock.patch.object(embeddings, "_request_json", side_effect=request):
            vector = embeddings._fetch_one("雷达波束", cfg)
        self.assertEqual(vector, [1.0])
        self.assertEqual(payload_lengths, [4, 2, 1])


class ProviderConfigurationTests(unittest.TestCase):
    def test_explicit_generic_endpoint_is_used_verbatim(self):
        cfg = embeddings.EmbeddingConfig(
            endpoint=(
                "https://gateway.example.com/proxy/volcengine"
                "?upstream=volces.com"
            ),
            model="custom",
        )
        self.assertEqual(embeddings._resolved_endpoint(cfg), cfg.endpoint)

    def test_legacy_base_url_gets_embeddings_suffix(self):
        with mock.patch.dict(
            embeddings.os.environ,
            {
                "EMBEDDING_BASE_URL": "http://127.0.0.1:11434/v1",
                "EMBEDDING_MODEL": "bge-m3",
            },
            clear=True,
        ):
            endpoint, model, _key = embeddings.get_embed_config()
        self.assertEqual(
            endpoint, "http://127.0.0.1:11434/v1/embeddings"
        )
        self.assertEqual(model, "bge-m3")

    def test_explicit_endpoint_wins_over_legacy_base(self):
        with mock.patch.dict(
            embeddings.os.environ,
            {
                "EMBEDDING_ENDPOINT": "https://example.test/custom/embed",
                "EMBEDDING_BASE_URL": "https://ignored.test/v1",
            },
            clear=True,
        ):
            endpoint, _model, _key = embeddings.get_embed_config()
        self.assertEqual(endpoint, "https://example.test/custom/embed")

    def test_google_endpoint_body_and_api_key_header_match_native_shape(self):
        cfg = embeddings.EmbeddingConfig(
            endpoint=(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-embedding-001:batchEmbedContents?key=URL_KEY&alt=json"
            ),
            model="gemini-embedding-001",
            api_key="HEADER_KEY",
            output_dimensionality=768,
        )
        endpoint = embeddings._resolved_endpoint(cfg)
        self.assertIn(":embedContent", endpoint)
        self.assertNotIn("batchEmbedContents", endpoint)
        self.assertNotIn("key=", endpoint)
        self.assertIn("alt=json", endpoint)
        self.assertEqual(
            embeddings._single_payload("hello", cfg)[
                "output_dimensionality"
            ],
            768,
        )
        self.assertEqual(
            embeddings._headers(cfg, endpoint)["x-goog-api-key"],
            "HEADER_KEY",
        )

    def test_volcengine_and_doubao_multimodal_shapes(self):
        text_cfg = embeddings.EmbeddingConfig(
            endpoint="https://ark.cn-beijing.volces.com/api/v3",
            model="doubao-embedding-text-240715",
        )
        self.assertEqual(
            embeddings._resolved_endpoint(text_cfg),
            "https://ark.cn-beijing.volces.com/api/v3/embeddings",
        )
        vision_cfg = embeddings.replace(
            text_cfg,
            endpoint=(
                "https://ark.cn-beijing.volces.com/api/v3/embeddings"
                "?trace=1"
            ),
            model="doubao-embedding-vision",
        )
        self.assertEqual(
            embeddings._resolved_endpoint(vision_cfg),
            "https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal"
            "?trace=1",
        )
        self.assertEqual(
            embeddings._single_payload("hello", vision_cfg)["input"],
            [{"type": "text", "text": "hello"}],
        )

    def test_default_timeout_matches_nashsu(self):
        with mock.patch.dict(embeddings.os.environ, {}, clear=True):
            cfg = embeddings.embedding_config_from_env()
        self.assertEqual(cfg.timeout, 8.0)


class IncrementalPageIndexTests(unittest.TestCase):
    def _configure(self, root: Path) -> None:
        runtime = root / ".llm-wiki"
        runtime.mkdir(parents=True, exist_ok=True)
        embeddings.ROOT = str(root)
        embeddings.WIKI = str(root / "wiki")
        embeddings.RUNTIME_DIR = str(runtime)
        embeddings.LANCE_DIR = str(runtime / "lancedb")
        embeddings.CONFIG = embeddings.EmbeddingConfig(
            endpoint="http://unused/v1",
            model="fake",
            target_chars=1000,
            overlap_chars=200,
        )
        embeddings.TARGET_CHARS = 1000
        embeddings.OVERLAP_CHARS = 200

    @staticmethod
    def _fake_vectors(texts, _config):
        return [
            [float(index + 1), float(len(text)), 0.5, -0.5]
            for index, text in enumerate(texts)
        ]

    def test_full_rebuild_then_page_upsert_preserves_untouched_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wiki = root / "wiki" / "concepts"
            wiki.mkdir(parents=True)
            page_a = wiki / "a.md"
            page_b = wiki / "b.md"
            page_a.write_text(
                "---\ntitle: A\n---\n# A\n\nold body\n", encoding="utf-8"
            )
            page_b.write_text(
                "---\ntitle: B\n---\n# B\n\nstable body\n", encoding="utf-8"
            )
            self._configure(root)
            embeddings.ARGS = SimpleNamespace(page=None)

            with (
                mock.patch.object(
                    embeddings, "embed_with_config", side_effect=self._fake_vectors
                ),
                mock.patch.object(
                    embeddings,
                    "_best_effort_compact_and_prune",
                    return_value=True,
                ),
            ):
                embeddings.cmd_embed()
                db = embeddings.lancedb.connect(embeddings.LANCE_DIR)
                table = db.open_table(embeddings.TABLE_NAME)
                b_before = table.count_rows(
                    embeddings._page_filter("concepts/b")
                )
                total_before = table.count_rows()

                page_a.write_text(
                    "---\ntitle: A\n---\n"
                    "# A\n\nnew body\n\n"
                    "## Details\n\nsecond section\n",
                    encoding="utf-8",
                )
                embeddings.ARGS = SimpleNamespace(page=[str(page_a)])
                embeddings.cmd_upsert()

            table = embeddings.lancedb.connect(embeddings.LANCE_DIR).open_table(
                embeddings.TABLE_NAME
            )
            self.assertEqual(
                table.count_rows(embeddings._page_filter("concepts/b")),
                b_before,
            )
            expected_a = len(
                embeddings.build_chunks(
                    embeddings.collect_pages([str(page_a)])
                )
            )
            self.assertEqual(
                table.count_rows(embeddings._page_filter("concepts/a")),
                expected_a,
            )
            self.assertEqual(
                table.count_rows(), total_before - 1 + expected_a
            )
            self.assertFalse((root / ".llm-wiki" / "embed-cache.json").exists())

    def test_embedding_failure_leaves_existing_page_rows_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            page = root / "wiki" / "concepts" / "a.md"
            page.parent.mkdir(parents=True)
            page.write_text("# A\n\nold body\n", encoding="utf-8")
            self._configure(root)
            embeddings.ARGS = SimpleNamespace(page=None)
            with (
                mock.patch.object(
                    embeddings, "embed_with_config", side_effect=self._fake_vectors
                ),
                mock.patch.object(
                    embeddings,
                    "_best_effort_compact_and_prune",
                    return_value=True,
                ),
            ):
                embeddings.cmd_embed()

            table = embeddings.lancedb.connect(embeddings.LANCE_DIR).open_table(
                embeddings.TABLE_NAME
            )
            rows_before = table.count_rows(
                embeddings._page_filter("concepts/a")
            )
            page.write_text("# A\n\nchanged body\n", encoding="utf-8")
            embeddings.ARGS = SimpleNamespace(page=[str(page)])
            with mock.patch.object(
                embeddings,
                "embed_with_config",
                side_effect=RuntimeError("endpoint down"),
            ):
                with self.assertRaisesRegex(RuntimeError, "endpoint down"):
                    embeddings.cmd_upsert()
            table = embeddings.lancedb.connect(embeddings.LANCE_DIR).open_table(
                embeddings.TABLE_NAME
            )
            self.assertEqual(
                table.count_rows(embeddings._page_filter("concepts/a")),
                rows_before,
            )


class PageAggregationTests(unittest.TestCase):
    def test_top_chunk_plus_bounded_tail_can_promote_a_page(self):
        class Frame:
            def iterrows(self):
                rows = [
                    {
                        "page_id": "a",
                        "path": "concepts/a.md",
                        "title": "A",
                        "chunk_text": "a1",
                        "heading_path": "# A",
                        "_distance": (1.0 / 0.90) - 1.0,
                    },
                    {
                        "page_id": "b",
                        "path": "concepts/b.md",
                        "title": "B",
                        "chunk_text": "b1",
                        "heading_path": "# B",
                        "_distance": (1.0 / 0.95) - 1.0,
                    },
                    {
                        "page_id": "a",
                        "path": "concepts/a.md",
                        "title": "A",
                        "chunk_text": "a2",
                        "heading_path": "## Details",
                        "_distance": 1.0,
                    },
                ]
                yield from enumerate(rows)

        ranked = embeddings._aggregate_page_results(Frame(), 2)
        self.assertEqual([item["page_id"] for item in ranked], ["a", "b"])
        self.assertAlmostEqual(ranked[0]["score"], 1.0)
        self.assertEqual(len(ranked[0]["matched_chunks"]), 2)


if __name__ == "__main__":
    unittest.main()
