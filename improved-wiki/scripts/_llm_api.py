"""LLM API helpers — conversation-mode router for text generation.

Round iv (2026-06-22): text generation has exactly ONE path again —
conversation mode is no longer optional. ``call_anthropic_protocol`` (the
entry point the stage modules import) always delegates to the conversation
router registered by ingest.py: the prompt is written to a file and
``ConversationPending`` is raised so the calling agent answers with the
current conversation's model. Serial only — there is no concurrent text-gen
path.

Image captioning (Stage 1.3) and minerU OCR are NOT text
generation and live elsewhere (`_stage_1_extract.py`); they are unaffected —
vision content can't flow through the conversation-file handoff, so they
always call their configured HTTP API directly regardless of this module.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from _core import ConversationPending


# ── Shared retry helpers (NashSU session-lesson §19 BatchError pattern) ──

def _retry_jitter(base_wait: float, attempt: int) -> float:
    """Exponential backoff with ±30% jitter to avoid thundering herd."""
    raw = base_wait * (2 ** attempt)
    seed = (int(time.time() * 1000) + attempt * 7919) % 100
    jitter = 0.7 + 0.6 * (seed / 100.0)
    return raw * jitter


def _is_retryable_http_error(code: int | None) -> bool:
    """All 5xx + 429 are transient and worth retrying."""
    return code is not None and (500 <= code < 600 or code == 429)


def _is_retryable_exception(exc: Exception) -> bool:
    """Check whether an exception is plausibly transient.

    Covers common provider error spectrum:
    HTTP 500/502/503/520/529, 429, ReadTimeout, ConnectError,
    ChunkedEncodingError, JSONDecodeError (truncated response),
    IncompleteRead (truncated chunked response), RemoteDisconnected,
    RuntimeError from empty/malformed LLM responses.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return _is_retryable_http_error(exc.code)
    if isinstance(exc, (OSError, TimeoutError)):
        return True
    if isinstance(exc, json.JSONDecodeError):
        return True
    # Catch http.client.IncompleteRead and other HTTP-level transient errors
    exc_name = type(exc).__name__
    if exc_name in ("IncompleteRead", "RemoteDisconnected", "ChunkedEncodingError"):
        return True
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        transient_markers = (
            "no content", "empty response", "no choices",
            "overload", "rate", "timeout", "connection",
            "reset by peer", "broken pipe", "service unavailable",
            "try again", "capacity", "internal error",
        )
        return any(m in msg for m in transient_markers)
    return False


_conversation_router = None  # (prompt, config, max_tokens) -> (text, stop_reason)


def set_conversation_router(fn) -> None:
    """Register ingest.py's conversation-mode LLM call router."""
    global _conversation_router
    _conversation_router = fn


def conversation_handoff(
    conv_dir: Path,
    slug: str,
    prompt_text: str,
    *,
    label: str | None = None,
    stale_check: Callable[[str, str], bool] | None = None,
    on_cached: Callable[[str], None] | None = None,
    on_prompt_written: Callable[[], None] | None = None,
) -> str:
    """Shared conversation-mode prompt-file handoff (cache read → write → raise).

    The single primitive both ingest.py's conversation router and the standalone
    sweep tools (``_llm_call.make_conversation_llm_call``) build on, so there is
    one cache-read / prompt-write / raise implementation across the skill.

    Cache hit (``<conv_dir>/<slug>.txt`` exists and passes ``stale_check``):
    return the cached response (calls ``on_cached(response)`` if given).

    Cache miss: write ``<slug>.md`` (calls ``on_prompt_written()`` if given),
    print the CONVERSATION banner, and raise ``ConversationPending``.

    Args:
        conv_dir: directory for prompt/result files (created if missing).
        slug: filesystem-safe cache key (caller-chosen strategy).
        prompt_text: exact text written to ``<slug>.md``.
        label: banner / log tag (defaults to slug).
        stale_check: optional ``(response, prompt_text) -> bool``; True deletes
            the stale result and re-prompts.
        on_cached: optional callback invoked with the cached response on hit
            (e.g. to mark a task done in the manifest).
        on_prompt_written: optional callback invoked after the prompt file is
            written (e.g. to record a pending task).

    Raises ConversationPending on cache miss.
    """
    conv_dir.mkdir(parents=True, exist_ok=True)
    result_file = conv_dir / f"{slug}.txt"
    prompt_file = conv_dir / f"{slug}.md"
    tag = label or slug

    if result_file.exists():
        response = result_file.read_text(encoding="utf-8")
        if stale_check is not None and stale_check(response, prompt_text):
            print(f"[conv:{tag}] Result appears stale — regenerating", flush=True)
            result_file.unlink(missing_ok=True)
        else:
            # Handoff latency = answer mtime − prompt mtime. The ingest process
            # exits during a handoff, so this is the only place wall-clock per
            # LLM step is visible (timing instrumentation, 2026-07-02).
            try:
                _lat = result_file.stat().st_mtime - prompt_file.stat().st_mtime
                _lat_s = f", handoff {_lat/60:.1f}m" if _lat > 0 else ""
            except OSError:
                _lat_s = ""
            print(f"[conv:{tag}] Read response ({len(response)} chars{_lat_s})", flush=True)
            if on_cached is not None:
                on_cached(response)
            return response

    prompt_file.write_text(prompt_text, encoding="utf-8")
    if on_prompt_written is not None:
        on_prompt_written()
    print(f"\n{'=' * 60}", flush=True)
    print(f"  CONVERSATION → {tag}", flush=True)
    print(f"  Prompt:  {prompt_file}", flush=True)
    print(f"  Result:  {result_file}", flush=True)
    print(f"{'=' * 60}\n", flush=True)
    raise ConversationPending()


def call_anthropic_protocol(prompt: str, config, max_tokens: int | None = None,
                            label: str = "") -> tuple[str, str]:
    """Route a text-generation LLM call to the conversation router.

    Always delegates to the conversation router registered by ingest.py
    (prompt-file handoff, raises ``ConversationPending``). Serial only —
    there is no direct-API fallback for text generation.

    ``label`` is accepted for call-site compatibility (progress lines that
    pass it) but is not otherwise used.

    Returns (text_content, stop_reason).
    """
    if _conversation_router is None:
        raise RuntimeError(
            "No conversation router is registered (ingest.py must call "
            "set_conversation_router at startup)."
        )
    return _conversation_router(prompt, config, max_tokens)


__all__ = [
    "_retry_jitter",
    "_is_retryable_http_error",
    "_is_retryable_exception",
    "call_anthropic_protocol",
    "conversation_handoff",
    "set_conversation_router",
]
