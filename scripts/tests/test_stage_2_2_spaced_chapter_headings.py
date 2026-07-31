"""Stage 2.2 heading-path resolution: letter-spaced chapter openers (Wiley
ELINT live incident, 2026-07-10).

This book's real chapter-opener typography renders through minerU OCR as
widely letter-spaced plain text — "C H A P T E R 1" (two-digit chapters even
space the digits: "C H A P T E R 1 0") — appearing as a bare line ABOVE the
real "# <Chapter Title>" H1, not as a markdown heading itself. Meanwhile the
book's own Table of Contents lists each chapter as "## CHAPTER N" (OCR
promoted TOC entries to real headings), which matches the OLD
_CHAPTER_ANCHOR_RE pattern perfectly.

Net effect before the fix: _CHAPTER_ANCHOR_RE matched 100% of the TOC noise
and 0% of the real chapter openers — Stage 2.2's "current location in the
book" line told chunk 1's answering agent it covered front matter through
Chapter 11, when the chunk's actual <extracted_text> only reached Chapter 3.
The agent caught the discrepancy by reading the actual text, but a less
careful model could trust the wrong label and hallucinate coverage.

Fix: recognize the letter-spaced "C H A P T E R N" line as an anchor too,
label it "Chapter N" (digits de-spaced). Real anchors sit at their true,
much-later position in the book, so once detected they naturally win the
"last anchor before chunk_end" comparison over the TOC's early-clustered
noise for realistic chunk sizes.

Stdlib unittest only.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _stage_2_analyze as s2  # noqa: E402


def _book(toc_extra: str = "") -> str:
    # Mirrors the real OCR shape: a TOC page with "## CHAPTER N" headings
    # (noise, including a chapter far past what this excerpt actually
    # contains — the exact shape of the live bug), then real chapters using
    # bare letter-spaced openers.
    toc = (
        "## Contents\n\n"
        "## CHAPTER 1\n\nCHAPTER 1\nElectronic Intelligence 1\n\n"
        "## CHAPTER 3\n\nCHAPTER 3\nSignal Analysis 41\n\n"
        "## CHAPTER 11\n\nCHAPTER 11\nECM Techniques 300\n\n"
        f"{toc_extra}"
    )
    ch1 = (
        "\n\nC H A P T E R 1\n\n"
        "# Electronic Intelligence\n\n"
        + ("Radar has been called the greatest advance. " * 200) + "\n\n"
    )
    ch2 = (
        "C H A P T E R 2\n\n"
        "# ELINT Implications of Range Equations\n\n"
        + ("The range equation governs detection. " * 200) + "\n\n"
    )
    ch3 = (
        "C H A P T E R 3\n\n"
        "# Signal Analysis\n\n"
        + ("Pulse descriptor words matter. " * 200) + "\n\n"
    )
    return toc + ch1 + ch2 + ch3


class SpacedChapterHeadings(unittest.TestCase):
    def test_toc_alone_no_longer_wins_over_real_later_chapter(self):
        text = _book()
        ch3_pos = text.index("C H A P T E R 3\n\n# Signal")
        # Chunk spans from just after the TOC through real chapter 3's start.
        label = s2._stage_2_2_resolve_chunk_heading_path(text, 0, ch3_pos + 10)
        self.assertNotIn("11", label)  # would have picked bogus TOC noise
        self.assertIn("Chapter 3", label)

    def test_spaced_two_digit_chapter_number_despaced(self):
        text = "C H A P T E R 1 0\n\n# Countermeasures\n\n" + ("Body text. " * 50)
        label = s2._stage_2_2_resolve_chunk_heading_path(text, 0, len(text))
        self.assertIn("Chapter 10", label)

    def test_real_chapter_span_label(self):
        text = _book()
        ch1_pos = text.index("C H A P T E R 1\n\n# Electronic")
        ch2_pos = text.index("C H A P T E R 2\n\n# ELINT")
        end = ch2_pos + 500
        label = s2._stage_2_2_resolve_chunk_heading_path(text, ch1_pos, end)
        self.assertIn("Chapter 1", label)
        self.assertIn("Chapter 2", label)

    def test_no_spaced_heading_falls_back_normally(self):
        text = "## CHAPTER 1\n\nSome front matter TOC text.\n"
        label = s2._stage_2_2_resolve_chunk_heading_path(text, 0, len(text))
        # No real (non-TOC-shaped) anchor exists — old normal-heading path
        # still applies unchanged.
        self.assertIn("CHAPTER 1", label)


if __name__ == "__main__":
    unittest.main()
