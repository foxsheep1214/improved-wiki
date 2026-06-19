"""Tests for _llm_call — config resolution + protocol routing + retry.

Monkeypatches _llm_call._http_json_post so no real network. Also forces
retry sleeps to no-ops so transient-retry tests are instant.

Run:  python3 scripts/tests/test_llm_call.py
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _llm_call as lc  # noqa: E402


class TestResolveLlmConfig(unittest.TestCase):
    def setUp(self):
        self._orig_env = {k: os.environ.get(k) for k in
                          ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
                           "LLM_PROTOCOL", "LLM_PROVIDER")}
        for k in self._orig_env:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._orig_env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_resolves_from_env(self):
        os.environ["LLM_API_KEY"] = "sk-test"
        os.environ["LLM_BASE_URL"] = "https://api.example.com"
        os.environ["LLM_MODEL"] = "test-model"
        os.environ["LLM_PROTOCOL"] = "openai"
        cfg = lc.resolve_llm_config()
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.api_key, "sk-test")
        self.assertEqual(cfg.protocol, "openai")

    def test_returns_none_when_missing_key(self):
        # No env, and config.json likely absent in CI — assert None or a
        # resolved config only if a real ~/.agents/config.json exists.
        cfg = lc.resolve_llm_config()
        home_cfg = Path.home() / ".agents" / "config.json"
        if home_cfg.exists():
            self.assertIsNotNone(cfg)  # real config present
        else:
            self.assertIsNone(cfg)

    def test_defaults_protocol_to_anthropic(self):
        os.environ["LLM_API_KEY"] = "sk-test"
        os.environ["LLM_BASE_URL"] = "https://api.example.com"
        os.environ["LLM_MODEL"] = "m"
        os.environ["LLM_PROTOCOL"] = "bogus"
        cfg = lc.resolve_llm_config()
        self.assertEqual(cfg.protocol, "anthropic")


class TestProtocolRouting(unittest.TestCase):
    """Monkeypatch _http_json_post to capture the request and return canned JSON."""

    def setUp(self):
        self._captured = {}
        def fake_post(url, headers, body, *, timeout):
            self._captured["url"] = url
            self._captured["headers"] = headers
            self._captured["body"] = json.loads(body)
            return self._response
        self._fake_post = fake_post
        self._orig_post = lc._http_json_post
        self._orig_sleep = lc._sleep
        lc._http_json_post = fake_post
        lc._sleep = lambda _s: None
        self._response = {}

    def tearDown(self):
        lc._http_json_post = self._orig_post
        lc._sleep = self._orig_sleep

    def test_anthropic_protocol_uses_system_field(self):
        self._response = {"content": [{"type": "text", "text": "hello"}]}
        cfg = lc.LLMConfig("sk", "https://api.example.com", "m", "anthropic")
        out = lc.llm_call("SYS", "USER", config=cfg)
        self.assertEqual(out, "hello")
        self.assertTrue(self._captured["url"].endswith("/anthropic/v1/messages"))
        self.assertEqual(self._captured["body"]["system"], "SYS")
        self.assertEqual(self._captured["body"]["messages"][0]["content"], "USER")
        self.assertEqual(self._captured["headers"]["x-api-key"], "sk")

    def test_openai_protocol_uses_system_role(self):
        self._response = {"choices": [{"message": {"content": "hi"}}]}
        cfg = lc.LLMConfig("sk", "https://api.example.com", "m", "openai")
        out = lc.llm_call("SYS", "USER", config=cfg)
        self.assertEqual(out, "hi")
        self.assertTrue(self._captured["url"].endswith("/v1/chat/completions"))
        msgs = self._captured["body"]["messages"]
        self.assertEqual(msgs[0], {"role": "system", "content": "SYS"})
        self.assertEqual(msgs[1], {"role": "user", "content": "USER"})
        self.assertEqual(self._captured["headers"]["Authorization"], "Bearer sk")

    def test_empty_anthropic_content_raises_transient(self):
        self._response = {"content": []}
        cfg = lc.LLMConfig("sk", "https://api.example.com", "m", "anthropic")
        with self.assertRaises(RuntimeError):
            lc.llm_call("SYS", "USER", config=cfg)


class TestRetry(unittest.TestCase):
    def setUp(self):
        self._orig_sleep = lc._sleep
        lc._sleep = lambda _s: None
        self._calls = 0
        self._orig_post = lc._http_json_post

    def tearDown(self):
        lc._sleep = self._orig_sleep
        lc._http_json_post = self._orig_post

    def test_retries_on_transient_then_succeeds(self):
        from urllib.error import HTTPError
        from io import BytesIO

        responses = iter([
            HTTPError("u", 503, "Service Unavailable", {}, BytesIO(b"busy")),
            {"content": [{"type": "text", "text": "ok"}]},
        ])

        def fake_post(url, headers, body, *, timeout):
            r = next(responses)
            self._calls += 1
            if isinstance(r, Exception):
                raise r
            return r

        lc._http_json_post = fake_post
        cfg = lc.LLMConfig("sk", "https://api.example.com", "m", "anthropic")
        out = lc.llm_call("SYS", "USER", config=cfg)
        self.assertEqual(out, "ok")
        self.assertEqual(self._calls, 2)

    def test_does_not_retry_non_transient(self):
        # _http_json_post wraps HTTP 400 into RuntimeError("LLM API HTTP 400: ..."),
        # which has no transient marker → not retried. The fake mimics that wrap.
        def fake_post(url, headers, body, *, timeout):
            self._calls += 1
            raise RuntimeError("LLM API HTTP 400: {\"error\":\"bad\"}")

        lc._http_json_post = fake_post
        cfg = lc.LLMConfig("sk", "https://api.example.com", "m", "anthropic")
        with self.assertRaisesRegex(RuntimeError, r"HTTP 400"):
            lc.llm_call("SYS", "USER", config=cfg)
        self.assertEqual(self._calls, 1)


class TestMakeLlmCallable(unittest.TestCase):
    def setUp(self):
        self._orig_post = lc._http_json_post

    def tearDown(self):
        lc._http_json_post = self._orig_post

    def test_bound_callable_returns_text(self):
        cfg = lc.LLMConfig("sk", "https://api.example.com", "m", "anthropic")
        lc._http_json_post = lambda u, h, b, *, timeout: {"content": [{"type": "text", "text": "X"}]}
        fn = lc.make_llm_callable(cfg, max_tokens=128)
        self.assertEqual(fn("s", "u"), "X")

    def test_raises_when_no_config(self):
        orig = lc.resolve_llm_config
        lc.resolve_llm_config = lambda: None
        try:
            with self.assertRaisesRegex(RuntimeError, "No LLM config"):
                lc.make_llm_callable()
        finally:
            lc.resolve_llm_config = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
