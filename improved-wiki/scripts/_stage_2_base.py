"""Phase 2 common imports — shared by all Stage 2.x modules.

Reduces 15-line import blocks duplicated across 6 files to a single import.
Created 2026-06-21 as part of Phase 2 consolidation.
"""
from __future__ import annotations

import json, os, re, sys, time, unicodedata
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from _core import (
    Config,
    record_rate_limit as _record_rate_limit,
    list_existing_slugs,
    load_schema_md,
    schema_folders,
    BASE_PAGE_DIRS,
    parse_yaml_block,
    parse_file_blocks,
    parse_simple_yaml,
    slugify,
    atomic_write,
    call_with_retry,
)
from _llm_api import (
    _retry_jitter,
    _is_retryable_exception,
    call_anthropic_protocol,
)
from _frontmatter import extract_frontmatter_title as _extract_fm_title

# Folders that may appear in schema.md but are not LLM-generated page types.
# Shared by Stage 2.2 (analysis) and 2.4 (generation) schema routing
# (NashSU schema-typed-candidates parity).
SCHEMA_NON_PAGE_DIRS = frozenset({"media", "raw", "page-history", "chats"})


def _stage_2_frontmatter_title(content: str) -> str:
    """Extract a page's `title:` frontmatter value, quotes stripped.

    Shared by Stage 2.3 / 2.8's title-Jaccard matching — frontmatter titles
    are written as `title: "Foo Bar"`, and feeding the raw quoted string into
    a word-split silently weakens every match by one token on each end (the
    first/last word carries a stray `"`).
    """
    return _extract_fm_title(content)


_TITLE_STOPWORDS = {
    "and", "or", "of", "the", "in", "on", "at", "to", "an", "vs",
    "for", "with", "by", "a", "as", "from",
}


def _stage_2_title_words(title: str) -> set:
    """Content-word token set for title-overlap Jaccard matching.

    Case-insensitive, len>1, with connective stopwords removed. Without the
    stopword strip, "Series and parallel capacitors" vs "Series and parallel
    batteries" shared {series, and, parallel} → Jaccard 0.6, so a Stage 2.3
    association wrongly flagged the capacitor concept as ALREADY COVERED by the
    battery page (book-2 re-ingest). Dropping connectives lets the head noun
    decide the match.

    Each token is also accent-folded (NFKD, drop combining marks) and stripped
    of non-alphanumerics before comparison, so orthographic variants of the SAME
    title still match. Without this, "Thévenin's Theorem" (existing page) vs
    "Thevenin's Theorem" (new) tokenized to {thévenin's,theorem} vs
    {thevenin's,theorem} → Jaccard 0.33, letting a duplicate slip past Stage 2.3
    dedup (Op Amps re-ingest, 2026-06-30). Folding both to {thevenins,theorem}
    → Jaccard 1.0. Folding is applied identically to both sides, so it cannot
    create a false match between genuinely different head nouns.
    """
    words = set()
    for w in re.split(r"[\s/]+", title):
        folded = unicodedata.normalize("NFKD", w)
        folded = "".join(c for c in folded if not unicodedata.combining(c))
        tok = re.sub(r"[^a-z0-9]+", "", folded.lower())
        if len(tok) > 1 and tok not in _TITLE_STOPWORDS:
            words.add(tok)
    return words


_TITLE_CJK_RUN_RE = re.compile("[\\u3400-\\u4dbf\\u4e00-\\u9fff]+")


def _stage_2_title_cjk_bigrams(title: str) -> set:
    """CJK character-bigram token set for title-overlap Jaccard matching.

    A4 (audit 2026-07-02, H1 layer 2): `_stage_2_title_words` keeps only
    `[a-z0-9]`, so a pure-CJK title tokenizes to the EMPTY set and Stage 2.3's
    Jaccard branch (both sides must be non-empty) never fires — Chinese
    concepts fell back to exact slug match only (匹配滤波 ×5 pages coexisting).
    Character bigrams over each maximal CJK run give CJK titles a usable token
    set: "匹配滤波器理论" → {匹配, 配滤, 滤波, 波器, 器理, 理论}. A length-1
    run contributes its single character. Kept SEPARATE from the ASCII word
    set (own Jaccard branch in Stage 2.3) so mixed CJK+Latin titles don't
    dilute existing ASCII-token matches.
    """
    tokens = set()
    for run in _TITLE_CJK_RUN_RE.findall(title):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[i:i + 2] for i in range(len(run) - 1))
    return tokens

def file_block_slug(path) -> str:
    """Slug stem of a ---FILE:--- block path, for generated_slugs membership.

    FILE-block-specific rule — deliberately SIMPLER than slugify(): the stem
    is already LLM-emitted kebab-case, so only lowercase + space/slash
    normalization is applied (no unicode folding, no punctuation stripping).
    Shared by _ingest_chunks and _stage_2_4_generation (was 6 verbatim copies).
    """
    return Path(path).stem.lower().replace(" ", "-").replace("/", "-")


# Explicitly re-export underscore-prefixed helpers. Without __all__, the
# `from _stage_2_base import *` used by every Stage 2.x module EXCLUDES
# _-prefixed names (Python default), so _retry_jitter / _is_retryable_exception
# / _record_rate_limit would NameError on retry paths.
__all__ = [n for n in dir() if not n.startswith("__")]
