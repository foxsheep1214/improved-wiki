"""Audit 三/B (2026-07-02) prompt-bundle helpers.

Two read-only helpers were introduced alongside the prompt-text edits:
  - _stage_2_4_generation._top_wiki_tags   (B3 / M10: inject wiki's top tags)
  - _stage_2_9_comparison._existing_comparisons (B4 / H1: inject existing
    comparison slug+title so 2.9 refuses same-topic twins)

Stdlib unittest only.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _core  # noqa: E402
import _stage_2_4_generation as gen  # noqa: E402
import _stage_2_9_comparison as comp  # noqa: E402


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


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _page(tags: str, title: str = "T") -> str:
    return f'---\ntype: concept\ntitle: "{title}"\ntags: [{tags}]\nrelated: []\n---\n\n# {title}\n'


class TestTopWikiTags(unittest.TestCase):
    def test_counts_orders_and_filters(self):
        # Arrange: 雷达 ×3, mti ×2, singleton ×1, stub/lint artifact tags,
        # and a page inside an artifact dir (lint/) that must be ignored.
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            w = cfg.wiki_dir
            _write(w / "concepts" / "a.md", _page("雷达, mti"))
            _write(w / "concepts" / "b.md", _page("雷达, mti"))
            _write(w / "entities" / "c.md", _page('"雷达", singleton'))
            _write(w / "queries" / "stub.md", _page("stub, lint"))
            _write(w / "lint" / "artifact.md", _page("lintdir-tag, lintdir-tag"))

            # Act
            tags = gen._top_wiki_tags(cfg)

            # Assert: count-desc order, quotes stripped, singletons and
            # stub/lint tags and artifact-dir pages excluded.
            self.assertEqual(tags, ["雷达", "mti"])

    def test_empty_wiki_returns_empty_and_section_blank(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))

            self.assertEqual(gen._top_wiki_tags(cfg), [])
            self.assertEqual(gen._tags_reuse_section(cfg), "")

    def test_top_n_cap(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            w = cfg.wiki_dir
            many = ", ".join(f"tag{i:02d}" for i in range(40))
            _write(w / "concepts" / "a.md", _page(many))
            _write(w / "concepts" / "b.md", _page(many))

            tags = gen._top_wiki_tags(cfg, top_n=5)

            self.assertEqual(len(tags), 5)
            # equal counts → deterministic name order
            self.assertEqual(tags, [f"tag{i:02d}" for i in range(5)])

    def test_reuse_section_lists_tags(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            w = cfg.wiki_dir
            _write(w / "concepts" / "a.md", _page("数据处理"))
            _write(w / "concepts" / "b.md", _page("数据处理"))

            section = gen._tags_reuse_section(cfg)

            self.assertIn("数据处理", section)
            self.assertIn("reuse", section.lower())


class TestExistingComparisons(unittest.TestCase):
    def test_returns_slug_title_pairs_sorted(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            w = cfg.wiki_dir
            _write(w / "comparisons" / "mti-vs-pulse-doppler.md",
                   '---\ntype: comparison\ntitle: "MTI vs Pulse Doppler"\n---\n# MTI vs PD\n')
            _write(w / "comparisons" / "ekf-vs-ukf.md",
                   '---\ntype: comparison\ntitle: "EKF vs UKF"\n---\n# EKF vs UKF\n')
            # excluded: index anchor and _-prefixed system file
            _write(w / "comparisons" / "index.md", "---\ntype: index\n---\n")
            _write(w / "comparisons" / "_audit.md", "---\ntype: x\n---\n")

            pairs = comp._existing_comparisons(cfg)

            self.assertEqual(pairs, [
                ("ekf-vs-ukf", "EKF vs UKF"),
                ("mti-vs-pulse-doppler", "MTI vs Pulse Doppler"),
            ])

    def test_title_falls_back_to_stem(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            _write(cfg.wiki_dir / "comparisons" / "no-title.md", "# body only\n")

            pairs = comp._existing_comparisons(cfg)

            self.assertEqual(pairs, [("no-title", "no-title")])

    def test_missing_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))

            self.assertEqual(comp._existing_comparisons(cfg), [])

    def test_injected_into_prompt(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            (cfg.raw_root).mkdir(parents=True, exist_ok=True)
            _write(cfg.wiki_dir / "comparisons" / "mti-vs-pulse-doppler.md",
                   '---\ntype: comparison\ntitle: "MTI vs Pulse Doppler"\n---\n# x\n')

            prompt = comp._stage_2_9_build_prompt_in_source(
                ["concept-a", "concept-b"], cfg.raw_root / "book.pdf", cfg)

            self.assertIn("comparisons/mti-vs-pulse-doppler — MTI vs Pulse Doppler", prompt)
            self.assertIn("do NOT create twins", prompt)


if __name__ == "__main__":
    unittest.main()
