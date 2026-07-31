"""Stage 3.5 idempotency and required aggregate-artifact postconditions."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _core  # noqa: E402
import _stage_3_write as stage3  # noqa: E402


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


class TestAggregateCompletionGate(unittest.TestCase):
    def setUp(self):
        self._original_call = stage3.call_anthropic_protocol

    def tearDown(self):
        stage3.call_anthropic_protocol = self._original_call

    def test_resume_does_not_append_duplicate_log_record(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)
            raw = root / "raw" / "Book" / "x.pdf"
            raw.parent.mkdir(parents=True)
            raw.write_bytes(b"book")
            source = root / "wiki" / "sources" / "x.md"
            source.parent.mkdir(parents=True)
            source.write_text("# x\n", encoding="utf-8")

            def _llm(prompt, config, max_tokens):
                if "# CURRENT index.md" in prompt:
                    return (
                        "# Index\n\n## Sources（来源）\n\n- [[x]] — x\n",
                        "end_turn",
                    )
                return ("# Overview\n\nTopic synthesis.\n", "end_turn")

            stage3.call_anthropic_protocol = _llm
            source_hash = _core.file_sha256(raw)
            first = stage3.stage_3_5_aggregate_repair(
                source, raw, {}, source_hash, "mineru-api", cfg)
            second = stage3.stage_3_5_aggregate_repair(
                source, raw, {}, source_hash, "mineru-api", cfg)

            log_text = (cfg.wiki_dir / "log.md").read_text(encoding="utf-8")
            self.assertEqual(
                log_text.count(f"- Hash: {source_hash[:16]}"), 1)
            for refs in (first, second):
                self.assertIn("wiki/log.md", refs)
                self.assertIn("wiki/index.md", refs)

    def test_postcondition_rejects_missing_index_link(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)
            log = cfg.wiki_dir / "log.md"
            index = cfg.wiki_dir / "index.md"
            log.parent.mkdir(parents=True)
            source_hash = "a" * 64
            log.write_text(
                "# Log\n\n## 2026-01-01 — INGEST\n"
                "- Source: `raw/Book/x.pdf`\n"
                f"- Hash: {source_hash[:16]}\n",
                encoding="utf-8",
            )
            index.write_text("# Index\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "does not contain"):
                stage3._assert_aggregate_outputs(
                    log,
                    index,
                    "raw/Book/x.pdf",
                    source_hash,
                    "x",
                )

    def test_postcondition_accepts_full_relative_index_target(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = _config(root)
            log = cfg.wiki_dir / "log.md"
            index = cfg.wiki_dir / "index.md"
            log.parent.mkdir(parents=True)
            source_hash = "b" * 64
            log.write_text(
                "# Log\n\n## 2026-01-01 — INGEST\n"
                "- Source: `raw/Book/x.pdf`\n"
                f"- Hash: {source_hash[:16]}\n",
                encoding="utf-8",
            )
            index.write_text(
                "# Wiki Index\n\n## source\n\n"
                "- [[sources/Book/x|Book X]]\n",
                encoding="utf-8",
            )

            stage3._assert_aggregate_outputs(
                log,
                index,
                "raw/Book/x.pdf",
                source_hash,
                "x",
            )


if __name__ == "__main__":
    unittest.main()
