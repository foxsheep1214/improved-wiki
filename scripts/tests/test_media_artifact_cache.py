"""Stage 1.2/1.3 media artifact cache integrity regressions."""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _core  # noqa: E402
import _media_integrity  # noqa: E402
from _paths import media_slug  # noqa: E402
from _stage_1_2_images import (  # noqa: E402
    _stage_1_2_extract_from_mineru,
    _stage_1_2_harvest_images,
    _stage_1_2_write_manifest,
    validate_stage_1_2_artifact,
)
from _stage_1_3_caption import validate_stage_1_3_artifact  # noqa: E402
import _stage_1_1_scanned  # noqa: E402
from _stage_3_write import _stage_3_1_wiki_path_for_source  # noqa: E402


def _config(tmp: Path) -> _core.Config:
    return _core.Config(
        wiki_root=tmp,
        raw_root=tmp / "raw",
        wiki_dir=tmp / "wiki",
        runtime_dir=tmp / ".llm-wiki",
        cache_path=tmp / ".llm-wiki" / "ingest-cache.json",
        progress_dir=tmp / ".llm-wiki" / "ingest-progress",
        extract_tmp_dir=tmp / ".llm-wiki" / "extract-tmp",
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


def _artifact(tmp: Path):
    cfg = _config(tmp)
    raw = tmp / "raw" / "Book" / "book.pdf"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"%PDF fake")
    media_dir = cfg.wiki_dir / "media" / media_slug(raw, cfg)
    media_dir.mkdir(parents=True, exist_ok=True)
    image_path = media_dir / "p0001-mineru_deadbeef.png"
    image_path.write_bytes(b"not-a-real-png-but-content-addressable")
    manifest_path = media_dir / "_manifest.json"
    images = _stage_1_2_write_manifest(
        manifest_path,
        "mineru-ocr",
        raw,
        [{
            "filename": image_path.name,
            "path": str(image_path.relative_to(cfg.wiki_root)),
            "page": 1,
            "width": 100,
            "height": 100,
        }],
    )
    result = {
        "count": 1,
        "media_dir": str(media_dir),
        "manifest": str(manifest_path),
        "images": images,
        "mineru": True,
    }
    return cfg, raw, image_path, result


class TestStage12ArtifactValidation(unittest.TestCase):
    def test_valid_v3_manifest_and_hash_pass(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, raw, _image, result = _artifact(Path(d))
            valid, reason, normalized = validate_stage_1_2_artifact(
                result, cfg, raw, expected_count=1)
            self.assertTrue(valid, reason)
            self.assertEqual(normalized["count"], 1)

    def test_missing_image_fails_even_when_cached_count_says_one(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, raw, image, result = _artifact(Path(d))
            image.unlink()
            valid, reason, _ = validate_stage_1_2_artifact(
                result, cfg, raw, expected_count=1)
            self.assertFalse(valid)
            self.assertIn("missing", reason)

    def test_tampered_image_hash_fails(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, raw, image, result = _artifact(Path(d))
            image.write_bytes(b"tampered")
            valid, reason, _ = validate_stage_1_2_artifact(
                result, cfg, raw, expected_count=1)
            self.assertFalse(valid)
            self.assertTrue(
                "size mismatch" in reason or "hash mismatch" in reason)


class TestStage13ArtifactValidation(unittest.TestCase):
    def test_required_policy_rejects_missing_caption(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, _raw, _image, result = _artifact(Path(d))
            valid, reason, actual = validate_stage_1_3_artifact(result, cfg)
            self.assertFalse(valid)
            self.assertIn("captions missing", reason)
            self.assertEqual(actual["pending"], 1)

    def test_required_policy_accepts_real_caption(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, _raw, image, result = _artifact(Path(d))
            (image.parent / (image.name + ".caption.txt")).write_text(
                "A detailed technical caption for the extracted figure.",
                encoding="utf-8",
            )
            valid, reason, actual = validate_stage_1_3_artifact(result, cfg)
            self.assertTrue(valid, reason)
            self.assertEqual(actual["complete"], 1)

    def test_best_effort_is_explicitly_degraded(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, _raw, _image, result = _artifact(Path(d))
            cfg.media_policy = "best_effort"
            valid, reason, actual = validate_stage_1_3_artifact(result, cfg)
            self.assertTrue(valid, reason)
            self.assertTrue(actual["degraded"])
            self.assertEqual(actual["policy"], "best_effort")


class TestMineruDurableMediaCache(unittest.TestCase):
    def test_figure_names_ignore_stale_layout_and_reharvest_children(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            active = root / "_chunk_0000-0032"
            stale = root / "_chunk_0000-0050"
            repair = root / "_media_reharvest_v1_hash" / "_chunk_0000-0032"
            for directory, filename in (
                (active, "p0001-mineru_active.png"),
                (stale, "p0001-mineru_stale.png"),
                (repair, "p0001-mineru_repair.png"),
            ):
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "_mineru_figures.json").write_text(
                    json.dumps([{"filename": filename}]), encoding="utf-8")

            names = _media_integrity.mineru_figure_names(
                root, active_chunk_keys={"0000-0032"})

            self.assertEqual(names, {"p0001-mineru_active.png"})

    def test_harvest_persists_chunk_local_bytes_for_rebuild(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _config(tmp)
            raw = tmp / "raw" / "Book" / "book.pdf"
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_bytes(b"%PDF fake")
            chunk_out = cfg.extract_tmp_dir / raw.stem / "_chunk_0000-0032"
            chunk_out.mkdir(parents=True, exist_ok=True)
            image_bytes = b"durable-mineru-image-bytes"
            results = {
                "chunk.pdf": {
                    "images": {
                        "figure.png": (
                            "data:image/png;base64,"
                            + base64.b64encode(image_bytes).decode("ascii")
                        ),
                    },
                    "content_list": [{
                        "type": "image",
                        "img_path": "images/figure.png",
                        "page_idx": 2,
                    }],
                },
            }

            saved = _stage_1_2_harvest_images(
                results, 32, raw, cfg, chunk_out)

            self.assertEqual(len(saved), 1)
            filename = saved[0]["filename"]
            durable = chunk_out / "_media_bytes" / filename
            canonical = (
                cfg.wiki_dir / "media" / media_slug(raw, cfg) / filename
            )
            self.assertEqual(durable.read_bytes(), image_bytes)
            self.assertEqual(canonical.read_bytes(), image_bytes)

    def test_rebuild_scope_ignores_untracked_stale_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _config(tmp)
            raw = tmp / "raw" / "Book" / "book.pdf"
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_bytes(b"%PDF fake")
            repair_out = cfg.extract_tmp_dir / raw.stem / "_media_reharvest"
            byte_dir = repair_out / "_chunk_0000-0032" / "_media_bytes"
            byte_dir.mkdir(parents=True, exist_ok=True)
            keep = "p0001-mineru_deadbeef.png"
            stale = "p0002-mineru_bad0cafe.png"
            (byte_dir / keep).write_bytes(b"keep")
            (byte_dir / stale).write_bytes(b"stale")

            result = _stage_1_2_extract_from_mineru(
                repair_out, cfg, raw, allowed_filenames={keep})

            self.assertEqual(result["count"], 1)
            self.assertEqual(result["images"][0]["filename"], keep)
            manifest = json.loads(
                Path(result["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(
                [item["filename"] for item in manifest["images"]], [keep])


class TestMineruMediaReharvest(unittest.TestCase):
    def test_reharvest_uses_source_bound_isolated_cache(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _config(tmp)
            raw = tmp / "raw" / "Book" / "book.pdf"
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_bytes(b"%PDF fake")

            with (
                mock.patch.object(
                    _stage_1_1_scanned,
                    "_stage_1_1_acquire_mineru_lock",
                    return_value=17,
                ),
                mock.patch.object(
                    _stage_1_1_scanned,
                    "_stage_1_1_release_mineru_lock",
                ) as release,
                mock.patch.object(
                    _stage_1_1_scanned,
                    "_stage_1_1_extract_text_scanned_impl",
                    return_value="",
                ) as extract,
            ):
                repair_dir = (
                    _stage_1_1_scanned._stage_1_1_reharvest_media(raw, cfg)
                )

            self.assertEqual(
                repair_dir.parent, cfg.extract_tmp_dir / raw.stem)
            self.assertIn("_media_reharvest_v1_", repair_dir.name)
            kwargs = extract.call_args.kwargs
            self.assertEqual(kwargs["out_dir_override"], repair_dir)
            self.assertFalse(kwargs["finalize_media"])
            release.assert_called_once_with(17)


class TestCompletedMediaRepair(unittest.TestCase):
    def test_preserved_canonical_media_is_not_shrunk_to_incomplete_ocr_index(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _config(tmp)
            raw = tmp / "raw" / "Book" / "book.pdf"
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_bytes(b"%PDF fake")
            media_dir = cfg.wiki_dir / "media" / media_slug(raw, cfg)
            media_dir.mkdir(parents=True, exist_ok=True)
            kept = "p0001-mineru_kept.png"
            retained = "p0002-mineru_retained.png"
            (media_dir / kept).write_bytes(b"kept")
            (media_dir / retained).write_bytes(b"retained")
            ocr_out = cfg.extract_tmp_dir / raw.stem
            ocr_out.mkdir(parents=True, exist_ok=True)

            with (
                mock.patch.object(
                    _media_integrity,
                    "_stage_1_2_extract_from_mineru",
                    return_value={"count": 2, "images": [
                        {"filename": kept}, {"filename": retained},
                    ]},
                ) as extract,
                mock.patch.object(
                    _media_integrity,
                    "_stage_1_1_reharvest_media",
                ) as reharvest,
            ):
                result, authoritative = (
                    _media_integrity.restore_or_reharvest_mineru_media(
                        raw, cfg, ocr_out,
                        expected_names={kept},
                    )
                )

            # The retained source media remains in scope even when the current
            # OCR index is incomplete.
            self.assertEqual(extract.call_args.kwargs["allowed_filenames"], {
                kept, retained})
            self.assertEqual(result["count"], 2)
            self.assertEqual(authoritative, 2)
            reharvest.assert_not_called()

    def test_unfinished_ingest_reharvests_instead_of_accepting_zero(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _config(tmp)
            raw = tmp / "raw" / "Book" / "book.pdf"
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_bytes(b"%PDF fake")
            ocr_out = cfg.extract_tmp_dir / raw.stem
            ocr_out.mkdir(parents=True, exist_ok=True)
            repair_out = ocr_out / "_media_reharvest"
            chunk = repair_out / "_chunk_0000-0032"
            chunk.mkdir(parents=True, exist_ok=True)
            (chunk / "_mineru_figures.json").write_text(
                json.dumps([{
                    "filename": "p0001-mineru_deadbeef.png",
                }]),
                encoding="utf-8",
            )

            with (
                mock.patch.object(
                    _media_integrity,
                    "_stage_1_2_extract_from_mineru",
                    side_effect=[
                        {"count": 0},
                        {"count": 1, "images": [{
                            "filename": "p0001-mineru_deadbeef.png",
                        }]},
                    ],
                ) as extract,
                mock.patch.object(
                    _media_integrity,
                    "_stage_1_1_reharvest_media",
                    return_value=repair_out,
                ) as reharvest,
            ):
                result, authoritative = (
                    _media_integrity.restore_or_reharvest_mineru_media(
                        raw, cfg, ocr_out, expected_hint=1)
                )

            self.assertEqual(result["count"], 1)
            self.assertEqual(authoritative, 1)
            reharvest.assert_called_once_with(raw, cfg)
            self.assertEqual(extract.call_count, 2)

    def test_repair_reembeds_modified_source_page(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg, raw, image, _result = _artifact(tmp)
            (image.parent / (image.name + ".caption.txt")).write_text(
                "A detailed technical caption for the repaired figure.",
                encoding="utf-8",
            )
            ocr_out = cfg.extract_tmp_dir / raw.stem
            ocr_out.mkdir(parents=True, exist_ok=True)
            source_path = _stage_3_1_wiki_path_for_source(raw, cfg)
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text("# Repaired source\n", encoding="utf-8")

            key = _core.source_cache_key(raw, cfg)
            _core.save_cache(cfg, {
                "version": "2",
                "entries": {
                    key: {
                        "hash": _core.file_sha256(raw),
                        "method": "mineru-api",
                        "stages": {
                            "images_extracted": 1,
                            "images_captioned": 0,
                            "images_injected": 0,
                        },
                    },
                },
            })

            with mock.patch.object(
                _media_integrity, "stage_3_7_embed_new_pages"
            ) as embed:
                repaired = _media_integrity.repair_completed_media(raw, cfg)

            self.assertEqual(repaired["images_injected"], 1)
            embed.assert_called_once()
            embedded_paths = embed.call_args.args[1]
            self.assertEqual(len(embedded_paths), 1)
            self.assertTrue(embedded_paths[0].endswith("book.md"))


if __name__ == "__main__":
    unittest.main()
