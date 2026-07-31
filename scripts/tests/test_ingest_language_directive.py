"""Regression tests for the output-language directive injection into ingest
generation prompts (NashSU buildLanguageDirective parity).

Stdlib unittest only — no network, no LLM — so it runs with the same python3
the pipeline uses.

Bug being guarded: build_language_directive() lived only in
wiki-lint-semantic.py and was NEVER injected into the Stage 2 ingest
analysis/generation prompts, so the generating model got no language
instruction and guessed. NashSU injects it at the top of ~7 prompt builders.

Run:
    python3 -m unittest tests.test_ingest_language_directive   # from scripts/
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _core  # noqa: E402
import _stage_2_4_generation as gen  # noqa: E402

_ENGLISH_SOURCE = (
    "The processor pipeline overlaps the execution stages of successive "
    "instructions to improve throughput across many clock cycles."
)
_CHINESE_SOURCE = (
    "处理器流水线把相邻指令的执行阶段重叠起来，从而在多个时钟周期内提升吞吐量，"
    "更充分地利用硬件资源。"
)

_CHUNK_ANALYSES = [
    {
        "concepts_found": [{"name": "Pipelining", "definition": "overlap stages"}],
        "entities_found": [],
    }
]


def _make_config(tmp: Path) -> _core.Config:
    (tmp / "wiki").mkdir(parents=True, exist_ok=True)
    (tmp / "raw").mkdir(parents=True, exist_ok=True)
    return _core.Config(
        wiki_root=tmp, raw_root=tmp / "raw",
        wiki_dir=tmp / "wiki", runtime_dir=tmp / "rt",
        cache_path=tmp / "rt" / "ingest-cache.json",
        progress_dir=tmp / "rt" / "ingest-progress",
        extract_tmp_dir=tmp / "rt" / "extract-tmp",
        llm_model="m",
        caption_api_key="", caption_base_url="x",
        caption_model="c", chunk_overlap=3000,
        source_budget=100000, target_chars=60000, target_tokens=30000,
        max_tokens=8192, conversation_prefix="ab12cd34",
    )


def _build(tmp: Path, source_context: str) -> str:
    config = _make_config(tmp)
    return gen._stage_2_4_build_all_prompt(
        _CHUNK_ANALYSES,
        config.raw_root / "book.pdf",
        config,
        source_context=source_context,
    )


class TestIngestLanguageDirective(unittest.TestCase):
    def setUp(self) -> None:
        # Ensure no stray override leaks in from the environment.
        self._saved = os.environ.pop("IMPROVED_WIKI_OUTPUT_LANGUAGE", None)

    def tearDown(self) -> None:
        os.environ.pop("IMPROVED_WIKI_OUTPUT_LANGUAGE", None)
        if self._saved is not None:
            os.environ["IMPROVED_WIKI_OUTPUT_LANGUAGE"] = self._saved

    def test_english_source_yields_english_directive(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            prompt = _build(Path(d), _ENGLISH_SOURCE)
        self.assertIn("MANDATORY OUTPUT LANGUAGE: English", prompt)
        # Directive must lead the prompt, before the `# Role` header.
        self.assertLess(prompt.index("MANDATORY OUTPUT LANGUAGE"), prompt.index("# Role"))

    def test_chinese_source_yields_chinese_directive(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            prompt = _build(Path(d), _CHINESE_SOURCE)
        self.assertIn("MANDATORY OUTPUT LANGUAGE: Chinese", prompt)

    def test_env_override_forces_english_on_chinese_source(self) -> None:
        os.environ["IMPROVED_WIKI_OUTPUT_LANGUAGE"] = "English"
        with tempfile.TemporaryDirectory() as d:
            prompt = _build(Path(d), _CHINESE_SOURCE)
        self.assertIn("MANDATORY OUTPUT LANGUAGE: English", prompt)


if __name__ == "__main__":
    unittest.main()
