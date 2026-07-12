"""Regression test for the persistent go/no-go warning log (2026-07-09 NashSU
0.6.0 parity: projectPath/.llm-wiki/ingest-warnings.log).

Before this, validate_stage_outputs()'s return value (go_nogo_warnings) was
computed and printed once, then discarded — nothing wrote it to disk, so the
warnings were gone once the terminal scrolled past them.

Stdlib unittest only — no pytest, no network, no LLM calls.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _core  # noqa: E402
from _ingest_write import _append_ingest_warning_log  # noqa: E402


def _make_config(tmp: Path) -> _core.Config:
    return _core.Config(
        wiki_root=tmp / "wiki", raw_root=tmp / "raw",
        wiki_dir=tmp / "wiki" / "wiki", runtime_dir=tmp / "rt",
        cache_path=tmp / "rt" / "ingest-cache.json",
        progress_dir=tmp / "rt" / "ingest-progress",
        extract_tmp_dir=tmp / "rt" / "extract-tmp",
        llm_model="m",
        caption_api_key="", caption_base_url="x",
        caption_model="c", chunk_overlap=3000,
        source_budget=100000, target_chars=60000, target_tokens=30000,
        max_tokens=8192, conversation_prefix="ab12cd34",
    )


class TestAppendIngestWarningLog(unittest.TestCase):
    def test_writes_warnings_to_runtime_dir_log(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            config = _make_config(tmp)
            raw_file = tmp / "raw" / "ELINT.pdf"

            _append_ingest_warning_log(config, raw_file, ["Stage 1.2: images extracted but _manifest.json missing"])

            log_path = config.runtime_dir / "ingest-warnings.log"
            self.assertTrue(log_path.exists())
            text = log_path.read_text(encoding="utf-8")
            self.assertIn("ELINT.pdf", text)
            self.assertIn("Stage 1.2: images extracted but _manifest.json missing", text)

    def test_appends_rather_than_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            config = _make_config(tmp)

            _append_ingest_warning_log(config, tmp / "raw" / "A.pdf", ["warning A"])
            _append_ingest_warning_log(config, tmp / "raw" / "B.pdf", ["warning B"])

            text = (config.runtime_dir / "ingest-warnings.log").read_text(encoding="utf-8")
            self.assertIn("A.pdf", text)
            self.assertIn("B.pdf", text)
            self.assertIn("warning A", text)
            self.assertIn("warning B", text)


if __name__ == "__main__":
    unittest.main()
