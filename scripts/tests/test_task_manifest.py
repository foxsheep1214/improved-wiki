"""Task-manifest source, contract, artifact, and page binding regressions."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _core  # noqa: E402
from _task_manifest import (  # noqa: E402
    TaskManifestError,
    bind_chunk_plan,
    bind_page_refs,
    ensure_task_manifest,
    task_manifest_path,
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
        llm_model="analysis-model",
        caption_api_key="super-secret",
        caption_base_url="https://caption.example/v1",
        caption_model="vision-model",
        chunk_overlap=3000,
        source_budget=100000,
        target_chars=60000,
        target_tokens=30000,
        max_tokens=8192,
        caption_fallback_api_key="other-secret",
        caption_fallback_base_url="http://127.0.0.1:11434/v1",
        caption_fallback_model="local-vision",
        caption_fallback_protocol="openai",
    )


def _raw(root: Path, name: str = "book.pdf", content: bytes = b"same") -> Path:
    path = root / "raw" / "Book" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


class TestTaskManifest(unittest.TestCase):
    def test_manifest_is_source_bound_and_secret_free(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)
            raw = _raw(root)
            manifest = ensure_task_manifest(raw, cfg)
            source_hash = _core.file_sha256(raw)
            persisted = task_manifest_path(cfg, source_hash).read_text(
                encoding="utf-8")

            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["source"]["identity"], "raw/Book/book.pdf")
            self.assertEqual(
                manifest["source"]["sha256"], source_hash)
            self.assertEqual(
                manifest["contract"]["media"]["caption_host"],
                "caption.example",
            )
            self.assertNotIn("super-secret", persisted)
            self.assertNotIn("other-secret", persisted)

    def test_identical_content_at_different_identity_is_not_reused(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)
            first = _raw(root, "first.pdf", b"identical")
            second = _raw(root, "second.pdf", b"identical")
            ensure_task_manifest(first, cfg)

            with self.assertRaisesRegex(
                TaskManifestError, "content-hash task collision"):
                ensure_task_manifest(second, cfg)

    def test_contract_drift_is_recorded_not_hidden(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)
            raw = _raw(root)
            first = ensure_task_manifest(raw, cfg)
            cfg.target_chars += 1
            second = ensure_task_manifest(raw, cfg)

            self.assertEqual(first["task_id"], second["task_id"])
            self.assertNotEqual(
                first["contract_sha256"], second["contract_sha256"])
            self.assertEqual(len(second["contract_history"]), 1)

    def test_legacy_bootstrap_auto_heals_pages_removed_by_later_lint(self):
        """Route A (2026-07-30, explicit user decision after Op Amps for
        Everyone hit this live on HardwareWiki): a source's FIRST-EVER
        manifest bootstrap auto-heals legacy filesWritten entries that a
        later, LEGITIMATE wiki-lint dedup/delete-orphans pass has since
        removed — normal wiki lifecycle, not corruption. The stale entry is
        pruned (with a loud warning, never silently) from both the new
        manifest and the legacy ingest-cache.json entry, so downstream
        consumers agree on the same, currently-true page set. A page that IS
        still on disk is kept untouched."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)
            raw = _raw(root)
            source_hash = _core.file_sha256(raw)
            key = _core.source_cache_key(raw, cfg)

            present = cfg.wiki_dir / "concepts" / "present.md"
            present.parent.mkdir(parents=True, exist_ok=True)
            present.write_text("# Present\n", encoding="utf-8")

            _core.save_cache(cfg, {
                "version": "2",
                "entries": {
                    key: {
                        "hash": source_hash,
                        "filesWritten": [
                            "wiki/concepts/present.md",
                            "wiki/concepts/missing.md",
                        ],
                    },
                },
            })
            _core.mark_stage_done(cfg, source_hash, "ingested")

            manifest = ensure_task_manifest(raw, cfg)  # must NOT raise

            self.assertEqual(
                manifest["resume"]["page_refs"],
                ["wiki/concepts/present.md"],
            )
            cache = _core.load_cache(cfg)
            self.assertEqual(
                cache["entries"][key]["filesWritten"],
                ["wiki/concepts/present.md"],
            )

    def test_legacy_bootstrap_still_raises_on_empty_page_not_missing(self):
        """Auto-heal only covers pages that are truly GONE (merged/removed by
        lint, which deletes files outright). A page that exists but is EMPTY
        is a different, more corruption-like signal and must still hard-fail
        even at first bootstrap — the same as before Route A."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)
            raw = _raw(root)
            source_hash = _core.file_sha256(raw)
            key = _core.source_cache_key(raw, cfg)

            empty_page = cfg.wiki_dir / "concepts" / "empty.md"
            empty_page.parent.mkdir(parents=True, exist_ok=True)
            empty_page.write_text("", encoding="utf-8")

            _core.save_cache(cfg, {
                "version": "2",
                "entries": {
                    key: {
                        "hash": source_hash,
                        "filesWritten": ["wiki/concepts/empty.md"],
                    },
                },
            })
            _core.mark_stage_done(cfg, source_hash, "ingested")

            with self.assertRaisesRegex(
                TaskManifestError, "empty written pages"):
                ensure_task_manifest(raw, cfg)

    def test_active_manifest_still_raises_when_a_bound_page_vanishes(self):
        """Route A auto-heals ONLY a source's first-ever manifest bootstrap.
        Once a manifest exists and is actively tracking a page (bound via
        bind_page_refs during a real resume), that page vanishing is a live
        corruption signal within the CURRENT resume's own bookkeeping — not
        years of accumulated lint drift — and must still hard-fail, with the
        same actionable guidance."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)
            raw = _raw(root)
            source_hash = _core.file_sha256(raw)
            ensure_task_manifest(raw, cfg)  # bootstrap: empty legacy cache

            page = cfg.wiki_dir / "concepts" / "present.md"
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text("# Present\n", encoding="utf-8")
            bind_page_refs(cfg, source_hash, ["concepts/present.md"])
            _core.mark_stage_done(cfg, source_hash, "write_phase")

            page.unlink()  # simulate the page vanishing mid-resume

            with self.assertRaises(TaskManifestError) as ctx:
                ensure_task_manifest(raw, cfg)

            message = str(ctx.exception)
            self.assertIn("missing written pages", message)
            self.assertIn("dedup", message)
            self.assertIn("--delete", message)

    def test_chunk_plan_and_page_set_are_resume_invariants(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)
            raw = _raw(root)
            source_hash = _core.file_sha256(raw)
            ensure_task_manifest(raw, cfg)

            plan = {
                "schema_version": 2,
                "source_sha256": source_hash,
                "chunks": [{"chunk_id": "0001-deadbeef"}],
            }
            _core.save_progress(cfg, source_hash, {
                "chunk_plan_v2": plan,
            })
            bind_chunk_plan(cfg, source_hash, plan)
            _core.mark_stage_done(cfg, source_hash, "stage_2_2_done")

            page = cfg.wiki_dir / "concepts" / "cache.md"
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text("# Cache\n", encoding="utf-8")
            bind_page_refs(cfg, source_hash, ["concepts/cache.md"])
            _core.mark_stage_done(cfg, source_hash, "write_phase")
            _core.mark_stage_done(cfg, source_hash, "ingested")

            complete = json.loads(
                task_manifest_path(cfg, source_hash).read_text(
                    encoding="utf-8"))
            self.assertEqual(complete["status"], "complete")
            self.assertIn(
                "stage_2_2_done", complete["resume"]["stage_markers"])
            self.assertEqual(
                complete["resume"]["page_refs"],
                ["wiki/concepts/cache.md"],
            )

            page.unlink()
            with self.assertRaisesRegex(
                TaskManifestError, "missing written pages"):
                ensure_task_manifest(raw, cfg)

    def test_tampered_chunk_plan_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)
            raw = _raw(root)
            source_hash = _core.file_sha256(raw)
            ensure_task_manifest(raw, cfg)
            original = {"schema_version": 2, "chunks": [{"chunk_id": "a"}]}
            _core.save_progress(cfg, source_hash, {
                "chunk_plan_v2": original,
            })
            bind_chunk_plan(cfg, source_hash, original)
            _core.save_progress(cfg, source_hash, {
                "chunk_plan_v2": {
                    "schema_version": 2,
                    "chunks": [{"chunk_id": "tampered"}],
                },
            })

            with self.assertRaisesRegex(
                TaskManifestError, "chunk-plan binding"):
                ensure_task_manifest(raw, cfg)


if __name__ == "__main__":
    unittest.main()
