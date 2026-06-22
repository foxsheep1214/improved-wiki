"""Tests for the direct HTTP text-gen helper (round iv, 2026-06-22).

`call_anthropic_direct` is no longer reachable from `call_anthropic_protocol`
(text generation is conversation-mode only — see test_conversation_router.py),
but the function itself is kept as a plain HTTP helper for callers outside
the ingest pipeline (e.g. `cross_source_dedup.py`). This file verifies:
  * `call_anthropic_direct` parses both the OpenAI chat-completions and the
    Anthropic messages protocols via SSE streaming (mocked urllib — no network).
  * Raises clearly when no API key is configured.

Stdlib `unittest` only — no pytest, no network, no real LLM calls.
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _llm_api
from _core import Config, ConversationPending


def _make_config(*, api_key: str = "sk-test", protocol: str = "openai") -> Config:
    return Config(
        wiki_root=Path("/tmp/wiki"),
        raw_root=Path("/tmp/raw"),
        wiki_dir=Path("/tmp/wiki"),
        runtime_dir=Path("/tmp/rt"),
        cache_path=Path("/tmp/rt/ingest-cache.json"),
        progress_dir=Path("/tmp/rt/ingest-progress"),
        extract_tmp_dir=Path("/tmp/rt/extract-tmp"),
        llm_base_url="https://provider.example",
        llm_model="test-model",
        llm_api_key=api_key,
        llm_protocol=protocol,
        caption_api_key="",
        caption_base_url="https://provider.example",
        caption_model="test-caption",
        chunk_size=60000,
        chunk_overlap=3000,
        source_budget=100000,
        target_chars=60000,
        max_tokens=8192,
        conversation_prefix="ab12cd34",
    )


def _openai_sse(*, content: str = "", finish: str = "stop") -> bytes:
    """Build an OpenAI-protocol SSE payload (stream=True format)."""
    lines: list[str] = []
    if content:
        evt = {"choices": [{"delta": {"content": content}, "finish_reason": None}]}
        lines.append(f"data: {json.dumps(evt)}\n")
    evt = {"choices": [{"delta": {}, "finish_reason": finish}]}
    lines.append(f"data: {json.dumps(evt)}\n")
    lines.append("data: [DONE]\n")
    return "".join(lines).encode("utf-8")


def _anthropic_sse(*, text_parts: list[str], stop: str = "end_turn") -> bytes:
    """Build an Anthropic-protocol SSE payload (stream=True format)."""
    lines: list[str] = []
    for t in text_parts:
        evt = {"type": "content_block_delta",
               "delta": {"type": "text_delta", "text": t}}
        lines.append(f"data: {json.dumps(evt)}\n")
    evt = {"type": "message_delta", "delta": {"stop_reason": stop}}
    lines.append(f"data: {json.dumps(evt)}\n")
    return "".join(lines).encode("utf-8")


class _FakeResponse:
    """Minimal context manager mimicking urllib.request.urlopen's return.

    Iterable so the streaming parsers can do ``for raw in resp:``; each yielded
    item is a bytes line (matching a real HTTP response body iteration).
    """

    def __init__(self, payload: bytes):
        self._lines = payload.splitlines(keepends=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return b"".join(self._lines)

    def __iter__(self):
        return iter(self._lines)


class TestCallAnthropicDirect(unittest.TestCase):
    def test_openai_protocol_parses_choices(self):
        cfg = _make_config(protocol="openai")
        payload = _openai_sse(content="hello world", finish="stop")
        with mock.patch.object(_llm_api.urllib.request, "urlopen",
                               return_value=_FakeResponse(payload)):
            text, stop = _llm_api.call_anthropic_direct("hi", cfg, max_tokens=128)
        self.assertEqual(text, "hello world")
        self.assertEqual(stop, "stop")

    def test_anthropic_protocol_parses_content_blocks(self):
        cfg = _make_config(protocol="anthropic")
        payload = _anthropic_sse(text_parts=["part A", " part B"], stop="end_turn")
        with mock.patch.object(_llm_api.urllib.request, "urlopen",
                               return_value=_FakeResponse(payload)):
            text, stop = _llm_api.call_anthropic_direct("hi", cfg, max_tokens=128)
        self.assertEqual(text, "part A part B")
        self.assertEqual(stop, "end_turn")

    def test_openai_posts_to_chat_completions_endpoint(self):
        cfg = _make_config(protocol="openai")
        captured: dict = {}
        payload = _openai_sse(content="ok", finish="stop")

        def _fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResponse(payload)

        with mock.patch.object(_llm_api.urllib.request, "urlopen",
                               side_effect=_fake_urlopen):
            _llm_api.call_anthropic_direct("hi", cfg, max_tokens=256)
        self.assertEqual(captured["url"], "https://provider.example/chat/completions")
        self.assertEqual(captured["body"]["model"], "test-model")
        self.assertEqual(captured["body"]["messages"],
                         [{"role": "user", "content": "hi"}])
        # Streaming is now the default (fixes mid-generation socket timeouts).
        self.assertTrue(captured["body"].get("stream"))

    def test_anthropic_posts_to_messages_endpoint_with_api_key_header(self):
        cfg = _make_config(protocol="anthropic", api_key="sk-anthropic")
        captured: dict = {}
        payload = _anthropic_sse(text_parts=["ok"], stop="end_turn")

        def _fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            # Request.add_header capitalizes keys — normalize to lowercase.
            captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            return _FakeResponse(payload)

        with mock.patch.object(_llm_api.urllib.request, "urlopen",
                               side_effect=_fake_urlopen):
            _llm_api.call_anthropic_direct("hi", cfg, max_tokens=256)
        self.assertEqual(captured["url"], "https://provider.example/v1/messages")
        self.assertEqual(captured["headers"].get("x-api-key"), "sk-anthropic")

    def test_no_api_key_raises(self):
        cfg = _make_config(api_key="")
        with self.assertRaises(RuntimeError) as cm:
            _llm_api.call_anthropic_direct("hi", cfg)
        self.assertIn("API key", str(cm.exception))

    def test_empty_response_is_treated_as_failure(self):
        cfg = _make_config(protocol="openai")
        payload = _openai_sse(content="", finish="stop")
        # Fresh response per retry attempt (the iterator is single-use).
        with mock.patch.object(_llm_api.urllib.request, "urlopen",
                               side_effect=lambda *a, **k: _FakeResponse(payload)):
            with self.assertRaises(RuntimeError):
                _llm_api.call_anthropic_direct("hi", cfg, max_tokens=64)


class TestRouting(unittest.TestCase):
    def test_protocol_always_routes_to_conversation_router(self):
        # Text generation has exactly one path now — even with an API key
        # present, call_anthropic_protocol must hand off via the router and
        # never touch HTTP directly.
        cfg = _make_config(protocol="openai", api_key="sk-x")
        called = {"router": False}

        def _router(prompt, config, max_tokens):
            called["router"] = True
            return "router-answer", "end_turn"

        with mock.patch.object(_llm_api, "_conversation_router", _router), \
             mock.patch.object(_llm_api.urllib.request, "urlopen") as urlopen:
            text, stop = _llm_api.call_anthropic_protocol("hi", cfg)
        self.assertTrue(called["router"])
        self.assertEqual(text, "router-answer")
        urlopen.assert_not_called()

    def test_protocol_raises_when_no_router_registered(self):
        cfg = _make_config(protocol="openai", api_key="sk-x")
        with mock.patch.object(_llm_api, "_conversation_router", None):
            with self.assertRaises(RuntimeError) as cm:
                _llm_api.call_anthropic_protocol("hi", cfg)
        self.assertIn("router", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
