"""Completed sources must not surface stale conversation tasks as pending."""
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
from _batch_status import _batch_status_snapshot  # noqa: E402


def _make_config(tmp: Path) -> _core.Config:
    return _core.Config(
        wiki_root=tmp,
        raw_root=tmp / "raw",
        wiki_dir=tmp / "wiki",
        runtime_dir=tmp / "rt",
        cache_path=tmp / "rt" / "ingest-cache.json",
        progress_dir=tmp / "rt" / "ingest-progress",
        extract_tmp_dir=tmp / "rt" / "extract-tmp",
        llm_model="m",
        caption_api_key="",
        caption_base_url="x",
        caption_model="c",
        chunk_overlap=3000,
        source_budget=100000,
        target_chars=60000,
        target_tokens=30000,
        max_tokens=8192,
        conversation_prefix="ab12cd34",
    )


def _write_source_state(
    cfg: _core.Config,
    source_hash: str,
    *,
    ingested: bool,
) -> None:
    cfg.progress_dir.mkdir(parents=True, exist_ok=True)
    task = {
        "status": "complete" if ingested else "in_progress",
        "updated_at": 1,
        "source": {
            "identity": f"raw/Book/{source_hash[-8:]}.pdf",
            "sha256": source_hash,
        },
    }
    stages = {"stage_2_2_done": 1}
    if ingested:
        stages["ingested"] = 2
    (cfg.progress_dir / f"{source_hash[:16]}.task.json").write_text(
        json.dumps(task), encoding="utf-8")
    (cfg.progress_dir / f"{source_hash[:16]}.stages.json").write_text(
        json.dumps(stages), encoding="utf-8")


def _write_pending_handoff(cfg: _core.Config, source_hash: str) -> None:
    conversation_dir = (
        cfg.runtime_dir / "conversation" / source_hash[-8:])
    conversation_dir.mkdir(parents=True, exist_ok=True)
    (conversation_dir / "stage.txt").write_text(
        "answer ready", encoding="utf-8")
    (conversation_dir / "tasks.json").write_text(
        json.dumps({
            "tasks": {
                "stage": {
                    "status": "pending",
                    "result_file": "stage.txt",
                },
            },
        }),
        encoding="utf-8",
    )


class BatchStatusCompletedHandoffTests(unittest.TestCase):
    def test_completed_source_hides_stale_pending_task(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            source_hash = "1" * 56 + "deadbeef"
            _write_source_state(cfg, source_hash, ingested=True)
            _write_pending_handoff(cfg, source_hash)

            snapshot = _batch_status_snapshot(cfg)

            self.assertEqual([], snapshot["handoffs"])
            self.assertEqual([], snapshot["unfinished_sources"])

    def test_unfinished_source_keeps_answer_ready_handoff_visible(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            source_hash = "2" * 56 + "feedface"
            _write_source_state(cfg, source_hash, ingested=False)
            _write_pending_handoff(cfg, source_hash)

            snapshot = _batch_status_snapshot(cfg)

            self.assertEqual(1, len(snapshot["handoffs"]))
            self.assertEqual("feedface", snapshot["handoffs"][0]["prefix"])
            self.assertEqual(1, snapshot["handoffs"][0]["answer_ready"])
            self.assertEqual(1, len(snapshot["unfinished_sources"]))


if __name__ == "__main__":
    unittest.main()
