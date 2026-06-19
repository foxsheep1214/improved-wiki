"""_llm_call.py — conversation-mode LLM handoff for standalone sweep tools.

History: this module used to resolve an LLM endpoint (env vars /
``~/.agents/config.json``) and make HTTP-direct calls (Anthropic / OpenAI
protocol) for the dedup + semantic-lint sweeps. As of round ii (2026-06-20)
text generation is **conversation mode only**: there is no http-direct path.
The calling agent answers each LLM step with the current conversation's model.

This module now provides the prompt-file handoff that sweep tools
(`dedup_sweep.py`, `wiki-lint-semantic.py`) use in conversation mode:

  1. The sweep calls ``llm_call(system, user)`` (the callable returned by
     ``make_conversation_llm_call``).
  2. If a cached result file exists for this (system, user) it is returned
     immediately — this is how a sweep *resumes* after a ConversationPending
     exit. The cache key is a content hash, so it auto-invalidates when the
     wiki content changes.
  3. Otherwise the prompt is written to
     ``<runtime>/conversation/<stage_prefix>/<slug>.md`` and
     ``ConversationPending`` is raised. The calling agent reads the prompt,
     answers with the current model, writes ``<slug>.txt``, and re-invokes
     the sweep — which hits step 2 and continues.

The callable is the ``(system, user) -> str`` shape that ``_dedup`` expects,
so ``_dedup.detect_duplicate_groups`` / ``merge_duplicate_group`` work
unchanged: each LLM call either returns from cache or raises
ConversationPending (the sweep exits 101, the agent answers, the next
re-invoke resumes from the top with all prior calls cached).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

# Reuse the shared ConversationPending signal (defined in _core, also used by
# ingest.py). Importing _core here keeps one canonical exception type across
# the whole skill.
from _core import ConversationPending

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

    On cache hit: read and return ``<slug>.txt`` (left in place so the sweep
    can resume across multiple re-invokes — each re-invoke re-runs from the
    top and finds every prior result cached).

    On cache miss: write ``<slug>.md`` and raise ``ConversationPending``. The
    calling agent answers, writes ``<slug>.txt``, and re-invokes.
    """
    conv_dir = runtime_dir / "conversation" / stage_prefix

    def _llm_call(system: str, user: str) -> str:
        slug = slug_for(system, user)
        conv_dir.mkdir(parents=True, exist_ok=True)
        result_file = conv_dir / f"{slug}.txt"
        prompt_file = conv_dir / f"{slug}.md"

        if result_file.exists():
            response = result_file.read_text(encoding="utf-8")
            print(f"[conv:{stage_prefix}/{slug}] Read cached response "
                  f"({len(response)} chars)", flush=True)
            return response

        prompt_file.write_text(
            f"# System\n{system}\n\n# User\n{user}\n",
            encoding="utf-8",
        )
        print(f"\n{'=' * 60}", flush=True)
        print(f"  CONVERSATION → {stage_prefix}/{slug}", flush=True)
        print(f"  Prompt:  {prompt_file}", flush=True)
        print(f"  Result:  {result_file}", flush=True)
        print(f"{'=' * 60}\n", flush=True)
        raise ConversationPending()

    return _llm_call
