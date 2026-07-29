"""Canonical PageRef path-contract regressions."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _core  # noqa: E402
import _stage_3_7_embed  # noqa: E402
from _page_ref import (  # noqa: E402
    PageRef,
    PageRefError,
    canonical_page_refs,
)


def _config(root: Path) -> _core.Config:
    return _core.Config(
        wiki_root=root,
        raw_root=root / "raw",
        wiki_dir=root / "wiki",
        runtime_dir=root / ".llm-wiki",
        cache_path=root / ".llm-wiki" / "ingest-cache.json",
        progress_dir=root / ".llm-wiki" / "ingest-progress",
        extract_tmp_dir=root / ".llm-wiki" / "extract-tmp",
        llm_model="m",
        caption_api_key="k",
        caption_base_url="x",
        caption_model="c",
        chunk_overlap=3000,
        source_budget=100000,
        target_chars=60000,
        target_tokens=30000,
        max_tokens=8192,
    )


class TestPageRef(unittest.TestCase):
    def test_canonical_legacy_and_absolute_inputs_converge(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            absolute = root / "wiki" / "concepts" / "cache.md"
            refs = [
                PageRef.parse("wiki/concepts/cache.md", root),
                PageRef.parse("concepts/cache.md", root),
                PageRef.parse(absolute, root),
                PageRef.parse(r"wiki\concepts\cache.md", root),
            ]
            for ref in refs:
                self.assertEqual(
                    ref.project_relative, "wiki/concepts/cache.md")
                self.assertEqual(ref.wiki_relative, "concepts/cache.md")
                self.assertEqual(ref.absolute_path, absolute.resolve())
                self.assertEqual(ref.slug, "concepts/cache")

    def test_canonical_list_deduplicates_all_legacy_spellings(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            got = canonical_page_refs([
                "wiki/entities/cpu.md",
                "entities/cpu.md",
                root / "wiki" / "entities" / "cpu.md",
                "wiki/concepts/cache.md",
            ], root)
            self.assertEqual(got, [
                "wiki/entities/cpu.md",
                "wiki/concepts/cache.md",
            ])

    def test_rejects_unsafe_or_ambiguous_values(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            bad = [
                "",
                "../outside.md",
                "wiki/../outside.md",
                "wiki/wiki/concepts/x.md",
                "wiki/concepts/x.txt",
                root / "outside.md",
            ]
            for value in bad:
                with self.subTest(value=value):
                    with self.assertRaises(PageRefError):
                        PageRef.parse(value, root)


class TestEmbeddingConsumesPageRef(unittest.TestCase):
    def test_local_ollama_capability_probe_parses_url_before_request(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"models": [{"model": "bge-m3:latest"}]}'

        with (
            mock.patch.dict(sys.modules, {"lancedb": mock.Mock()}),
            mock.patch.object(
                _stage_3_7_embed.urllib.request,
                "urlopen",
                return_value=Response(),
            ) as urlopen,
        ):
            result = _stage_3_7_embed._stage_3_7_check_embed_capability(
                "http://127.0.0.1:11434/v1",
                "bge-m3",
            )

        self.assertEqual(result, (True, ""))
        urlopen.assert_called_once_with(
            "http://127.0.0.1:11434/api/tags",
            timeout=3,
        )

    def test_legacy_ref_resolves_once_without_wiki_wiki_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)
            page = cfg.wiki_dir / "concepts" / "cache.md"
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text("# Cache\n", encoding="utf-8")

            completed = mock.Mock(returncode=0, stdout="", stderr="")
            with (
                mock.patch.object(
                    _stage_3_7_embed,
                    "_stage_3_7_check_embed_capability",
                    return_value=(True, ""),
                ),
                mock.patch(
                    "subprocess.run",
                    return_value=completed,
                ) as run,
            ):
                _stage_3_7_embed.stage_3_7_embed_new_pages(
                    cfg, ["concepts/cache.md"])

            self.assertTrue(run.called)
            command = run.call_args.args[0]
            self.assertIn("upsert", command)
            self.assertIn("--page", command)
            self.assertNotIn("embed", command)

    def test_invalid_ref_is_a_hard_failure(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _config(Path(d))
            with mock.patch.object(
                _stage_3_7_embed,
                "_stage_3_7_check_embed_capability",
                return_value=(True, ""),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "invalid page reference"):
                    _stage_3_7_embed.stage_3_7_embed_new_pages(
                        cfg, ["wiki/../escape.md"])


if __name__ == "__main__":
    unittest.main()
