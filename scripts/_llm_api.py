"""LLM API helpers — direct HTTP text-gen + conversation-mode router.

Routing (round iii, 2026-06-21): text generation has TWO paths again.

  * **Direct API** (default): ``call_anthropic_direct(prompt, config, ...)``
    makes a real HTTP call to the configured provider (OpenAI or Anthropic
    protocol) using ``config.llm_api_key``. This is the fast path — no
    process-exit handoff — and the only path that can run concurrently
    (ThreadPoolExecutor parallel chunk analysis).
  * **Conversation mode** (opt-in via ``--conversation``): the prompt is
    written to a file and ``ConversationPending`` is raised so the calling
    agent answers with the current conversation's model. Serial only.

``call_anthropic_protocol`` (the entry point the stage modules import) picks
the path: conversation router when ``config.conversation_mode`` is set,
otherwise direct API (requires ``config.llm_api_key``; raises if absent).

Round ii (2026-06-20) had removed HTTP-direct text gen entirely (conversation
only). Round iii re-adds it because (a) enrichment is high-volume / low-value
per call and the conversation handoff made it ~8× slower, and (b) parallel
chunk analysis is impossible when every call exits the process.

Image captioning (Stage 1.3, MiniMax VLM) and minerU OCR are NOT text
generation and live elsewhere (`_stage_1_extract.py`); they are unaffected.
"""
from __future__ import annotations

import json
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


# ── Direct HTTP text generation (round iii, 2026-06-21) ──
#
# Makes a real HTTP call to the configured provider. Two protocols:
#   * "openai"    → POST {base_url}/chat/completions  (DeepSeek, OpenAI, …)
#   * "anthropic" → POST {base_url}/v1/messages        (Anthropic, MiniMax, …)
#
# This is the path that lets the pipeline run concurrently (parallel chunk
# analysis via ThreadPoolExecutor) and the path enrichment uses unconditionally
# (high-volume / low-value — the conversation handoff made it ~8× slower).

_DIRECT_MAX_RETRIES = 3


def _direct_http_post(url: str, headers: dict, body: dict, timeout: int = 600
                      ) -> dict:
    """POST JSON to ``url``, return parsed JSON. Raises on HTTP/transport error."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={**headers, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def call_anthropic_direct(prompt: str, config, max_tokens: int | None = None
                          ) -> tuple[str, str]:
    """Direct HTTP text-generation call. Returns (text, stop_reason).

    Uses ``config.llm_protocol`` ("openai" or "anthropic"), ``config.llm_base_url``,
    ``config.llm_model``, ``config.llm_api_key``. Retries transient errors with
    jittered backoff (``_retry_jitter`` / ``_is_retryable_exception``).

    Raises ``RuntimeError`` if no API key is configured, or if all retries fail.
    """
    api_key = getattr(config, "llm_api_key", "") or ""
    if not api_key:
        raise RuntimeError(
            "Direct LLM call needs an API key. Set LLM_API_KEY (or a provider "
            "in ~/.agents/config.json), or run with --conversation to delegate "
            "text generation to the calling agent."
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
            "stream": False,
        }

        def _parse(resp: dict) -> tuple[str, str]:
            choices = resp.get("choices") or []
            if not choices:
                raise RuntimeError(f"openai response had no choices: {resp}")
            msg = choices[0].get("message", {})
            text = msg.get("content", "") or ""
            stop = choices[0].get("finish_reason", "stop")
            return text, stop
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
        }

        def _parse(resp: dict) -> tuple[str, str]:
            content = resp.get("content") or []
            text = "".join(
                c.get("text", "") for c in content if c.get("type") == "text"
            )
            stop = resp.get("stop_reason", "end_turn")
            return text, stop

    last_exc: Exception | None = None
    for attempt in range(_DIRECT_MAX_RETRIES):
        try:
            _progress(f"direct:{protocol}", attempt + 1, _DIRECT_MAX_RETRIES)
            resp = _direct_http_post(url, headers, body)
            text, stop = _parse(resp)
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
# a prompt file and raises ConversationPending) here at startup. When
# `config.conversation_mode` is set, text-gen calls delegate to it. Otherwise
# they go through `call_anthropic_direct` (the default fast path).

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


def call_anthropic_protocol(prompt: str, config, max_tokens: int | None = None
                            ) -> tuple[str, str]:
    """Route a text-generation LLM call.

    Path selection (round iii):
      * ``config.conversation_mode`` → delegate to the conversation router
        registered by ingest.py (prompt-file handoff, raises
        ``ConversationPending``). Serial only.
      * otherwise → ``call_anthropic_direct`` (real HTTP call to the
        configured provider). This is the default and the only parallelizable
        path.

    Returns (text_content, stop_reason).
    """
    if getattr(config, 'conversation_mode', False):
        if _conversation_router is None:
            raise RuntimeError(
                "Conversation mode is active but no router is registered "
                "(ingest.py must call set_conversation_router at startup)."
            )
        return _conversation_router(prompt, config, max_tokens)
    return call_anthropic_direct(prompt, config, max_tokens)


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
