"""Run-level event ledger and source-page time projection regressions."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _core  # noqa: E402
from _frontmatter import parse_frontmatter  # noqa: E402
from _ingest_events import (  # noqa: E402
    IngestEventError,
    append_ingest_event,
    build_ingest_completed_event,
    build_repair_completed_event,
    clear_source_page_time_snapshot,
    commit_ingest_completion,
    ingest_event_path,
    load_ingest_events,
    restore_source_page_times,
    snapshot_source_page_times,
    summarize_ingest_history,
)
from _stage_3_write import _stage_3_2_wiki_path_for_source  # noqa: E402


def _config(root: Path) -> _core.Config:
    return _core.Config(
        wiki_root=root,
        raw_root=root / "raw",
        wiki_dir=root / "wiki",
        runtime_dir=root / ".llm-wiki",
        cache_path=root / ".llm-wiki" / "ingest-cache.json",
        progress_dir=root / ".llm-wiki" / "ingest-progress",
        extract_tmp_dir=root / ".llm-wiki" / "extract-tmp",
        llm_model="test",
        caption_api_key="",
        caption_base_url="http://127.0.0.1",
        caption_model="test",
        chunk_overlap=3000,
        source_budget=100000,
        target_chars=60000,
        target_tokens=30000,
        max_tokens=8192,
    )


def _raw(root: Path) -> Path:
    raw = root / "raw" / "Book" / "x.pdf"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"book")
    return raw


def _source_page(raw: Path, cfg: _core.Config) -> Path:
    page = _stage_3_2_wiki_path_for_source(raw, cfg)
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntype: source\ntitle: X\n"
        "created: 2026-08-14\nupdated: 2026-08-14\n---\n\n# X\n",
        encoding="utf-8",
    )
    return page


def _ms(year: int, month: int, day: int, hour: int = 0) -> int:
    return int(
        datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp() * 1000
    )


class TestIngestEvents(unittest.TestCase):
    def test_pre_delete_snapshot_restores_updated_only_for_identical_content(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)
            raw = _raw(root)
            page = _source_page(raw, cfg)
            page.write_text(
                "---\ntype: source\ntitle: X\n"
                "created: 2026-06-30\nupdated: 2026-07-01\n"
                "first_ingested_at: \"2026-06-30T10:00:00.000+08:00\"\n"
                "last_ingested_at: \"2026-07-01T10:00:00.000+08:00\"\n"
                "---\n\n# X\nSame body.\n",
                encoding="utf-8",
            )
            snapshot_source_page_times(
                raw, cfg, page, captured_at_ms=_ms(2026, 8, 14)
            )
            # Re-created page: only operational dates differ.
            page.write_text(
                "---\ntype: source\ntitle: X\n"
                "created: 2026-08-14\nupdated: 2026-08-14\n"
                "---\n\n# X\nSame body.\n",
                encoding="utf-8",
            )
            changed, identical = restore_source_page_times(
                raw, cfg, "reingest-run"
            )
            self.assertTrue(changed)
            self.assertTrue(identical)
            fm, _body = parse_frontmatter(page.read_text(encoding="utf-8"))
            self.assertEqual(fm["created"], "2026-06-30")
            self.assertEqual(fm["updated"], "2026-07-01")
            clear_source_page_time_snapshot(raw, cfg, "reingest-run")

    def test_pre_delete_snapshot_keeps_new_updated_when_content_changed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)
            raw = _raw(root)
            page = _source_page(raw, cfg)
            page.write_text(
                "---\ntype: source\ntitle: X\n"
                "created: 2026-06-30\nupdated: 2026-07-01\n"
                "---\n\n# X\nOld body.\n",
                encoding="utf-8",
            )
            snapshot_source_page_times(
                raw, cfg, page, captured_at_ms=_ms(2026, 8, 14)
            )
            page.write_text(
                "---\ntype: source\ntitle: X\n"
                "created: 2026-08-14\nupdated: 2026-08-14\n"
                "---\n\n# X\nChanged body.\n",
                encoding="utf-8",
            )
            _changed, identical = restore_source_page_times(
                raw, cfg, "changed-run"
            )
            self.assertFalse(identical)
            fm, _body = parse_frontmatter(page.read_text(encoding="utf-8"))
            self.assertEqual(fm["created"], "2026-06-30")
            self.assertEqual(fm["updated"], "2026-08-14")

    def test_two_runs_preserve_first_and_advance_last_without_touching_updated(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)
            raw = _raw(root)
            page = _source_page(raw, cfg)
            source_hash = _core.file_sha256(raw)
            first = build_ingest_completed_event(
                raw, cfg, source_hash, "run-1", _ms(2026, 6, 30, 2)
            )
            second = build_ingest_completed_event(
                raw, cfg, source_hash, "run-2", _ms(2026, 8, 14, 2)
            )

            commit_ingest_completion(cfg, first)
            commit_ingest_completion(cfg, second)
            # Crash/replay of the same completed run is idempotent.
            _event, appended = commit_ingest_completion(cfg, second)
            self.assertFalse(appended)

            events = load_ingest_events(cfg)
            self.assertEqual([event["run_id"] for event in events], ["run-1", "run-2"])
            fm, _body = parse_frontmatter(page.read_text(encoding="utf-8"))
            self.assertEqual(fm["first_ingested_at"], first["completed_at"])
            self.assertEqual(fm["last_ingested_at"], second["completed_at"])
            self.assertEqual(fm["created"], first["completed_at"][:10])
            self.assertEqual(fm["updated"], "2026-08-14")
            log = (cfg.wiki_dir / "log.md").read_text(encoding="utf-8")
            self.assertEqual(log.count("— INGEST COMPLETED"), 2)
            self.assertEqual(log.count("- Run: `run-2`"), 1)

    def test_repair_event_is_separate_from_full_ingest_summary(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)
            raw = _raw(root)
            _source_page(raw, cfg)
            source_hash = _core.file_sha256(raw)
            ingest = build_ingest_completed_event(
                raw, cfg, source_hash, "run-ingest", _ms(2026, 6, 30)
            )
            repair = build_repair_completed_event(
                raw,
                cfg,
                source_hash,
                _ms(2026, 8, 14),
                repair_kind="media",
                run_id="run-repair",
            )
            append_ingest_event(cfg, ingest)
            append_ingest_event(cfg, repair)
            rows = summarize_ingest_history(load_ingest_events(cfg))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["ingest_count"], 1)
            self.assertEqual(rows[0]["last_ingested_at"], ingest["completed_at"])

    def test_same_run_with_different_content_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)
            raw = _raw(root)
            source_hash = _core.file_sha256(raw)
            first = build_ingest_completed_event(
                raw, cfg, source_hash, "same-run", _ms(2026, 6, 30)
            )
            conflicting = build_ingest_completed_event(
                raw, cfg, source_hash, "same-run", _ms(2026, 7, 1)
            )
            append_ingest_event(cfg, first)
            with self.assertRaisesRegex(IngestEventError, "different content"):
                append_ingest_event(cfg, conflicting)

    def test_corrupt_ledger_fails_loudly(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)
            path = ingest_event_path(cfg)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not-json}\n", encoding="utf-8")
            with self.assertRaisesRegex(IngestEventError, "corrupt at line 1"):
                load_ingest_events(cfg)


if __name__ == "__main__":
    unittest.main()
