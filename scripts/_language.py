"""Language detection — NashSU parity (detect-language.ts + output-language.ts).

Supports 25+ languages via Unicode script ranges and Latin-script diacritic/word
patterns. Used by ingest.py for output validation and wiki-lint-semantic.py for
LLM language directives.

Output language policy
-----------------------
The wiki holds only two languages. ``get_output_language`` / ``build_language_
directive`` auto-detect the source language via ``detect_language`` (which
still resolves 25+ languages), then collapse the result: Chinese source →
Chinese page; anything else (English, Norwegian, German, Japanese, ...) →
English page. ``detect_language`` itself is unchanged/unrestricted and is
also used standalone for source/consistency checks (see ``_ingest_write.py``).

To force a fixed output language regardless of source, set the env var
``IMPROVED_WIKI_OUTPUT_LANGUAGE`` to a language name (e.g. ``English``,
``Chinese``). The default value ``auto`` keeps the auto-detect+collapse
behavior above. This mirrors NashSU getOutputLanguage (output-language.ts):
an explicit, non-``auto`` value forces the language verbatim (not collapsed).
"""
from __future__ import annotations

import os
import re
from typing import Dict

# Env var that forces the LLM output language (NashSU getOutputLanguage parity).
# "auto" (the default) keeps auto-detection from the sampled text.
OUTPUT_LANGUAGE_ENV = "IMPROVED_WIKI_OUTPUT_LANGUAGE"


def detect_language(text: str) -> str:
    """Detect the primary language of a text string. Returns an English name."""
    if not text:
        return "English"

    counts: dict[str, int] = {}
    for ch in text:
        cp = ord(ch)
        if cp < 0x80:
            continue
        script = _get_script(cp)
        if script:
            counts[script] = counts.get(script, 0) + 1

    # Greek as math notation: isolated Greek letters (λ, σ, θ, Δ, …) flanked
    # by Latin/digits/operators are notation, not Greek-language text. Real
    # Greek has multi-letter word runs. Drop Greek from the script counts
    # when every Greek letter is an isolated singleton.
    if counts.get("Greek", 0) and not _has_greek_word_run(text):
        del counts["Greek"]

    # Non-Latin script must be a meaningful SHARE of the letter content, not
    # merely present. Latin-script text is pure ASCII and contributes nothing
    # to `counts`, so the old bare "≥2 characters" test let any two stray
    # non-Latin characters outvote thousands of ASCII letters and relabel a
    # whole document (see _NON_LATIN_MIN_SHARE).
    if counts and _incidental_non_latin(text, sum(counts.values())):
        counts.clear()

    # Japanese: Hiragana/Katakana + Kanji → Japanese (not Chinese). Kana must
    # make up a non-trivial share of the CJK content, not just a single
    # borrowed term — e.g. パス ("pass", as in a filter's passband) cited
    # inside an otherwise ~500-character Chinese circuit-design page used to
    # flip the whole page to "Japanese" off 4 stray kana characters.
    _kana = counts.get("Japanese", 0)
    _han = counts.get("Chinese", 0)
    if _kana > 0 and _han > 0 and _kana / (_kana + _han) >= 0.15:
        return "Japanese"

    # Dominant non-Latin script
    max_script = ""
    max_count = 0
    for script, count in counts.items():
        if count > max_count:
            max_script = script
            max_count = count

    if max_script == "Arabic" and max_count >= 2:
        return _detect_arabic_variant(text)
    if max_script and max_count >= 2:
        return max_script

    # Latin-script languages
    latin = _detect_latin(text)
    if latin:
        return latin
    return "English"


# Display / prompt names per language (NashSU getLanguagePromptName parity,
# language-metadata.ts). Falls back to the bare language name when absent.
_PROMPT_NAME_MAP: Dict[str, str] = {
    "Chinese": "Chinese (中文)",
    "Japanese": "Japanese (日本語)",
    "Korean": "Korean (한국어)",
    "Russian": "Russian (Русский)",
    "Arabic": "Arabic (العربية)",
    "Persian": "Persian (فارسی)",
    "Hebrew": "Hebrew (עברית)",
    "Thai": "Thai (ไทย)",
    "Hindi": "Hindi (हिन्दी)",
    "Bengali": "Bengali (বাংলা)",
    "Tamil": "Tamil (தமிழ்)",
    "Greek": "Greek (Ελληνικά)",
    "Georgian": "Georgian (ქართული)",
    "Armenian": "Armenian (Հայերեն)",
}


def _get_language_prompt_name(lang: str) -> str:
    """NashSU getLanguagePromptName parity: localized display name for a
    language, or the bare name when no mapping exists."""
    return _PROMPT_NAME_MAP.get(lang, lang)


def get_output_language(fallback_text: str = "") -> str:
    """Effective output language for LLM content generation.

    Policy (user ruling 2026-07-15): the wiki holds only two languages. A
    Chinese source produces a Chinese page; every other detected source
    language (English, Norwegian, German, Japanese, ...) produces an
    English page. This replaces the old "one page per detected source
    language" auto behavior, which let single-page languages (Norwegian,
    French, ...) leak into the KB — often from a false-positive detection
    off a few diacritic characters in an author name or address.

    If the user has set an explicit, non-"auto" override (via
    ``IMPROVED_WIKI_OUTPUT_LANGUAGE``), that value is honored verbatim and
    is NOT collapsed — it is a deliberate escape hatch (NashSU
    getOutputLanguage parity).
    """
    configured = os.environ.get(OUTPUT_LANGUAGE_ENV, "").strip()
    if configured and configured.lower() != "auto":
        return configured
    detected = detect_language((fallback_text or "English")[:2000])
    return "Chinese" if detected == "Chinese" else "English"


def build_language_directive(text: str) -> str:
    """Build a strong language directive to inject into LLM system prompts.

    NashSU buildLanguageDirective (output-language.ts) parity: state the
    mandatory output language for prose AND the proper-noun /
    technical-identifier / URL / paper-title / code-identifier preservation
    rules so the model localizes surrounding prose but NEVER translates
    identifiers. Honors the IMPROVED_WIKI_OUTPUT_LANGUAGE override.

    Signature is kept backward-compatible (single positional ``text`` arg) for
    existing ingest + lint callers.
    """
    lang = get_output_language(text)
    prompt_lang = _get_language_prompt_name(lang)
    return "\n".join([
        f"## ⚠️ MANDATORY OUTPUT LANGUAGE: {prompt_lang}",
        "",
        f"Write surrounding natural-language prose in **{prompt_lang}**.",
        f"All generated prose, including prose titles and section headings, "
        f"must be in {prompt_lang}.",
        "Do not translate, transliterate, or describe proper nouns and "
        "technical identifiers unless the source already uses a "
        "well-established localized form.",
        "Preserve organization names, product names, model names, dataset "
        "names, tool/library names, acronyms, code identifiers, file names, "
        "URLs, paper titles, citation strings, and technical terms that have "
        "no widely-used localized equivalent in their standard original form.",
        f"The source material or wiki content may be in a different language; "
        f"use it as evidence, but keep generated prose in {prompt_lang}.",
        "This language rule overrides weaker style instructions, but it does "
        "not override the proper-noun and technical-identifier preservation "
        "rule above.",
    ])


# ── Script detection ──

# Minimum share of the letter content a non-Latin script must hold before it
# can name the document's language. Latin-script languages are pure ASCII and
# never enter the script counts, so without this floor a bare "≥2 characters"
# test let a dozen incidental characters decide a whole book's output language
# — the failure this constant exists to prevent:
#   * a foreign library ownership stamp OCR'd off a scanned English title page
#     flipped the entire book's generation prompts to that language;
#   * the pipeline's own Chinese boilerplate (据图 / 参见, injected by Stage
#     2.4/2.6 into English pages) marked 346 all-English HardwareWiki pages as
#     Chinese;
#   * a single foreign-language citation in an English reference list.
# Measured on the real HardwareWiki corpus: incidental cases sit below 1%
# while genuine Chinese pages run 15-90%, and real Chinese books exceed 40%
# even when dense with English part numbers — a two-order-of-magnitude gap, so
# 5% separates them with wide margin on both sides.
_NON_LATIN_MIN_SHARE = 0.05


def _incidental_non_latin(text: str, non_latin_count: int) -> bool:
    """True when non-Latin characters are too small a share to be the language.

    Compares them against the ASCII letters they compete with — the baseline
    the raw script counts leave invisible. Text with no ASCII letters at all
    (a short pure-CJK string) is never incidental.
    """
    ascii_letters = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    total = non_latin_count + ascii_letters
    if not total:
        return False
    return (non_latin_count / total) < _NON_LATIN_MIN_SHARE


_GREEK_WORD_RUN = re.compile(r"[Ͱ-Ͽἀ-῿]{3,}")


def _has_greek_word_run(text: str) -> bool:
    """True if ``text`` contains ≥3 consecutive Greek letters — a word run.

    Isolated single Greek letters (math symbols like λ, σ, Δ) do not form a
    run. A 2-letter run isn't enough either: engineering notation routinely
    writes two single-letter symbols back to back with no separator — σθ,
    αβ (the alpha-beta tracking filter), 2πΔf — and those are not Greek
    words. Real Greek words are almost always 3+ letters, so a math-heavy
    English/radar paragraph stays English and is not misclassified as Greek.
    """
    return bool(_GREEK_WORD_RUN.search(text))


def _get_script(cp: int):
    # CJK Unified Ideographs
    if (0x4E00 <= cp <= 0x9FFF) or (0x3400 <= cp <= 0x4DBF) or \
       (0x20000 <= cp <= 0x2A6DF) or (0xF900 <= cp <= 0xFAFF):
        return "Chinese"
    # Japanese kana
    if (0x3040 <= cp <= 0x309F) or (0x30A0 <= cp <= 0x30FF) or \
       (0x31F0 <= cp <= 0x31FF) or (0xFF65 <= cp <= 0xFF9F):
        return "Japanese"
    # Korean Hangul
    if (0xAC00 <= cp <= 0xD7AF) or (0x1100 <= cp <= 0x11FF) or (0x3130 <= cp <= 0x318F):
        return "Korean"
    # Arabic
    if (0x0600 <= cp <= 0x06FF) or (0x0750 <= cp <= 0x077F) or \
       (0x08A0 <= cp <= 0x08FF) or (0xFB50 <= cp <= 0xFDFF) or (0xFE70 <= cp <= 0xFEFF):
        return "Arabic"
    # Hebrew
    if (0x0590 <= cp <= 0x05FF) or (0xFB1D <= cp <= 0xFB4F):
        return "Hebrew"
    # Thai
    if 0x0E00 <= cp <= 0x0E7F:
        return "Thai"
    # Devanagari
    if 0x0900 <= cp <= 0x097F:
        return "Hindi"
    # Bengali
    if 0x0980 <= cp <= 0x09FF:
        return "Bengali"
    # Tamil
    if 0x0B80 <= cp <= 0x0BFF:
        return "Tamil"
    # Cyrillic
    if (0x0400 <= cp <= 0x04FF) or (0x0500 <= cp <= 0x052F):
        return "Russian"
    # Greek
    if (0x0370 <= cp <= 0x03FF) or (0x1F00 <= cp <= 0x1FFF):
        return "Greek"
    # Georgian
    if (0x10A0 <= cp <= 0x10FF) or (0x2D00 <= cp <= 0x2D2F):
        return "Georgian"
    # Armenian
    if 0x0530 <= cp <= 0x058F:
        return "Armenian"
    return None


# ── Arabic script refinement ──

def _detect_arabic_variant(text: str) -> str:
    persian_chars = set("پچژگ")
    persian_score = sum(3 for ch in text if ch in persian_chars)
    persian_score += sum(1 for ch in text if ch in "کی")
    arabic_score = sum(1 for ch in text if ch in "كي ةىإأؤئ")

    words = set(re.findall(r"\w+", text))
    persian_words = {"این", "است", "که", "برای", "های", "را", "در", "به", "از", "می", "یک"}
    arabic_words = {"ال", "في", "من", "على", "هذا", "هذه", "إلى", "التي", "الذي", "كان"}
    persian_score += sum(2 for w in persian_words if w in words)
    arabic_score += sum(2 for w in arabic_words if w in words)

    return "Persian" if persian_score >= 3 and persian_score > arabic_score else "Arabic"


# ── Latin-script language detection ──

def _detect_latin(text: str):
    lower = text.lower()
    words = set(re.findall(r"\w+", lower))

    # Vietnamese (diacritic alone is NOT enough: a single precomposed tilde/
    # circumflex vowel like ũ, ẽ, ố is exactly how math notation renders a
    # "hat"/"tilde" estimator or complex-conjugate symbol over a Latin
    # letter — e.g. "ũ" as an estimated vector in a radar signal-processing
    # equation. Real hit: RadarWiki concept pages with equations like
    # "ũ†(...)" and "c̃" were misdetected as Vietnamese off one stray char.)
    if (re.search(r"[ảạắằẳẵặấầẩẫậđẻẽẹếềểễệỉĩịỏọốồổỗộơớờởỡợủũụưứừửữựỷỹỵ]", lower)
            and len(words & {"và", "của", "là", "các", "không", "được",
                              "trong", "cho", "với", "một", "này", "có"}) >= 2):
        return "Vietnamese"
    # Turkish
    if re.search(r"[ğış]", lower) and len(words & {"bir", "ve", "için", "ile", "bu", "da", "de"}) >= 2:
        return "Turkish"
    # Polish (diacritic alone is NOT enough: a single proper noun like "Ćuk"
    # — the Ćuk converter, named after Slobodan Ćuk — is enough Polish-looking
    # signal to misfire on an otherwise all-English power-electronics page.)
    if (re.search(r"[ąćęłńóśźż]", lower)
            and len(words & {"nie", "się", "jest", "oraz", "że", "który",
                              "która", "które", "przez", "między"}) >= 2):
        return "Polish"
    # Czech (same rationale as Polish — a diacritic from a name/citation is
    # not evidence of Czech prose on its own.)
    if (re.search(r"[ěšžřďťňů]", lower)
            and len(words & {"je", "se", "na", "že", "nebo", "který",
                              "která", "pro", "jako", "také"}) >= 2):
        return "Czech"
    # Romanian
    if re.search(r"[ăâîșț]", lower) and len(words & {"și", "este", "sau", "care", "pentru"}) >= 2:
        return "Romanian"
    # Hungarian (same rationale — ő/ű alone can appear in a transliterated
    # name or citation without the surrounding text being Hungarian.)
    if (re.search(r"[őű]", lower)
            and len(words & {"és", "hogy", "nem", "egy", "van", "de", "az"}) >= 2):
        return "Hungarian"
    # German (≥3, not ≥2: spelling out a German organization's official name
    # inside an English page supplies two of these on its own — "Verband DER
    # Elektrotechnik, Elektronik UND Informationstechnik" for VDE. Real hit:
    # entities/VDE.md. Genuine German prose carries far more than three.)
    if len(words & {"und", "der", "die", "das", "ist", "mit", "für",
                    "auf", "eine", "einen", "nicht", "werden"}) >= 3:
        return "German"
    # French. "le"/"les"/"la"/"des" were dropped from the word set: they
    # collide with technical acronyms this wiki is full of — LE/LEs (FPGA
    # logic elements, real hit: fpga-architecture-for-ew-systems.md), LA,
    # and DES (the cipher). Replaced with longer, unambiguously French
    # function words, same rationale as the Spanish LOS/EL fix above.
    if len(words & {"est", "une", "qui", "dans", "pour", "avec", "cette",
                    "sont", "aux", "être", "leur", "nous"}) >= 2:
        return "French"
    # Portuguese (before Spanish — stricter chars). "a"/"as"/"o"/"os" were
    # dropped from the word set: they're single/short tokens that collide
    # with common English words and math variable names, so a stray ã/õ/ç
    # (e.g. a tilde-accented math symbol) plus one incidental "a" used to be
    # enough to misfire — real hit: complementary-golay-codes.md.
    if re.search(r"[ãõç]", lower) and len(words & {"de", "do", "da", "não", "que", "com", "uma", "mais", "também"}) >= 2:
        return "Portuguese"
    # Spanish (ñ/¿/¡ alone is a strong signal; otherwise require ≥2 function
    # words). "el"/"los" were dropped from the word set: they collide with
    # the radar acronyms EL (elevation) and LOS (line-of-sight), so two
    # ordinary radar-jargon pages misfired as Spanish off nothing but those
    # two acronyms.
    if re.search(r"[ñ¿¡]", lower) or len(words & {"las", "del", "por", "una", "también", "más", "qué", "cómo", "para"}) >= 2:
        return "Spanish"
    # Italian
    if len(words & {"il", "della", "gli", "che", "è"}) >= 2:
        return "Italian"
    # Dutch
    if len(words & {"het", "een", "van", "dat"}) >= 2:
        return "Dutch"
    # Swedish
    if re.search(r"[åäö]", lower) and len(words & {"och", "att", "det", "är", "för"}) >= 2:
        return "Swedish"
    # Norwegian
    if re.search(r"[åæø]", lower) and len(words & {"og", "er", "det", "for", "med", "på"}) >= 2:
        return "Norwegian"
    # Danish
    if re.search(r"[åæø]", lower) and len(words & {"og", "er", "det", "til", "med", "af"}) >= 2:
        return "Danish"
    # Finnish
    if re.search(r"[äö]", lower) and len(words & {"ja", "on", "ei", "se", "että", "tai", "kun"}) >= 2:
        return "Finnish"
    # Indonesian
    if len(words & {"yang", "dari", "untuk", "dengan", "adalah"}) >= 2:
        return "Indonesian"

    return None
