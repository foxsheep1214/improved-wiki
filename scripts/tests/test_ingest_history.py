"""Legacy history migration evidence and deduplication tests."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _ingest_events import load_ingest_events  # noqa: E402
from ingest_history import (  # noqa: E402
    HistoryConfig,
    cmd_migrate,
    migration_candidates,
)


def _config(root: Path) -> HistoryConfig:
    runtime = root / ".llm-wiki"
    return HistoryConfig(
        wiki_root=root,
        raw_root=root / "raw",
        wiki_dir=root / "wiki",
        runtime_dir=runtime,
        progress_dir=runtime / "ingest-progress",
        cache_path=runtime / "ingest-cache.json",
    )


def _seed_task(cfg: HistoryConfig, marker_ms: int) -> str:
    source_hash = "a" * 64
    cfg.progress_dir.mkdir(parents=True, exist_ok=True)
    (cfg.progress_dir / f"{source_hash[:16]}.task.json").write_text(
        json.dumps({
            "source": {
                "identity": "raw/Book/x.pdf",
                "sha256": source_hash,
            }
        }),
        encoding="utf-8",
    )
    (cfg.progress_dir / f"{source_hash[:16]}.stages.json").write_text(
        json.dumps({"ingested": marker_ms}),
        encoding="utf-8",
    )
    return source_hash


class TestHistoryMigration(unittest.TestCase):
    def test_nearby_legacy_retries_collapse_but_distant_runs_remain(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)
            cfg.wiki_dir.mkdir(parents=True)
            cfg.wiki_dir.joinpath("log.md").write_text(
                "# Log\n\n"
                "## 2026-06-30 11:49:27 — INGEST\n"
                "- Source: `raw/Book/x.pdf`\n"
                "- Hash: aaaaaaaaaaaaaaaa\n\n"
                "## 2026-06-30 11:51:02 — INGEST\n"
                "- Source: `raw/Book/x.pdf`\n"
                "- Hash: aaaaaaaaaaaaaaaa\n\n"
                "## 2026-07-02 09:00:00 — INGEST\n"
                "- Source: `raw/Book/x.pdf`\n"
                "- Hash: aaaaaaaaaaaaaaaa\n",
                encoding="utf-8",
            )

            candidates = migration_candidates(cfg)

            self.assertEqual(len(candidates), 2)
            self.assertEqual(candidates[0]["completed_at"][11:19], "11:51:02")
            self.assertEqual(candidates[0]["legacy_log_record_count"], 2)
            self.assertEqual(
                candidates[0]["legacy_log_first_at"][11:19], "11:49:27"
            )
            self.assertNotIn("legacy_log_record_count", candidates[1])

    def test_different_legacy_and_marker_dates_become_first_and_last_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)
            cfg.wiki_dir.mkdir(parents=True)
            cfg.wiki_dir.joinpath("log.md").write_text(
                "# Log\n\n## 2026-06-30 — INGEST\n"
                "- Source: `raw/Book/x.pdf`\n"
                "- Hash: aaaaaaaaaaaaaaaa\n",
                encoding="utf-8",
            )
            marker_ms = int(
                datetime(2026, 8, 14, 2, tzinfo=timezone.utc).timestamp() * 1000
            )
            _seed_task(cfg, marker_ms)

            candidates = migration_candidates(cfg)
            self.assertEqual(len(candidates), 2)
            self.assertEqual(
                {event["origin"] for event in candidates},
                {"legacy_log", "stages_marker"},
            )

    def test_same_calendar_day_prefers_precise_stage_marker(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)
            cfg.wiki_dir.mkdir(parents=True)
            cfg.wiki_dir.joinpath("log.md").write_text(
                "# Log\n\n## 2026-08-14 — INGEST\n"
                "- Source: `raw/Book/x.pdf`\n"
                "- Hash: aaaaaaaaaaaaaaaa\n",
                encoding="utf-8",
            )
            marker_ms = int(
                datetime(2026, 8, 14, 2, tzinfo=timezone.utc).timestamp() * 1000
            )
            _seed_task(cfg, marker_ms)

            candidates = migration_candidates(cfg)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["origin"], "stages_marker")

    def test_apply_binds_current_marker_to_migrated_run(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)
            marker_ms = int(
                datetime(2026, 8, 14, 2, tzinfo=timezone.utc).timestamp() * 1000
            )
            source_hash = _seed_task(cfg, marker_ms)

            self.assertEqual(
                cmd_migrate(cfg, SimpleNamespace(apply=True)), 0
            )
            events = load_ingest_events(cfg)
            self.assertEqual(len(events), 1)
            stages = json.loads(
                (cfg.progress_dir / f"{source_hash[:16]}.stages.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                stages["ingested__payload"]["run_id"], events[0]["run_id"]
            )


if __name__ == "__main__":
    unittest.main()
