"""LLM API call helpers — extracted from ingest.py (2026-06-18).

Provides retry logic and API call functions for Anthropic-protocol (MiniMax)
and OpenAI-protocol (DeepSeek, OpenAI) endpoints.

All transient errors (5xx, 429, network, timeout, truncated responses) are
retried with exponential backoff + jitter.  Non-retryable errors (4xx except
429, permanent failures) raise immediately.
"""
from __future__ import annotations

import json
import time
import urllib.request
import urllib.error


# ── Optional progress hook (set by ingest.py after import) ──

_progress_hook = None  # callable(label, attempt, retries) | None


def set_progress_hook(hook) -> None:
    """Set a callback for LLM call progress (used by ingest.py for [file] tags)."""
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

    Covers MiniMax CN cluster's observed error spectrum:
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


# ── Public API ──

__all__ = [
    "_retry_jitter",
    "_is_retryable_http_error",
    "_is_retryable_exception",
    "call_anthropic_api",
    "call_openai_api",
    "set_progress_hook",
]


def call_anthropic_api(api_key: str, base_url: str, model: str,
                       prompt: str, max_tokens: int = 4096,
                       timeout: int = 600) -> tuple[str, str]:
    """Call an Anthropic-protocol API (MiniMax or compatible).

    Uses Anthropic Messages API format:
      POST {base_url}/anthropic/v1/messages
      Auth: x-api-key

    Retry: up to 5 attempts with exponential backoff + jitter on all
    transient errors (5xx, 429, network, timeout, truncated responses).

    Returns (text_content, stop_reason).
    """
    url = f"{base_url.rstrip('/')}/anthropic/v1/messages"
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    max_retries = 5
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            t0 = time.time()
            _progress("LLM (Anthropic)", attempt + 1, max_retries)
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            content = data.get("content", [])
            if not content:
                raise RuntimeError("LLM response has no content (transient)")
            text_parts = [block.get("text", "") for block in content if block.get("type") == "text"]
            stop_reason = data.get("stop_reason", "unknown")
            result = "".join(text_parts)
            elapsed = time.time() - t0
            print(f"OK ({elapsed:.0f}s, {len(result):,} chars)", flush=True)
            return result, stop_reason
        except Exception as e:
            last_error = e
            if attempt < max_retries and _is_retryable_exception(e):
                wait = _retry_jitter(2.0, attempt)
                err_label = type(e).__name__
                if isinstance(e, urllib.error.HTTPError):
                    err_label = f"HTTP {e.code}"
                print(f"[llm] {err_label} on attempt {attempt + 1}/{max_retries + 1} "
                      f"— retrying in {wait:.1f}s...", flush=True)
                time.sleep(wait)
                continue
            if isinstance(e, urllib.error.HTTPError):
                err_body = e.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"LLM API HTTP {e.code}: {err_body[-500:]}")
            raise
    raise RuntimeError(f"LLM API call failed after {max_retries + 1} attempts: {last_error}")


def call_openai_api(api_key: str, base_url: str, model: str,
                    prompt: str, max_tokens: int = 4096,
                    timeout: int = 600) -> tuple[str, str]:
    """Call an OpenAI-compatible API (DeepSeek, OpenAI, etc.).

    Uses OpenAI Chat Completions format:
      POST {base_url}/v1/chat/completions
      Auth: Authorization: Bearer <key>

    Retry: up to 5 attempts with exponential backoff + jitter on all
    transient errors (5xx, 429, network, timeout, truncated responses).

    Returns (text_content, stop_reason).
    """
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    max_retries = 5
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            t0 = time.time()
            _progress("LLM (OpenAI)", attempt + 1, max_retries)
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            choices = data.get("choices", [])
            if not choices:
                raise RuntimeError("LLM response has no choices (transient)")
            text = choices[0].get("message", {}).get("content", "")
            stop_reason = choices[0].get("finish_reason", "unknown")
            elapsed = time.time() - t0
            print(f"OK ({elapsed:.0f}s, {len(text):,} chars)", flush=True)
            usage = data.get("usage", {})
            if usage:
                print(f"[llm] tokens: {usage.get('prompt_tokens', '?')} in / "
                      f"{usage.get('completion_tokens', '?')} out", flush=True)
            return text.strip(), stop_reason
        except Exception as e:
            last_error = e
            if attempt < max_retries and _is_retryable_exception(e):
                wait = _retry_jitter(2.0, attempt)
                err_label = type(e).__name__
                if isinstance(e, urllib.error.HTTPError):
                    err_label = f"HTTP {e.code}"
                print(f"[llm] {err_label} on attempt {attempt + 1}/{max_retries + 1} "
                      f"— retrying in {wait:.1f}s...", flush=True)
                time.sleep(wait)
                continue
            if isinstance(e, urllib.error.HTTPError):
                err_body = e.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"LLM API HTTP {e.code}: {err_body[-500:]}")
            raise
    raise RuntimeError(f"LLM API call failed after {max_retries + 1} attempts: {last_error}")


# ── Config-aware router (used by ingest.py stage functions) ──

def call_anthropic_protocol(prompt: str, config, max_tokens: int | None = None
                            ) -> tuple[str, str]:
    """Route LLM call to the correct protocol based on config.llm_protocol."""
    mt = max_tokens or config.max_tokens
    if getattr(config, 'conversation_mode', False):
        # Conversation mode — handled by ingest.py, not here
        raise RuntimeError("Conversation mode requires ingest.py context")
    if getattr(config, 'llm_protocol', 'anthropic') == "openai":
        return call_openai_api(
            api_key=config.llm_api_key,
            base_url=config.llm_base_url,
            model=config.llm_model,
            prompt=prompt,
            max_tokens=mt,
        )
    return call_anthropic_api(
        api_key=config.llm_api_key,
        base_url=config.llm_base_url,
        model=config.llm_model,
        prompt=prompt,
        max_tokens=mt,
    )


__all__ = [
    "_retry_jitter",
    "_is_retryable_http_error",
    "_is_retryable_exception",
    "call_anthropic_api",
    "call_openai_api",
    "call_anthropic_protocol",
    "set_progress_hook",
]
