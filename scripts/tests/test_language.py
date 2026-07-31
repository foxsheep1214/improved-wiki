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


class TestDiacriticNameNotNordic(unittest.TestCase):
    """A single Nordic diacritic (from an author name/affiliation) plus one
    incidental English function word must not flip an English paper to
    Norwegian/Danish/Swedish. Real hit: an Aalborg University arXiv paper
    (English body) with author "Alba Spliid Damkjær" (æ) and "Magnus Ørum
    Bastrup Poulsen" (ø) was misdetected as Norwegian because the abstract
    happened to contain the word "for" — the only Norwegian function word
    that also doubles as common English vocabulary."""

    def test_english_paper_with_danish_author_names_stays_english(self):
        text = (
            "Anders Malthe Westerkam, Alba Spliid Damkjaer, Magnus Oerum "
            "Bastrup Poulsen. Aalborg University, Aalborg Denmark.\n"
            "Abstract—We propose an analytic model for the second-order "
            "characteristics of the radar return signal from a swarm of "
            "rotor drones, presenting new challenges for radar detection."
        ).replace("ae", "æ").replace("Oerum", "Ørum")
        self.assertEqual(detect_language(text), "English")

    def test_single_nordic_function_word_not_enough(self):
        # One diacritic char + exactly one function word ("for") must not
        # be enough on its own (mirrors the German/French ≥2 threshold).
        self.assertEqual(
            detect_language("Poulsen Damkjær reaching for the radar data."),
            "English",
        )

    def test_real_norwegian_still_detected(self):
        text = "Vi målte støyen på radarsystemet og fant gode resultater."
        self.assertEqual(detect_language(text), "Norwegian")

    def test_real_danish_still_detected(self):
        text = "Dette system bruges til at måle støj fra dronen og fugle."
        self.assertEqual(detect_language(text), "Danish")


class TestMathAndAcronymFalsePositivesStayEnglish(unittest.TestCase):
    """Broader sweep (2026-07-15) of the same false-positive pattern across
    other detectors, found by scanning RadarWiki/HardwareWiki for pages whose
    body language came out neither Chinese nor English."""

    def test_two_letter_greek_math_pairs_stay_english(self):
        # σθ, αβ (alpha-beta tracking filter), 2πΔf — two single-letter Greek
        # symbols written back to back with no separator is common notation,
        # not a Greek word. Real hits: pa-vs-fda-vs-mimo-vs-fda-mimo.md,
        # classical-control-for-radar-servo-tracking.md.
        text = "Scaling σθ≈θbw/(km√(2SNR)). This method best matches an αβ/Kalman tracker over 2πΔf bandwidth."
        self.assertEqual(detect_language(text), "English")

    def test_los_el_radar_acronyms_not_spanish(self):
        # "LOS" (line-of-sight) and "EL" (elevation) lowercase to "los"/"el",
        # which used to be 2 of Spanish's 5 function words. Real hit:
        # satellite-communication-link-geometry-and-loss-budget.md.
        text = "LOS loss = 32.44 + 20 log(distance) + 20 log(freq), computed from the EL and AZ angles."
        self.assertEqual(detect_language(text), "English")

    def test_stray_tilde_char_not_portuguese(self):
        # A single ã/õ/ç from a tilde-accented math symbol plus incidental
        # "a"/"as" (both dropped from the word set) used to be enough.
        # Real hit: complementary-golay-codes.md.
        text = "The estimator ã is used here, as well as a related bound derived from the same sequence."
        self.assertEqual(detect_language(text), "English")

    def test_stray_tilde_vowel_not_vietnamese(self):
        # A single precomposed tilde/circumflex vowel (ũ, ẽ, ...) is exactly
        # how an "estimate"/"conjugate" math symbol renders over a Latin
        # letter. Real hit: cramer-rao-bound-for-mimo-radar.md, with
        # equations like "ũ†(...)" and "c̃".
        text = "The received signal model f = 2|b|^2 k^2 re{n_r ũ†(x - m)} uses ũ as the whitened vector."
        self.assertEqual(detect_language(text), "English")

    def test_cuk_converter_name_not_polish(self):
        # "Ćuk" (the Ćuk converter, named after Slobodan Ćuk) is the only
        # diacritic in an all-English power-electronics page. Real hits:
        # cuk-converter.md, buck-boost-converter-dc-dc.md, Hart's Power
        # Electronics textbook source page.
        text = "The Ćuk converter is a type of DC-DC converter named after Slobodan Ćuk, providing inverted output."
        self.assertEqual(detect_language(text), "English")

    def test_japanese_loanword_in_chinese_page_stays_chinese(self):
        # パス ("pass", as in a filter's passband) cited once inside an
        # otherwise Chinese circuit-design page used to flip the whole page
        # to "Japanese" off 4 stray kana characters against 500+ Han
        # characters. Real hit: "Bandstop filters Bainter topology" page.
        text = (
            "许多应用需要陷波滤波器（bandstop/notch filter）来消除特定频率信号，"
            "如音频信号处理、助听器反馈抑制、工频噪声抑制等。关键参数定义："
            "f0为陷波中心频率，带宽定义品质因数，通带パス特性决定滤波器性能，"
            "工程师需要根据具体应用场景选择合适的滤波器拓扑结构和元器件参数。"
        )
        self.assertEqual(detect_language(text), "Chinese")

    def test_genuine_japanese_still_detected(self):
        text = "これは日本語のテキストです。レーダーについて説明します。"
        self.assertEqual(detect_language(text), "Japanese")

    def test_genuine_polish_still_detected(self):
        text = "To jest bardzo ważne, że nie możemy zapomnieć o tym problemie, który się pojawił."
        self.assertEqual(detect_language(text), "Polish")

    def test_genuine_czech_still_detected(self):
        text = "Tento systém se používá pro sledování letadel a dronů, což je velmi užitečné pro obranu."
        self.assertEqual(detect_language(text), "Czech")

    def test_genuine_hungarian_still_detected(self):
        text = "Ez egy fontos kérdés, és sokan gondolkodnak róla a jövőben is."
        self.assertEqual(detect_language(text), "Hungarian")

    def test_genuine_vietnamese_still_detected(self):
        text = "Đây là một câu tiếng Việt để kiểm tra việc phát hiện ngôn ngữ."
        self.assertEqual(detect_language(text), "Vietnamese")

    def test_genuine_portuguese_still_detected(self):
        text = "Este sistema utiliza um radar para detectar aviões não tripulados, o que é muito importante."
        self.assertEqual(detect_language(text), "Portuguese")

    def test_genuine_spanish_still_detected(self):
        text = "Esta es una técnica de detección por radar, muy útil para el seguimiento de objetivos también."
        self.assertEqual(detect_language(text), "Spanish")

    def test_german_org_name_expansion_not_german(self):
        # Real hit (HardwareWiki entities/VDE.md, found 2026-07-30 while
        # verifying the non-Latin share fix): spelling out a German standards
        # body's official name inside an English page supplies two function
        # words on its own — "Verband DER Elektrotechnik, Elektronik UND
        # Informationstechnik" — which used to be the entire bar for German.
        # Same proper-noun false-positive class as Ćuk (Polish) and LOS/EL
        # (Spanish); an English page citing VDE, TÜV or a German paper title
        # must stay English.
        text = (
            "VDE is a European safety specification (Verband der "
            "Elektrotechnik, Elektronik und Informationstechnik). In the "
            "context of transformer design, VDE standards require specific "
            "insulation creepage distances that affect the winding space "
            "factor and the overall isolation barrier construction."
        )
        self.assertEqual(detect_language(text), "English")

    def test_genuine_german_still_detected(self):
        text = (
            "Das ist ein wichtiger Punkt für die Konstruktion, und der "
            "Wirkungsgrad der Schaltung ist dabei entscheidend."
        )
        self.assertEqual(detect_language(text), "German")

    def test_fpga_logic_element_acronym_not_french(self):
        # Real hit (RadarWiki concepts/fpga-architecture-for-ew-systems.md,
        # found 2026-07-30 while verifying the non-Latin share fix): "LE"
        # (Logic Element) and its plural "LEs" lowercase to the French
        # articles le/les, which used to be 2 of French's 6 function words.
        # Identical to the LOS/EL → Spanish hit already fixed above.
        text = (
            "LE count has grown enormously: from 1,728 LEs in a legacy FPGA "
            "to 5,540,850 LEs in a new advanced device. Each LE contains a "
            "lookup table and a register, and the routing fabric between LEs "
            "dominates both area and propagation delay in this architecture."
        )
        self.assertEqual(detect_language(text), "English")


class TestNonLatinScriptNeedsShareNotJustPresence(unittest.TestCase):
    """A handful of stray non-Latin characters must not outvote thousands of
    ASCII letters (known-issues.md, fixed 2026-07-30).

    The dominant-script test used to be a bare absolute count
    (``max_count >= 2``). Latin-script text is pure ASCII and contributes
    nothing to the script counts, so ANY two non-Latin characters anywhere in
    an otherwise all-English document won the vote outright. Latin-script
    detectors had already been hardened one by one (Greek word runs, Polish
    Ćuk, Portuguese ã, Vietnamese ũ, Spanish LOS/EL); the non-Latin branch
    had no equivalent guard at all.

    Measured on the real HardwareWiki corpus before the fix: 346 pages whose
    only Han characters were the pipeline's own ``参见`` / ``据图`` boilerplate
    were being detected as Chinese, and genuine Chinese pages sat at 15-90%
    share — a gap of two orders of magnitude, so a 5% floor separates them
    cleanly."""

    def test_pipeline_boilerplate_han_does_not_flip_english_page(self):
        # Real hit (346 HardwareWiki pages): Stage 2.4/2.6 inject the Chinese
        # figure-citation word 据图 and the see-also heading 参见 into pages
        # whose prose is entirely English. 2 Han chars vs ~1500 ASCII letters.
        text = (
            "Stripline return current flows on the reference planes directly "
            "above and below the trace, concentrated under the signal path. "
            "Splitting that plane forces the return current to detour around "
            "the gap, which raises loop inductance and radiates. Keep the "
            "reference continuous beneath every high-speed net, and place "
            "stitching vias where a signal changes reference layer. 据图 "
            "the measured impedance discontinuity grows with gap width. 参见"
        )
        self.assertEqual(detect_language(text), "English")

    def test_foreign_library_stamp_on_scanned_title_page_stays_english(self):
        # The originally reported symptom: OCR of a scanned English textbook's
        # title page picks up a dozen Cyrillic characters from a library
        # ownership stamp, and the whole book's generation stages then receive
        # a "MANDATORY OUTPUT LANGUAGE: Russian" directive.
        text = (
            "МОСКВА БИБЛИОТЕКА "  # library stamp OCR'd off the title page
            "Fundamentals of Power Electronics. This textbook develops the "
            "converter modeling techniques used throughout the power "
            "electronics field, beginning with steady-state converter "
            "analysis and the principles of inductor volt-second balance and "
            "capacitor charge balance. Later chapters cover small-signal "
            "averaged models, converter transfer functions, and the design "
            "of feedback loops for switching regulators operating in both "
            "continuous and discontinuous conduction modes."
        )
        self.assertEqual(detect_language(text), "English")

    def test_single_foreign_citation_does_not_flip_english_document(self):
        text = (
            "The original derivation appears in Котельников's 1933 sampling "
            "paper, but the result is usually attributed to Shannon in the "
            "English-language literature. This section restates the sampling "
            "theorem in the form used later for bandpass sampling, then "
            "applies it to the intermediate-frequency digitizer design and "
            "works through the aliasing budget for the chosen sample rate."
        )
        self.assertEqual(detect_language(text), "English")

    def test_genuine_chinese_prose_still_detected(self):
        text = (
            "热设计的核心是建立从芯片结点到环境的完整传热路径。"
            "热阻是描述这一路径的基本参数，串联各段热阻即可估算结温。"
            "工程上还需要考虑接触热阻、界面材料的压力与厚度关系。"
        )
        self.assertEqual(detect_language(text), "Chinese")

    def test_chinese_page_heavy_with_english_identifiers_stays_chinese(self):
        # HardwareWiki reality: Chinese hardware pages carry many English part
        # numbers and units. Han share drops but stays far above the floor.
        text = (
            "本页说明 BCM56970 交换芯片与 Intel Xeon D-1518 之间的气流耦合问题。"
            "在 FloTHERM 热仿真中，方案5 的结温达到 135°C，超出 110°C 规格；"
            "将 CPU 左移、散热器加宽加高、基板改用均温板后降至 108°C。"
            "相关标准为 GR-63-CORE (NEBS) 与 IEC 62368-1, 环境温度取 45°C。"
        )
        self.assertEqual(detect_language(text), "Chinese")

    def test_short_pure_cjk_string_still_detected(self):
        # A short string with no ASCII at all must still resolve — the share
        # gate must not require a long document.
        self.assertEqual(detect_language("北京大学"), "Chinese")

    def test_genuine_russian_still_detected(self):
        text = (
            "Радиолокационная станция обнаруживает цели с помощью "
            "отражённого сигнала и измеряет дальность по времени задержки."
        )
        self.assertEqual(detect_language(text), "Russian")

    def test_genuine_korean_still_detected(self):
        text = "이 문서는 레이더 신호 처리에 대한 한국어 설명을 제공합니다."
        self.assertEqual(detect_language(text), "Korean")

    def test_stray_kana_and_han_together_do_not_flip_english_page(self):
        # The Japanese branch compares kana against Han only — both being
        # incidental against a wall of ASCII must be filtered before that
        # relative comparison runs.
        text = (
            "The datasheet's Japanese edition is titled パス and 通過, but "
            "the rest of this application note is written in English and "
            "describes the passband ripple budget for the anti-aliasing "
            "filter ahead of the analog-to-digital converter stage, "
            "including the group-delay variation across the passband."
        )
        self.assertEqual(detect_language(text), "English")


class TestOutputLanguageCollapsesToTwoLanguages(unittest.TestCase):
    """Policy (user ruling 2026-07-15): the wiki only ever holds Chinese or
    English pages. Any detected source language other than Chinese must
    collapse to English — it must NOT return the source's own language."""

    def setUp(self):
        self._old = os.environ.get(OUTPUT_LANGUAGE_ENV)
        os.environ.pop(OUTPUT_LANGUAGE_ENV, None)

    def tearDown(self):
        if self._old is None:
            os.environ.pop(OUTPUT_LANGUAGE_ENV, None)
        else:
            os.environ[OUTPUT_LANGUAGE_ENV] = self._old

    def test_chinese_source_stays_chinese(self):
        self.assertEqual(get_output_language("先进米波雷达是一种重要的雷达体制。"), "Chinese")

    def test_english_source_stays_english(self):
        self.assertEqual(get_output_language("This is a plain English sentence."), "English")

    def test_french_source_collapses_to_english(self):
        text = "Le radar est un système de détection qui utilise les ondes."
        self.assertEqual(detect_language(text), "French")  # raw detector unchanged
        self.assertEqual(get_output_language(text), "English")  # policy collapses it

    def test_norwegian_source_collapses_to_english(self):
        text = "Vi målte støyen på radarsystemet og fant gode resultater."
        self.assertEqual(detect_language(text), "Norwegian")  # raw detector unchanged
        self.assertEqual(get_output_language(text), "English")  # policy collapses it

    def test_japanese_source_collapses_to_english(self):
        text = "これは日本語のテキストです。レーダーについて説明します。"
        self.assertEqual(get_output_language(text), "English")


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
