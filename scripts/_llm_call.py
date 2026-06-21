"""_llm_call.py — conversation-mode LLM handoff for standalone sweep tools.

Sweep tools (``dedup_sweep.py``, ``wiki-lint-semantic.py``) call LLM steps in a
``(system, user) -> str`` shape. This module builds that callable on top of the
shared :func:`_llm_api.conversation_handoff` primitive — the same primitive
ingest.py's conversation router uses — so there is one cache-read / prompt-write
/ raise implementation across the skill.

History: this module used to resolve an LLM endpoint (env vars /
``~/.agents/config.json``) and make HTTP-direct calls for the dedup + semantic-
lint sweeps. As of round ii (2026-06-20) text generation is conversation-mode
only; round iv (2026-06-21) folds the handoff into ``_llm_api`` to eliminate the
duplicate cache/handoff logic that used to live here.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

from _llm_api import conversation_handoff

__all__ = [
    "make_conversation_llm_call",
    "slug_for",
]

# 16 hex chars (64 bits) — collision-resistant for the small number of
# LLM calls a single sweep makes (1 detect + N merges, or 1 lint pass).
_SLUG_LEN = 16


def slug_for(system: str, user: str) -> str:
    """Deterministic filesystem-safe slug for a (system, user) prompt pair.

    Content-addressed so the cache auto-invalidates when wiki content
    changes (the user message encodes the page summaries / merge inputs).
    """
    digest = hashlib.sha256(
        f"{system}\n\n{user}".encode("utf-8")
    ).hexdigest()
    return digest[:_SLUG_LEN]


def make_conversation_llm_call(
    runtime_dir: Path,
    stage_prefix: str,
) -> Callable[[str, str], str]:
    """Return a ``(system, user) -> str`` callable that does the conversation
    prompt-file handoff against ``<runtime>/conversation/<stage_prefix>/``.

    On cache hit: return ``<slug>.txt`` (left in place so the sweep *resumes*
    across multiple re-invokes — each re-invoke re-runs from the top and finds
    every prior result cached).

    On cache miss: write ``<slug>.md`` and raise ``ConversationPending``. The
    calling agent answers, writes ``<slug>.txt``, and re-invokes.
    """
    conv_dir = runtime_dir / "conversation" / stage_prefix

    def _llm_call(system: str, user: str) -> str:
        slug = slug_for(system, user)
        prompt_text = f"# System\n{system}\n\n# User\n{user}\n"
        return conversation_handoff(
            conv_dir, slug, prompt_text,
            label=f"{stage_prefix}/{slug}",
        )

    return _llm_call
