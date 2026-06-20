"""LLM API helpers — retry classification + conversation-mode router.

History: this module used to host the HTTP-direct text-generation callers
(`call_anthropic_api` / `call_openai_api`) and a protocol router. As of
2026-06-20 (round ii) text generation is **conversation mode only** — the
calling agent spawns sub-agents using the current conversation's model, and
no external LLM API key is needed for text gen. The HTTP-direct text-gen
path has been removed.

What remains here:
  * Retry classification helpers (`_retry_jitter`, `_is_retryable_exception`,
    `_is_retryable_http_error`) — still imported by the stage modules and by
    `_llm_call.py` (which itself is being retired for text gen).
  * A conversation router hook: `ingest.py` registers its
    `call_anthropic_protocol` (which performs the prompt-file handoff) via
    `set_conversation_router`. The stage modules call
    `_llm_api.call_anthropic_protocol(prompt, config, max_tokens)`; when
    `config.conversation_mode` is set the call is delegated to the registered
    router, otherwise it raises (http-direct is gone).

Image captioning (Stage 1.3, MiniMax VLM) and minerU OCR are NOT text
generation and live elsewhere (`_stage_1_extract.py`); they are unaffected.
"""
from __future__ import annotations

import json
import time
import urllib.error


# ── Optional progress hook (set by ingest.py after import) ──

_progress_hook = None  # callable(label, attempt, retries) | None


def set_progress_hook(hook) -> None:
    """Set a callback for LLM call progress (kept for backward compat)."""
    global _progress_hook
    _progress_hook = hook


def _progress(label: str, attempt: int = 1, retries: int = 0) -> None:
    if _progress_hook:
        _progress_hook(label, attempt, retries)


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


# ── Conversation-mode router ──
#
# ingest.py registers its `call_anthropic_protocol` (the function that writes
# a prompt file and raises ConversationPending) here at startup. The stage
# modules (`_stage_2_analyze`, `_stage_2_generate`, `_stage_3_write`,
# `_enrich_wikilinks`) import `call_anthropic_protocol` from this module, so
# registering the router once makes every stage text-gen call route through
# conversation mode automatically — no per-module monkeypatching needed.

_conversation_router = None  # (prompt, config, max_tokens) -> (text, stop_reason)


def set_conversation_router(fn) -> None:
    """Register ingest.py's conversation-mode LLM call router."""
    global _conversation_router
    _conversation_router = fn


def call_anthropic_protocol(prompt: str, config, max_tokens: int | None = None
                            ) -> tuple[str, str]:
    """Route a text-generation LLM call.

    Conversation mode (the only text-gen path as of round ii): delegate to
    the router registered by ingest.py, which writes a prompt file and raises
    ``ConversationPending`` so the calling agent can answer with the current
    conversation's model.

    HTTP-direct text generation has been removed. If invoked without
    conversation mode, raise a clear error pointing at ``--conversation``.
    """
    if getattr(config, 'conversation_mode', False):
        if _conversation_router is None:
            raise RuntimeError(
                "Conversation mode is active but no router is registered "
                "(ingest.py must call set_conversation_router at startup)."
            )
        return _conversation_router(prompt, config, max_tokens)
    raise RuntimeError(
        "Text generation requires --conversation mode. HTTP-direct LLM calls "
        "have been removed (round ii, 2026-06-20); run ingest.py with "
        "--conversation so the calling agent handles each LLM step with the "
        "current conversation's model. (Image captioning still calls MiniMax "
        "VLM separately; OCR runs local minerU.)"
    )


__all__ = [
    "_retry_jitter",
    "_is_retryable_http_error",
    "_is_retryable_exception",
    "call_anthropic_protocol",
    "set_conversation_router",
    "set_progress_hook",
]
