"""Source-scoped minerU caption context and manifest accounting."""
from __future__ import annotations

import base64
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _stage_1_1_scanned as scanned  # noqa: E402
import _stage_1_2_images as images  # noqa: E402
import _stage_1_3_caption as caption  # noqa: E402


def _png_data_uri() -> str:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def _config(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        wiki_root=root,
        raw_root=root / "raw",
        wiki_dir=root / "wiki",
        runtime_dir=root / ".llm-wiki",
        extract_tmp_dir=root / ".llm-wiki" / "extract-tmp",
        caption_api_key="",
    )


class TestSourceScopedContext(unittest.TestCase):
    def setUp(self):
        caption._CONTEXT_MAP_CACHE.clear()

    def tearDown(self):
        caption._CONTEXT_MAP_CACHE.clear()

    def test_harvest_persists_source_join_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            raw_file = config.raw_root / "Book" / "book.pdf"
            raw_file.parent.mkdir(parents=True)
            raw_file.write_bytes(b"%PDF")
            chunk_out = config.extract_tmp_dir / "book" / "_chunk_0000-0032"
            chunk_out.mkdir(parents=True)
            content = [
                {"type": "text", "text": "before"},
                {
                    "type": "image",
                    "img_path": "images/current.png",
                    "page_idx": 1,
                    "image_caption": ["Figure 1"],
                },
                {"type": "text", "text": "after"},
            ]
            results = {
                "chunk": {
                    "images": {"current.png": _png_data_uri()},
                    "content_list": json.dumps(content),
                },
            }

            saved = images._stage_1_2_harvest_images(
                results, 0, raw_file, config, chunk_out,
            )

            self.assertEqual(saved[0]["mineru_basename"], "current.png")
            self.assertEqual(
                json.loads(
                    (chunk_out / "_mineru_content_list.json").read_text()
                ),
                content,
            )

    def test_context_map_uses_only_current_source_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            media_dir = config.wiki_dir / "media" / "Book" / "current"
            media_dir.mkdir(parents=True)
            (media_dir / "p0001-mineru_aaaaaaaa.jpg").write_bytes(b"current")

            chunk = config.extract_tmp_dir / "current" / "_chunk_0000-0032"
            chunk.mkdir(parents=True)
            (chunk / "_mineru_content_list.json").write_text(json.dumps([
                {"type": "text", "text": "current before"},
                {"type": "image", "img_path": "images/current.png"},
                {"type": "text", "text": "current after"},
            ]))
            (chunk / "_mineru_figures.json").write_text(json.dumps([{
                "filename": "p0001-mineru_aaaaaaaa.jpg",
                "mineru_basename": "current.png",
            }]))

            # An unrelated global minerU job must not enter this source map.
            other = (
                config.runtime_dir
                / "mineru-api-out/other/chunk/hybrid_auto"
            )
            (other / "images").mkdir(parents=True)
            (other / "images/other.png").write_bytes(b"other")
            (other / "chunk_content_list.json").write_text(json.dumps([
                {"type": "text", "text": "wrong before"},
                {"type": "image", "img_path": "images/other.png"},
                {"type": "text", "text": "wrong after"},
            ]))

            context = caption._stage_1_3_build_context_map(config, media_dir)

            self.assertEqual(set(context), {"aaaaaaaa"})
            self.assertEqual(context["aaaaaaaa"]["context_before"], "current before")
            self.assertEqual(context["aaaaaaaa"]["context_after"], "current after")


class TestManifestAccounting(unittest.TestCase):
    def test_caption_sidecars_are_not_counted_as_extracted_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            raw_file = config.raw_root / "Book" / "book.pdf"
            raw_file.parent.mkdir(parents=True)
            raw_file.write_bytes(b"%PDF")
            media_dir = config.wiki_dir / "media" / "Book" / "book"
            media_dir.mkdir(parents=True)
            image_path = media_dir / "p0001-mineru_aaaaaaaa.jpg"
            image_path.write_bytes(b"image")
            (media_dir / (image_path.name + ".caption.txt")).write_text(
                "A complete VLM caption longer than twenty characters."
            )
            output = io.StringIO()

            with (
                patch.object(
                    scanned, "_stage_1_1_assemble_ocr_text", return_value="body"
                ),
                redirect_stdout(output),
            ):
                scanned._stage_1_1_scanned_assemble_manifest(
                    root / "out", raw_file, config, 1,
                )

            manifest = json.loads((media_dir / "_manifest.json").read_text())
            self.assertEqual(len(manifest["images"]), 1)
            self.assertEqual(manifest["images"][0]["filename"], image_path.name)
            self.assertIn("1 images extracted", output.getvalue())


if __name__ == "__main__":
    unittest.main()
