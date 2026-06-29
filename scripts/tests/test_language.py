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

import os
import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _language import (  # noqa: E402
    detect_language,
    build_language_directive,
    get_output_language,
    OUTPUT_LANGUAGE_ENV,
)


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


class TestDirectivePreservationClauses(unittest.TestCase):
    """build_language_directive must port NashSU buildLanguageDirective's
    preservation rules so the LLM localizes prose but NEVER translates
    proper nouns, technical identifiers, URLs, paper titles, or code."""

    def test_directive_states_mandatory_language(self):
        directive = build_language_directive("This is plain English text.")
        self.assertIn("MANDATORY OUTPUT LANGUAGE", directive)
        self.assertIn("English", directive)

    def test_directive_has_proper_noun_preservation(self):
        directive = build_language_directive("先进米波雷达是一种重要的雷达体制。")
        # Localized prose language is Chinese...
        self.assertIn("Chinese (中文)", directive)
        # ...but the preservation clauses must be present verbatim.
        self.assertIn("Do not translate, transliterate", directive)
        self.assertIn("proper nouns", directive)
        self.assertIn("organization names", directive)
        self.assertIn("acronyms", directive)
        self.assertIn("code identifiers", directive)
        self.assertIn("file names", directive)
        self.assertIn("URLs", directive)
        self.assertIn("paper titles", directive)
        self.assertIn("citation strings", directive)

    def test_directive_has_override_ordering_clause(self):
        directive = build_language_directive("plain English")
        self.assertIn("overrides weaker style instructions", directive)
        self.assertIn("does not override", directive)


class TestOutputLanguageOverride(unittest.TestCase):
    """IMPROVED_WIKI_OUTPUT_LANGUAGE forces the output language regardless of
    the source text (NashSU getOutputLanguage parity)."""

    def setUp(self):
        self._old = os.environ.get(OUTPUT_LANGUAGE_ENV)

    def tearDown(self):
        if self._old is None:
            os.environ.pop(OUTPUT_LANGUAGE_ENV, None)
        else:
            os.environ[OUTPUT_LANGUAGE_ENV] = self._old

    def test_auto_default_detects_from_text(self):
        os.environ.pop(OUTPUT_LANGUAGE_ENV, None)
        self.assertEqual(get_output_language("先进米波雷达是雷达体制。"), "Chinese")

    def test_explicit_auto_value_still_detects(self):
        os.environ[OUTPUT_LANGUAGE_ENV] = "auto"
        self.assertEqual(get_output_language("先进米波雷达是雷达体制。"), "Chinese")

    def test_override_forces_language_over_source(self):
        os.environ[OUTPUT_LANGUAGE_ENV] = "French"
        # Source is Chinese, but override forces French.
        self.assertEqual(get_output_language("先进米波雷达是雷达体制。"), "French")
        directive = build_language_directive("先进米波雷达是雷达体制。")
        self.assertIn("French", directive)
        self.assertNotIn("Chinese (中文)", directive)

    def test_override_blank_falls_back_to_detect(self):
        os.environ[OUTPUT_LANGUAGE_ENV] = ""
        self.assertEqual(get_output_language("先进米波雷达是雷达体制。"), "Chinese")


if __name__ == "__main__":
    unittest.main()
