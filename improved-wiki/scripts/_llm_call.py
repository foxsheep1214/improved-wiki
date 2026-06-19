"""_llm_call.py — shared LLM config resolution + (system, user) -> str callable.

Bridges the `_dedup` / `_lint_suggest` library contract — which inject a plain
``(system_prompt, user_message) -> str`` callable — to the real LLM endpoints,
so standalone sweep scripts don't each re-implement env/config.json resolution,
protocol routing, and retry.

Config resolution order (mirrors wiki-lint-semantic.py + ingest.py):
  1. Env vars:  LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_PROTOCOL
  2. ~/.agents/config.json:  default provider → {protocol, base_url, api_key,
     models.text | model}

Both Anthropic-protocol (system as top-level field) and OpenAI-protocol
(system as a message role) are supported. Retry reuses _llm_api primitives.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from _llm_api import _is_retryable_exception, _retry_jitter

__all__ = [
    "LLMConfig",
    "resolve_llm_config",
    "make_llm_callable",
    "llm_call",
]

DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT = 600
MAX_RETRIES = 5


@dataclass
class LLMConfig:
    api_key: str
    base_url: str
    model: str
    protocol: str  # "anthropic" | "openai"


def resolve_llm_config() -> LLMConfig | None:
    """Resolve LLM config from env vars, falling back to ~/.agents/config.json.

    Returns None if no api_key can be found (caller decides how to surface).
    """
    api_key = os.environ.get("LLM_API_KEY", "")
    base_url = os.environ.get("LLM_BASE_URL", "")
    model = os.environ.get("LLM_MODEL", "")
    protocol = os.environ.get("LLM_PROTOCOL", "")

    if not (api_key and base_url and model):
        config_path = Path.home() / ".agents" / "config.json"
        try:
            if config_path.exists():
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
                default = os.environ.get("LLM_PROVIDER") or cfg.get("default", "")
                provider = cfg.get("providers", {}).get(default, {})
                if provider:
                    api_key = api_key or provider.get("api_key", "")
                    base_url = base_url or provider.get("base_url", "")
                    model = model or provider.get("models", {}).get(
                        "text", provider.get("model", "")
                    )
                    protocol = protocol or provider.get("protocol", "anthropic")
        except (OSError, ValueError):
            pass

    if not (api_key and base_url and model):
        return None
    if protocol not in ("anthropic", "openai"):
        protocol = "anthropic"
    return LLMConfig(
        api_key=api_key, base_url=base_url, model=model, protocol=protocol
    )


def llm_call(system: str, user: str, *, config: LLMConfig,
             max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
    """Call the LLM with a system + user message. Returns the text content.

    Retries transient errors (5xx, 429, network, timeout, truncated) with
    exponential backoff + jitter, reusing _llm_api's retry classification.
    """
    if config.protocol == "openai":
        url = f"{config.base_url.rstrip('/')}/v1/chat/completions"
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        body = json.dumps({
            "model": config.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.api_key}",
        }
        return _call_with_retry(url, headers, body, _parse_openai)

    # Anthropic protocol
    url = f"{config.base_url.rstrip('/')}/anthropic/v1/messages"
    body = json.dumps({
        "model": config.model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "x-api-key": config.api_key,
        "anthropic-version": "2023-06-01",
    }
    return _call_with_retry(url, headers, body, _parse_anthropic)


def make_llm_callable(
    config: LLMConfig | None = None,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> Callable[[str, str], str]:
    """Return a ``(system, user) -> str`` closure bound to a resolved config.

    Raises RuntimeError if no config can be resolved. This is the shape
    `_dedup.detect_duplicate_groups` / `merge_duplicate_group` expect.
    """
    cfg = config or resolve_llm_config()
    if cfg is None:
        raise RuntimeError(
            "No LLM config found. Set LLM_API_KEY/LLM_BASE_URL/LLM_MODEL env "
            "vars or configure ~/.agents/config.json."
        )

    def _callable(system: str, user: str) -> str:
        return llm_call(system, user, config=cfg, max_tokens=max_tokens)

    return _callable


# ── HTTP + retry internals ──────────────────────────────────────────────────

def _call_with_retry(url, headers, body, parse_response: Callable[[dict], str]) -> str:
    """POST ``body`` to ``url`` with retry; parse via ``parse_response``."""
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            data = _http_json_post(url, headers, body, timeout=DEFAULT_TIMEOUT)
            return parse_response(data)
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES and _is_retryable_exception(e):
                wait = _retry_jitter(2.0, attempt)
                label = (f"HTTP {e.code}"
                         if isinstance(e, urllib.error.HTTPError)
                         else type(e).__name__)
                print(f"[llm] {label} on attempt {attempt + 1}/{MAX_RETRIES + 1} "
                      f"— retrying in {wait:.1f}s...", flush=True)
                _sleep(wait)
                continue
            raise
    raise RuntimeError(f"LLM call failed after {MAX_RETRIES + 1} attempts: {last_error}")


def _sleep(seconds: float) -> None:
    import time
    time.sleep(seconds)


def _http_json_post(url: str, headers: dict, body: bytes, *, timeout: int) -> dict:
    """Single HTTP POST returning parsed JSON. Monkeypatched in tests."""
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM API HTTP {e.code}: {err_body[-500:]}") from None


def _parse_anthropic(data: dict) -> str:
    content = data.get("content", [])
    if not content:
        raise RuntimeError("LLM response has no content (transient)")
    return "".join(b.get("text", "") for b in content if b.get("type") == "text")


def _parse_openai(data: dict) -> str:
    choices = data.get("choices", [])
    if not choices:
        raise RuntimeError("LLM response has no choices (transient)")
    return choices[0].get("message", {}).get("content", "").strip()
