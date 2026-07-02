"""Audit 2026-07-02 section D design rulings (user-decided).

  D1 — slug language = source-text language: rule injected into BOTH Stage 2.4
       generation prompts and the Stage 2.7 query constraint.
  D2 — book-level granularity switch: Stage 2.2 injects a COARSE directive
       ONLY when Stage 2.1 classified book_meta.granularity == "manual".
  D4 — figure references link to the book's source page (never bare numbers).
  D6 — Stage 2.9 section headings follow the content language (fixed Chinese
       vocabulary for Chinese sources, English otherwise).

Stdlib unittest only.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _core  # noqa: E402
import _language  # noqa: E402
import _stage_2_4_generation as gen  # noqa: E402
import _stage_2_7_query_generation as qgen  # noqa: E402
import _stage_2_9_comparison as comp  # noqa: E402
import _stage_2_analyze as analyze  # noqa: E402


def _make_config(tmp: Path) -> _core.Config:
    return _core.Config(
        wiki_root=tmp, raw_root=tmp / "raw",
        wiki_dir=tmp / "wiki", runtime_dir=tmp / "rt",
        cache_path=tmp / "rt" / "ingest-cache.json",
        progress_dir=tmp / "rt" / "ingest-progress",
        extract_tmp_dir=tmp / "rt" / "extract-tmp",
        llm_base_url="https://example.invalid", llm_model="m", llm_api_key="",
        llm_protocol="anthropic", caption_api_key="", caption_base_url="x",
        caption_model="c", chunk_size=60000, chunk_overlap=3000,
        source_budget=100000, target_chars=60000, target_tokens=30000,
        max_tokens=8192, conversation_prefix="ab12cd34",
    )


class _EnvIsolatedCase(unittest.TestCase):
    """Neutralize IMPROVED_WIKI_OUTPUT_LANGUAGE so detection is exercised."""

    def setUp(self):
        self._saved = os.environ.pop(_language.OUTPUT_LANGUAGE_ENV, None)

    def tearDown(self):
        if self._saved is not None:
            os.environ[_language.OUTPUT_LANGUAGE_ENV] = self._saved


class TestD1D4GenerationRules(unittest.TestCase):
    def _build_prompts(self, tmp: Path) -> tuple[str, str]:
        cfg = _make_config(tmp)
        (cfg.raw_root / "Book").mkdir(parents=True, exist_ok=True)
        file_path = cfg.raw_root / "Book" / "雷达手册.pdf"
        analysis = {
            "concepts_found": [{"name": "matched filter", "importance": "core",
                                "definition": "d", "key_details": []}],
            "entities_found": [],
        }
        per_chunk = gen._stage_2_4_build_prompt(analysis, "some text", 0, file_path, cfg)
        single_shot = gen._stage_2_4_build_all_prompt([analysis], file_path, cfg)
        return per_chunk, single_shot

    def test_slug_language_rule_in_both_prompts(self):
        with tempfile.TemporaryDirectory() as d:
            per_chunk, single_shot = self._build_prompts(Path(d))

            for prompt in (per_chunk, single_shot):
                self.assertIn("slug uses the SOURCE language", prompt)
                self.assertIn("中英双拼", prompt)
                self.assertIn("mti, cfar, dds", prompt)

    def test_figure_reference_rule_links_source_page(self):
        with tempfile.TemporaryDirectory() as d:
            per_chunk, single_shot = self._build_prompts(Path(d))

            for prompt in (per_chunk, single_shot):
                self.assertIn("[[sources/Book/雷达手册|据图2.6]]", prompt)
                self.assertIn("do NOT embed images", prompt)

    def test_source_page_slug_outside_raw_falls_back_to_stem(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))

            slug = gen._source_page_slug(Path("/elsewhere/foo.pdf"), cfg)

            self.assertEqual(slug, "sources/foo")


class TestD1QueryConstraint(unittest.TestCase):
    def test_query_prompt_uses_source_language_slug_constraint(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            (cfg.raw_root / "Book").mkdir(parents=True, exist_ok=True)
            file_path = cfg.raw_root / "Book" / "book.pdf"

            prompt = qgen._stage_2_7_build_prompt(
                {"book_meta": {"title": "t"}}, ["concept-a"], [], [], file_path, cfg)

            self.assertNotIn("slug: English kebab-case", prompt)
            self.assertIn("SOURCE-language slug", prompt)
            self.assertIn("中英双拼", prompt)


class TestD2GranularitySwitch(unittest.TestCase):
    def test_manual_injects_coarse_block(self):
        block = analyze._stage_2_2_granularity_block(
            {"book_meta": {"granularity": "manual"}})

        self.assertIn("COARSE granularity", block)
        self.assertIn("system/subsystem-level", block)

    def test_textbook_absent_or_malformed_inject_nothing(self):
        for digest in (
            {"book_meta": {"granularity": "textbook"}},
            {"book_meta": {}},
            {},
            {"book_meta": "not-a-dict"},
            None,
        ):
            self.assertEqual(analyze._stage_2_2_granularity_block(digest), "")

    def test_stage_2_1_prompt_asks_for_granularity(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            (cfg.raw_root / "Book").mkdir(parents=True, exist_ok=True)

            prompt = analyze._stage_2_1_build_prompt(
                "text", cfg.raw_root / "Book" / "b.pdf", cfg)

            self.assertIn('granularity: "textbook" | "manual"', prompt)

    def test_stage_2_2_prompt_conditional_injection(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            (cfg.raw_root / "Book").mkdir(parents=True, exist_ok=True)
            file_path = cfg.raw_root / "Book" / "b.pdf"

            manual = analyze._stage_2_2_build_prompt(
                "text", 0, 1, {"book_meta": {"granularity": "manual"}}, file_path, cfg)
            textbook = analyze._stage_2_2_build_prompt(
                "text", 0, 1, {"book_meta": {"granularity": "textbook"}}, file_path, cfg)

            self.assertIn("COARSE granularity", manual)
            self.assertNotIn("COARSE granularity", textbook)


class TestD6ComparisonHeadings(_EnvIsolatedCase):
    def test_chinese_source_uses_chinese_headings(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            (cfg.raw_root).mkdir(parents=True, exist_ok=True)

            prompt = comp._stage_2_9_build_prompt_in_source(
                ["动目标显示", "脉冲多普勒"], cfg.raw_root / "book.pdf", cfg,
                source_context="雷达信号处理中，动目标显示与脉冲多普勒经常被放在一起比较。")

            for heading in ("## 为何对比", "## 对比表", "## 选型指南", "## 参见"):
                self.assertIn(heading, prompt)
            for heading in ("## Why Compare", "## Comparison Table",
                            "## Selection Guide", "## See Also"):
                self.assertNotIn(heading, prompt)

    def test_english_source_keeps_english_headings(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            (cfg.raw_root).mkdir(parents=True, exist_ok=True)

            prompt = comp._stage_2_9_build_prompt_in_source(
                ["mti", "pulse-doppler"], cfg.raw_root / "book.pdf", cfg,
                source_context="MTI and pulse-Doppler radar are frequently compared.")

            for heading in ("## Why Compare", "## Comparison Table",
                            "## Selection Guide", "## See Also"):
                self.assertIn(heading, prompt)
            self.assertNotIn("## 为何对比", prompt)

    def test_headings_helper_fixed_vocabularies(self):
        self.assertEqual(comp._stage_2_9_headings("纯中文样本文字，足够判定语言。"),
                         ("为何对比", "对比表", "选型指南", "参见"))
        self.assertEqual(comp._stage_2_9_headings("plain English sample text"),
                         ("Why Compare", "Comparison Table", "Selection Guide", "See Also"))


if __name__ == "__main__":
    unittest.main()
