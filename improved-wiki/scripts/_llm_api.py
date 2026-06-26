"""LLM API helpers — conversation-mode router for text generation.

Round iv (2026-06-22): text generation has exactly ONE path again —
conversation mode is no longer optional. ``call_anthropic_protocol`` (the
entry point the stage modules import) always delegates to the conversation
router registered by ingest.py: the prompt is written to a file and
``ConversationPending`` is raised so the calling agent answers with the
current conversation's model. Serial only — there is no concurrent text-gen
path.

this skill is only ever driven from a CLI session where an agent is already
present to answer conversation prompts, so a separate paid text-gen API key
added cost and complexity without a real use case. ``call_anthropic_direct``
is kept as a plain HTTP helper for callers that explicitly want it outside
the main ingest pipeline (e.g. ``cross_source_dedup.py``, a standalone tool)
— it is just no longer reachable from ``call_anthropic_protocol``.

Image captioning (Stage 1.3, MiniMax VLM) and minerU OCR are NOT text
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


# ── Direct HTTP text generation ──
#
# Makes a real HTTP call to the configured provider. Two protocols:
#   * "openai"    → POST {base_url}/chat/completions  (DeepSeek, OpenAI, …)
#   * "anthropic" → POST {base_url}/v1/messages        (Anthropic, MiniMax, …)
#
# Not used by the main ingest pipeline (conversation mode only). Kept as a
# plain HTTP helper for callers that explicitly want direct API access, e.g.
# `cross_source_dedup.py` (a standalone tool, not invoked from ingest.py).

_DIRECT_MAX_RETRIES = 3

# Per-read socket timeout for the streaming HTTP connection. With stream=True
# the server sends data as it generates, so the timeout applies to each SSE
# chunk read — an active (even slow) generation never times out because data
# keeps flowing, while a truly stalled connection still fails in ~60s. This
# fixes the root cause of the old non-streaming design where a >300s generation
# hit the socket timeout mid-generation. Override via LLM_HTTP_TIMEOUT env.
_HTTP_TIMEOUT_DEFAULT = int(os.environ.get("LLM_HTTP_TIMEOUT", "60"))


def _stream_post(url: str, headers: dict, body: dict,
                 parse_fn, timeout: int | None = None) -> tuple[str, str]:
    """POST with stream=True, parse SSE events via ``parse_fn(resp)``.

    Reads the response line-by-line so the socket timeout applies per-read:
    active generation keeps the connection alive (data flows), a stall times
    out fast. Returns ``(text, stop_reason)``.
    """
    if timeout is None:
        timeout = _HTTP_TIMEOUT_DEFAULT
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={**headers, "Content-Type": "application/json",
                 "Accept": "text/event-stream"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return parse_fn(resp)


def _parse_anthropic_stream(resp) -> tuple[str, str]:
    """Parse Anthropic-protocol SSE stream → (text, stop_reason)."""
    parts: list[str] = []
    stop = "end_turn"
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
        if not line.startswith("data:"):
            continue
        payload = line[5:].lstrip()
        if payload == "[DONE]" or not payload:
            continue
        try:
            evt = json.loads(payload)
        except json.JSONDecodeError:
            continue
        et = evt.get("type")
        if et == "content_block_delta":
            d = evt.get("delta") or {}
            if d.get("type") == "text_delta":
                parts.append(d.get("text", ""))
        elif et == "message_delta":
            d = evt.get("delta") or {}
            if d.get("stop_reason"):
                stop = d["stop_reason"]
        elif et == "error":
            raise RuntimeError(f"anthropic stream error: {evt.get('error', evt)}")
    return "".join(parts), stop


def _parse_openai_stream(resp) -> tuple[str, str]:
    """Parse OpenAI-protocol SSE stream → (text, finish_reason)."""
    parts: list[str] = []
    stop = "stop"
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
        if not line.startswith("data:"):
            continue
        payload = line[5:].lstrip()
        if payload == "[DONE]":
            break
        if not payload:
            continue
        try:
            evt = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if evt.get("error"):
            raise RuntimeError(f"openai stream error: {evt.get('error', evt)}")
        choices = evt.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            if delta.get("content"):
                parts.append(delta["content"])
            fr = choices[0].get("finish_reason")
            if fr:
                stop = fr
    return "".join(parts), stop


def call_anthropic_direct(prompt: str, config, max_tokens: int | None = None,
                          label: str = "") -> tuple[str, str]:
    """Direct HTTP text-generation call. Returns (text, stop_reason).

    Uses ``config.llm_protocol`` ("openai" or "anthropic"), ``config.llm_base_url``,
    ``config.llm_model``, ``config.llm_api_key``. Retries transient errors with
    jittered backoff (``_retry_jitter`` / ``_is_retryable_exception``).

    ``label`` is shown in the progress line (e.g. "chunk 1 generation") so the
    user can see what the call is for, not just "direct:anthropic".

    Raises ``RuntimeError`` if no API key is configured, or if all retries fail.
    """
    api_key = getattr(config, "llm_api_key", "") or ""
    if not api_key:
        raise RuntimeError(
            "Direct LLM call needs an API key. Set LLM_API_KEY or configure "
            "a provider in ~/.agents/config.json."
        )
    base_url = (getattr(config, "llm_base_url", "") or "").rstrip("/")
    model = getattr(config, "llm_model", "") or ""
    protocol = (getattr(config, "llm_protocol", "") or "anthropic").lower()
    if max_tokens is None:
        max_tokens = getattr(config, "max_tokens", 16384) or 16384

    if not base_url or not model:
        raise RuntimeError(
            "Direct LLM call needs llm_base_url and llm_model (check "
            "~/.agents/config.json or LLM_BASE_URL / LLM_MODEL env vars)."
        )

    if protocol == "openai":
        url = f"{base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}"}
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.3,
            "stream": True,
        }
        parse_fn = _parse_openai_stream
    else:
        url = f"{base_url}/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "stream": True,
        }
        parse_fn = _parse_anthropic_stream

    last_exc: Exception | None = None
    for attempt in range(_DIRECT_MAX_RETRIES):
        try:
            _progress(label or f"direct:{protocol}", attempt + 1, _DIRECT_MAX_RETRIES)
            text, stop = _stream_post(url, headers, body, parse_fn)
            if not text.strip():
                raise RuntimeError("empty response from LLM")
            return text, stop
        except Exception as exc:
            last_exc = exc
            if attempt < _DIRECT_MAX_RETRIES - 1 and _is_retryable_exception(exc):
                wait = _retry_jitter(2.0, attempt)
                print(f"  [direct:{protocol}] retry {attempt + 1}/{_DIRECT_MAX_RETRIES}"
                      f" ({type(exc).__name__}: {str(exc)[:80]}) — {wait:.1f}s",
                      flush=True)
                time.sleep(wait)
                continue
            raise
    # Unreachable: loop either returns or raises, but keep a defensive raise.
    raise RuntimeError(
        f"direct LLM call failed after {_DIRECT_MAX_RETRIES} attempts: {last_exc}"
    )


# ── Conversation-mode router ──
#
# ingest.py registers its `call_anthropic_protocol` (the function that writes
# a prompt file and raises ConversationPending) here at startup.
# `call_anthropic_protocol` below always delegates to it — there is no
# direct-API fallback for text generation anymore.

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
            print(f"[conv:{tag}] Read response ({len(response)} chars)", flush=True)
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
    "call_anthropic_direct",
    "call_anthropic_protocol",
    "conversation_handoff",
    "set_conversation_router",
    "set_progress_hook",
]
