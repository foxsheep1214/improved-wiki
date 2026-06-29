"""Phase 2 common imports — shared by all Stage 2.x modules.

Reduces 15-line import blocks duplicated across 6 files to a single import.
Created 2026-06-21 as part of Phase 2 consolidation.
"""
from __future__ import annotations

import json, os, re, sys, time
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from _core import (
    Config,
    heartbeat as _heartbeat,
    stage_begin as _stage_begin,
    stage_end as _stage_end,
    llm_call_progress as _llm_call_progress,
    llm_call_done as _llm_call_done,
    record_rate_limit as _record_rate_limit,
    load_template,
    load_progress,
    save_progress,
    clear_progress,
    progress_path,
    load_cache,
    save_cache,
    list_existing_slugs,
    load_schema_md,
    schema_folders,
    BASE_PAGE_DIRS,
    str_distance as _str_distance,
    FOLDER_TO_TEMPLATE,
    detect_template_type,
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
from _paths import media_slug as _stage_1_2_media_slug

# Folders that may appear in schema.md but are not LLM-generated page types.
# Shared by Stage 2.2 (analysis) and 2.4 (generation) schema routing
# (NashSU 0.5.3 schema-typed-candidates parity).
SCHEMA_NON_PAGE_DIRS = frozenset({"media", "raw", "page-history", "chats"})


def _stage_2_frontmatter_title(content: str) -> str:
    """Extract a page's `title:` frontmatter value, quotes stripped.

    Shared by Stage 2.3 / 2.8's title-Jaccard matching — frontmatter titles
    are written as `title: "Foo Bar"`, and feeding the raw quoted string into
    a word-split silently weakens every match by one token on each end (the
    first/last word carries a stray `"`).
    """
    m = re.search(r"title:\s*([^\n]+)", content)
    if not m:
        return ""
    return m.group(1).strip().strip("\"'")


def _stage_2_title_words(title: str) -> set:
    """Word-level token set for title-overlap Jaccard matching (case-insensitive, len>1)."""
    return set(w.lower() for w in re.split(r"[\s/]+", title) if len(w) > 1)

# Explicitly re-export underscore-prefixed helpers. Without __all__, the
# `from _stage_2_base import *` used by every Stage 2.x module EXCLUDES
# _-prefixed names (Python default), so _retry_jitter / _is_retryable_exception
# / _record_rate_limit / _stage_1_2_media_slug would NameError on retry paths.
__all__ = [n for n in dir() if not n.startswith("__")]
