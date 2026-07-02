"""Stage 2.2 chunk heading-path resolution: chapter-marker-first labeling.

Regression (5-book batch, 2026-07-01): the nearest-heading ancestor stack
labeled essentially every chunk with OCR garbage — a chunk covering 第15-16章
came out as "地基雷达（AN/TPS-59…） > 雨", another covering 第2-3章 as
"《雷达手册（第三版）》中文版出版说明 > 磁控管". OCR promotes front-matter
titles and figure captions to markdown headings, and the stack ignored
chunk_end entirely, so stale/deep pseudo-headings won over the chapters the
chunk actually spans.

Fix: prefer explicit chapter anchors (第N章 / Chapter N, else numeric section
headings) and label the chunk's SPAN ("第2章 … → 第3章 …"); chunks starting
before chapter 1 get the front-matter label; texts without chapter anchors
keep the legacy heading-stack behavior.

Stdlib unittest only.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _stage_2_analyze import _stage_2_2_resolve_chunk_heading_path  # noqa: E402


# Synthetic OCR-style book: front-matter pseudo-headings, chapters at H2,
# and a figure-caption pseudo-heading ("### 磁控管") inside chapter 2.
TEXT = (
    "# 《雷达手册（第三版）》中文版出版说明\n\n"
    "出版说明正文。\n\n"
    "# 目录\n\n"
    "第1章 雷达概述…… 第2章 MTI雷达…… 第3章 AMTI雷达……\n\n"
    "## 第1章 雷达概述\n\n" + "第1章正文。" * 60 + "\n\n"
    "## 第2章 MTI雷达\n\n" + "第2章正文。" * 30 + "\n\n"
    "### 磁控管\n\n" + "磁控管图注正文。" * 30 + "\n\n"
    "## 第3章 AMTI雷达\n\n" + "第3章正文。" * 60 + "\n"
)


class ChapterAnchorLabeling(unittest.TestCase):
    def test_chunk_spanning_two_chapters_gets_span_label(self):
        start = TEXT.index("第2章正文。")
        end = TEXT.index("第3章正文。") + 30
        path = _stage_2_2_resolve_chunk_heading_path(TEXT, start, end)
        self.assertEqual(path, "第2章 MTI雷达 → 第3章 AMTI雷达")

    def test_chunk_inside_single_chapter_gets_that_chapter(self):
        start = TEXT.index("第1章正文。")
        path = _stage_2_2_resolve_chunk_heading_path(TEXT, start, start + 120)
        self.assertEqual(path, "第1章 雷达概述")

    def test_front_matter_chunk_gets_front_matter_label(self):
        end = TEXT.index("## 第1章")
        path = _stage_2_2_resolve_chunk_heading_path(TEXT, 0, end)
        self.assertEqual(path, "前置材料（前言/目录）")

    def test_front_matter_chunk_spanning_into_chapter_1(self):
        end = TEXT.index("第1章正文。") + 60
        path = _stage_2_2_resolve_chunk_heading_path(TEXT, 0, end)
        self.assertEqual(path, "前置材料（前言/目录） → 第1章 雷达概述")

    def test_stale_front_matter_and_caption_headings_not_chosen(self):
        # Chunk starts right after the "### 磁控管" caption pseudo-heading;
        # the old stack returned "目录 > 第2章 MTI雷达 > 磁控管".
        start = TEXT.index("磁控管图注正文。")
        path = _stage_2_2_resolve_chunk_heading_path(TEXT, start, start + 60)
        self.assertEqual(path, "第2章 MTI雷达")
        self.assertNotIn("出版说明", path)
        self.assertNotIn("磁控管", path)

    def test_english_chapter_anchors_get_span_label(self):
        text = (
            "# Preface\n\npreface body. " + "x " * 50 + "\n\n"
            "# Chapter 1 Introduction\n\n" + "intro body. " * 40 + "\n\n"
            "# Chapter 2 Methods\n\n" + "methods body. " * 40 + "\n"
        )
        start = text.index("intro body.")
        end = text.index("methods body.") + 20
        path = _stage_2_2_resolve_chunk_heading_path(text, start, end)
        self.assertEqual(path, "Chapter 1 Introduction → Chapter 2 Methods")

    def test_numeric_section_anchors_when_no_chapter_markers(self):
        text = (
            "## 1.1 Scope\n\n" + "scope body. " * 40 + "\n\n"
            "## 1.2 Terms\n\n" + "terms body. " * 40 + "\n"
        )
        start = text.index("scope body.")
        path = _stage_2_2_resolve_chunk_heading_path(text, start, start + 100)
        self.assertEqual(path, "1.1 Scope")


class FallbackWithoutChapterAnchors(unittest.TestCase):
    def test_falls_back_to_heading_stack(self):
        text = (
            "# Radar Handbook\n\n"
            "## Antenna Basics\n\n" + "plain body text. " * 40
        )
        start = text.index("plain body text.")
        path = _stage_2_2_resolve_chunk_heading_path(text, start, start + 200)
        self.assertEqual(path, "Radar Handbook > Antenna Basics")

    def test_no_headings_at_all_returns_empty(self):
        text = "plain body text. " * 40
        path = _stage_2_2_resolve_chunk_heading_path(text, 10, 200)
        self.assertEqual(path, "")


if __name__ == "__main__":
    unittest.main()
