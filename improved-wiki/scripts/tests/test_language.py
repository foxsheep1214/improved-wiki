"""Regression tests for _language.detect_language.

Stdlib ``unittest`` only — no pytest, no network, no LLM calls.

Run:
    python3 -m unittest tests.test_language   # from scripts/
    python3 scripts/tests/test_language.py     # from skill root

Each test maps to a real misdetection hit during radar-book ingestion
(see references/known-issues.md): math Greek symbols and stray Latin
function words must not flip the detected language of an English
technical document.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _language import detect_language  # noqa: E402


class TestMathGreekNotGreek(unittest.TestCase):
    """Isolated Greek letters used as math symbols (λ, σ, θ, Δ, …) are
    notation, not Greek-language text. An English paragraph full of them
    must stay English."""

    def test_english_radar_equation_stays_english(self):
        text = (
            "The radar equation: P_r = P_t G^2 λ^2 σ / ((4π)^3 R^4), where λ "
            "is wavelength, σ is RCS, θ beamwidth, φ phase. SNR depends on "
            "α, β, μ, ω, Δ, Σ across the aperture."
        )
        self.assertEqual(detect_language(text), "English")

    def test_isolated_single_greek_letter_is_not_greek(self):
        # Two isolated Greek letters (the old ≥2-count threshold) among Latin.
        self.assertEqual(detect_language("Let λ and μ vary."), "English")


class TestRealGreekIsGreek(unittest.TestCase):
    """Genuine Greek text — multi-letter runs forming words — must still
    be detected as Greek so the directive still works for Greek sources."""

    def test_greek_sentence(self):
        text = "Αυτό είναι ένα κείμενο στα ελληνικά για δοκιμή ανίχνευσης."
        self.assertEqual(detect_language(text), "Greek")


class TestStrayLatinTokenNotFrench(unittest.TestCase):
    """A single short French-looking token (e.g. 'le') appearing inside
    English text must not flip the document to French. The Advanced Metric
    Wave Radar English foreword was misdetected as French this way."""

    def test_english_with_stray_le_stays_english(self):
        text = (
            "Advanced Metric Wave Radar by Jianqi Wu. The idea to write this "
            "book relates to the International Radar Conferences attended in "
            "le series of nations."
        )
        self.assertEqual(detect_language(text), "English")

    def test_single_french_word_not_enough(self):
        # 'est' appears as a standalone token but the rest is English.
        self.assertEqual(detect_language("The estimate est given here."), "English")


class TestRealFrenchIsFrench(unittest.TestCase):
    """Genuine French — multiple function words — must still be detected."""

    def test_french_sentence(self):
        text = "Le radar est un système de détection qui utilise les ondes."
        self.assertEqual(detect_language(text), "French")


class TestChineseAndEnglish(unittest.TestCase):
    """Sanity: the dominant-script path still works."""

    def test_chinese_text(self):
        self.assertEqual(detect_language("先进米波雷达是一种重要的雷达体制。"), "Chinese")

    def test_plain_english(self):
        self.assertEqual(detect_language("This is a plain English sentence about radar."), "English")


if __name__ == "__main__":
    unittest.main()
