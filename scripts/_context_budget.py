"""Explicit context budget for conversation-mode workers; no model self-report."""
from __future__ import annotations

import os
from _config import _CONTEXT_SIZE_DEFAULT

CONTEXT_ENV = "IMPROVED_WIKI_CONTEXT_TOKENS"


def context_tokens(value=None) -> int:
    raw = value if value is not None else os.environ.get(CONTEXT_ENV, "")
    if raw in (None, ""):
        return _CONTEXT_SIZE_DEFAULT
    if not isinstance(raw, (str, int)) or isinstance(raw, bool):
        raise ValueError(f"{CONTEXT_ENV}/--context-tokens must be an integer")
    try:
        tokens = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{CONTEXT_ENV}/--context-tokens must be an integer") from exc
    if not 64_000 <= tokens <= 10_000_000:
        raise ValueError("context tokens must be between 64000 and 10000000; "
                         "the prompt reserves require at least 64000")
    return tokens


def apply_context_budget(config) -> None:
    tokens = context_tokens()
    source = "explicit runtime setting" if os.environ.get(CONTEXT_ENV) else "conservative default"
    print(f"[context] {tokens:,} tokens ({source}); no model self-report or cached probe")
    config.apply_context(tokens)
