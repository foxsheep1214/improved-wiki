"""Language detection — NashSU parity (detect-language.ts + output-language.ts).

Supports 25+ languages via Unicode script ranges and Latin-script diacritic/word
patterns. Used by ingest.py for output validation and wiki-lint-semantic.py for
LLM language directives.
"""

import re


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

    # Japanese: Hiragana/Katakana + Kanji → Japanese (not Chinese)
    if counts.get("Japanese", 0) > 0 and counts.get("Chinese", 0) > 0:
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


def build_language_directive(text: str) -> str:
    """Build a language directive string for LLM prompts."""
    lang = detect_language(text[:2000])
    name_map = {
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
    display = name_map.get(lang, lang)
    return f"All output MUST be in {display}. Respond in {lang}."


# ── Script detection ──

_GREEK_WORD_RUN = re.compile(r"[Ͱ-Ͽἀ-῿]{2,}")


def _has_greek_word_run(text: str) -> bool:
    """True if ``text`` contains ≥2 consecutive Greek letters — a word run.

    Isolated single Greek letters (math symbols like λ, σ, Δ) do not form a
    run, so a math-heavy English paragraph returns False and is not
    misclassified as Greek.
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

    # Vietnamese
    if re.search(r"[ảạắằẳẵặấầẩẫậđẻẽẹếềểễệỉĩịỏọốồổỗộơớờởỡợủũụưứừửữựỷỹỵ]", lower):
        return "Vietnamese"
    # Turkish
    if re.search(r"[ğış]", lower) and words & {"bir", "ve", "için", "ile", "bu", "da", "de"}:
        return "Turkish"
    # Polish
    if re.search(r"[ąćęłńóśźż]", lower):
        return "Polish"
    # Czech
    if re.search(r"[ěšžřďťňů]", lower):
        return "Czech"
    # Romanian
    if re.search(r"[ăâîșț]", lower) and words & {"și", "este", "sau", "care", "pentru"}:
        return "Romanian"
    # Hungarian
    if re.search(r"[őű]", lower):
        return "Hungarian"
    # German
    if len(words & {"und", "der", "die", "das", "ist"}) >= 2:
        return "German"
    # French
    if len(words & {"le", "la", "les", "est", "une", "des"}) >= 2:
        return "French"
    # Portuguese (before Spanish — stricter chars)
    if re.search(r"[ãõç]", lower) and words & {"o", "a", "os", "as", "de", "do", "da", "não", "que"}:
        return "Portuguese"
    # Spanish (ñ/¿/¡ alone is a strong signal; otherwise require ≥2 function words)
    if re.search(r"[ñ¿¡]", lower) or len(words & {"el", "los", "las", "del", "por"}) >= 2:
        return "Spanish"
    # Italian
    if len(words & {"il", "della", "gli", "che", "è"}) >= 2:
        return "Italian"
    # Dutch
    if len(words & {"het", "een", "van", "dat"}) >= 2:
        return "Dutch"
    # Swedish
    if re.search(r"[åäö]", lower) and words & {"och", "att", "det", "är", "för"}:
        return "Swedish"
    # Norwegian
    if re.search(r"[åæø]", lower) and words & {"og", "er", "det", "for", "med", "på"}:
        return "Norwegian"
    # Danish
    if re.search(r"[åæø]", lower) and words & {"og", "er", "det", "til", "med", "af"}:
        return "Danish"
    # Finnish
    if re.search(r"[äö]", lower) and words & {"ja", "on", "ei", "se", "että", "tai", "kun"}:
        return "Finnish"
    # Indonesian
    if len(words & {"yang", "dari", "untuk", "dengan", "adalah"}) >= 2:
        return "Indonesian"

    return None
