"""Regression tests for unknown Office image page/slide metadata."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _stage_1_2_images import _stage_1_2_extract_images_office  # noqa: E402


class TestOfficeImagePageMetadata(unittest.TestCase):
    def test_unmapped_office_images_keep_null_page(self):
        cases = (
            ("docx", "word/media/image1.png"),
            ("pptx", "ppt/media/image1.png"),
        )
        for extension, archive_path in cases:
            with self.subTest(extension=extension):
                with tempfile.TemporaryDirectory() as d:
                    tmp = Path(d)
                    raw_file = tmp / f"book.{extension}"
                    with zipfile.ZipFile(raw_file, "w") as archive:
                        archive.writestr(archive_path, b"image-bytes" * 16)
                    media_dir = tmp / "media"
                    media_dir.mkdir()
                    manifest_path = media_dir / "_manifest.json"

                    result = _stage_1_2_extract_images_office(
                        raw_file, media_dir, manifest_path, min_size=10
                    )
                    manifest = json.loads(
                        manifest_path.read_text(encoding="utf-8")
                    )

                self.assertEqual(result["count"], 1)
                self.assertIsNone(result["images"][0]["page"])
                self.assertIsNone(manifest["images"][0]["page"])

if __name__ == "__main__":
    unittest.main()
