"""Tests for the direct HTTP text-gen path (round iii, 2026-06-21).

Verifies:
  * `call_anthropic_direct` parses both the OpenAI chat-completions and the
    Anthropic messages protocols (mocked urllib — no network).
  * Routing in `call_anthropic_protocol`: direct API when not in conversation
    mode, conversation router when ``--conversation`` is set.
  * Raises clearly when no API key is configured (the message must point at
    both LLM_API_KEY and --conversation so the existing regression test that
    asserts ``--conversation`` in the error still holds).

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


def _make_config(*, conversation: bool = False, api_key: str = "sk-test",
                 protocol: str = "openai") -> Config:
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
        conversation_mode=conversation,
        conversation_prefix="ab12cd34",
    )


class _FakeResponse:
    """Minimal context manager mimicking urllib.request.urlopen's return."""

    def __init__(self, payload: bytes):
        self._buf = io.BytesIO(payload)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._buf.read()


class TestCallAnthropicDirect(unittest.TestCase):
    def test_openai_protocol_parses_choices(self):
        cfg = _make_config(protocol="openai")
        payload = json.dumps({
            "choices": [{
                "message": {"role": "assistant", "content": "hello world"},
                "finish_reason": "stop",
            }],
        }).encode("utf-8")
        with mock.patch.object(_llm_api.urllib.request, "urlopen",
                               return_value=_FakeResponse(payload)):
            text, stop = _llm_api.call_anthropic_direct("hi", cfg, max_tokens=128)
        self.assertEqual(text, "hello world")
        self.assertEqual(stop, "stop")

    def test_anthropic_protocol_parses_content_blocks(self):
        cfg = _make_config(protocol="anthropic")
        payload = json.dumps({
            "content": [
                {"type": "text", "text": "part A"},
                {"type": "text", "text": " part B"},
            ],
            "stop_reason": "end_turn",
        }).encode("utf-8")
        with mock.patch.object(_llm_api.urllib.request, "urlopen",
                               return_value=_FakeResponse(payload)):
            text, stop = _llm_api.call_anthropic_direct("hi", cfg, max_tokens=128)
        self.assertEqual(text, "part A part B")
        self.assertEqual(stop, "end_turn")

    def test_openai_posts_to_chat_completions_endpoint(self):
        cfg = _make_config(protocol="openai")
        captured: dict = {}
        payload = json.dumps({"choices": [{"message": {"content": "ok"},
                                           "finish_reason": "stop"}]}).encode()

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

    def test_anthropic_posts_to_messages_endpoint_with_api_key_header(self):
        cfg = _make_config(protocol="anthropic", api_key="sk-anthropic")
        captured: dict = {}
        payload = json.dumps({"content": [{"type": "text", "text": "ok"}],
                              "stop_reason": "end_turn"}).encode()

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

    def test_no_api_key_raises_pointing_at_conversation(self):
        cfg = _make_config(api_key="")
        with self.assertRaises(RuntimeError) as cm:
            _llm_api.call_anthropic_direct("hi", cfg)
        # The conversation-router regression test asserts "--conversation"
        # appears in the non-conversation-mode error; keep that contract.
        self.assertIn("--conversation", str(cm.exception))
        self.assertIn("API key", str(cm.exception))

    def test_empty_response_is_treated_as_failure(self):
        cfg = _make_config(protocol="openai")
        payload = json.dumps({"choices": [{"message": {"content": ""},
                                           "finish_reason": "stop"}]}).encode()
        # Fresh response per retry attempt (the buffer is single-use).
        with mock.patch.object(_llm_api.urllib.request, "urlopen",
                               side_effect=lambda *a, **k: _FakeResponse(payload)):
            with self.assertRaises(RuntimeError):
                _llm_api.call_anthropic_direct("hi", cfg, max_tokens=64)


class TestRouting(unittest.TestCase):
    def test_protocol_routes_to_direct_when_not_conversation(self):
        cfg = _make_config(conversation=False, protocol="openai")
        payload = json.dumps({"choices": [{"message": {"content": "direct"},
                                           "finish_reason": "stop"}]}).encode()
        with mock.patch.object(_llm_api.urllib.request, "urlopen",
                               return_value=_FakeResponse(payload)) as urlopen:
            text, _ = _llm_api.call_anthropic_protocol("hi", cfg, max_tokens=64)
        self.assertEqual(text, "direct")
        urlopen.assert_called_once()  # went through direct HTTP, not router

    def test_protocol_routes_to_conversation_router_when_enabled(self):
        # Conversation mode must still hand off via the router (no HTTP call),
        # even when an API key is present.
        cfg = _make_config(conversation=True, protocol="openai", api_key="sk-x")
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


if __name__ == "__main__":
    unittest.main()
