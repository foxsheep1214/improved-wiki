"""Tests for call_anthropic_protocol's conversation-mode routing.

Text generation has exactly one path: ``call_anthropic_protocol`` always hands
off to the registered conversation router and never makes a direct HTTP call.
(The former direct-HTTP helper ``call_anthropic_direct`` and its progress-hook
plumbing were removed when the dead direct-API text-gen path was deleted — it
had no caller and contradicted the conversation-mode-only design.)

Stdlib `unittest` only — no pytest, no network, no real LLM calls.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _llm_api
from _core import Config


def _make_config() -> Config:
    # Config no longer carries llm_base_url/llm_api_key/llm_protocol (dropped
    # 2026-07 with the dead direct-HTTP text-gen path) — routing must go
    # through the conversation router regardless, which is what these tests pin.
    return Config(
        wiki_root=Path("/tmp/wiki"),
        raw_root=Path("/tmp/raw"),
        wiki_dir=Path("/tmp/wiki"),
        runtime_dir=Path("/tmp/rt"),
        cache_path=Path("/tmp/rt/ingest-cache.json"),
        progress_dir=Path("/tmp/rt/ingest-progress"),
        extract_tmp_dir=Path("/tmp/rt/extract-tmp"),
        llm_model="test-model",
        caption_api_key="",
        caption_base_url="https://provider.example",
        caption_model="test-caption",
        chunk_overlap=3000,
        source_budget=100000,
        target_chars=60000,
        target_tokens=30000,
        max_tokens=8192,
        conversation_prefix="ab12cd34",
    )


class TestRouting(unittest.TestCase):
    def test_protocol_always_routes_to_conversation_router(self):
        # Text generation has exactly one path now — call_anthropic_protocol
        # must hand off via the router and never touch HTTP directly.
        cfg = _make_config()
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
        cfg = _make_config()
        with mock.patch.object(_llm_api, "_conversation_router", None):
            with self.assertRaises(RuntimeError) as cm:
                _llm_api.call_anthropic_protocol("hi", cfg)
        self.assertIn("router", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
