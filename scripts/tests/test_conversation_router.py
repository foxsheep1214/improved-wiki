"""Tests for the conversation-mode router (round ii).

Verifies that:
  * ingest.py registers its `call_anthropic_protocol` as the conversation
    router on `_llm_api` at import time, so the stage modules (which import
    `call_anthropic_protocol` from `_llm_api`) route through conversation mode
    automatically.
  * `_llm_api.call_anthropic_protocol` performs the prompt-file handoff
    (writes prompt, raises ConversationPending) and, on re-invoke with a
    result file present, returns the cached response.
  * Without conversation mode the call raises (http-direct removed).
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _llm_api
import ingest  # noqa: F401  (import side-effect: registers the router)
from _core import Config, ConversationPending


def _make_config(tmp: Path, *, conversation: bool) -> Config:
    return Config(
        wiki_root=tmp / "wiki",
        raw_root=tmp / "raw",
        wiki_dir=tmp / "wiki",
        runtime_dir=tmp / "rt",
        cache_path=tmp / "rt" / "ingest-cache.json",
        progress_dir=tmp / "rt" / "ingest-progress",
        extract_tmp_dir=tmp / "rt" / "extract-tmp",
        llm_base_url="https://example.invalid",
        llm_model="test-model",
        llm_api_key="",
        llm_protocol="anthropic",
        caption_api_key="",
        caption_base_url="https://example.invalid",
        caption_model="test-caption",
        chunk_size=60000,
        chunk_overlap=3000,
        source_budget=100000,
        target_chars=60000,
        max_tokens=8192,
        conversation_mode=conversation,
        conversation_prefix="ab12cd34",
    )


class TestRouterRegistration(unittest.TestCase):
    def test_router_registered_at_import(self):
        self.assertIsNotNone(_llm_api._conversation_router)
        self.assertIs(_llm_api._conversation_router, ingest.call_anthropic_protocol)


class TestConversationHandoff(unittest.TestCase):
    def test_writes_prompt_and_raises_pending(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp, conversation=True)
            with self.assertRaises(ConversationPending):
                _llm_api.call_anthropic_protocol("analyze this text", cfg)
            conv_dir = cfg.runtime_dir / "conversation" / cfg.conversation_prefix
            md_files = list(conv_dir.glob("*.md"))
            self.assertEqual(len(md_files), 1)
            self.assertIn("analyze this text", md_files[0].read_text(encoding="utf-8"))

    def test_returns_cached_result_on_reinvoke(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp, conversation=True)
            with self.assertRaises(ConversationPending):
                _llm_api.call_anthropic_protocol("build a digest", cfg, max_tokens=2048)
            conv_dir = cfg.runtime_dir / "conversation" / cfg.conversation_prefix
            md = next(conv_dir.glob("*.md"))
            result = md.with_suffix(".txt")
            result.write_text("digest: ready", encoding="utf-8")

            text, stop = _llm_api.call_anthropic_protocol("build a digest", cfg, max_tokens=2048)
            self.assertEqual(text, "digest: ready")
            self.assertEqual(stop, "end_turn")

    def test_without_conversation_mode_raises(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp, conversation=False)
            with self.assertRaises(RuntimeError) as cm:
                _llm_api.call_anthropic_protocol("any prompt", cfg)
            self.assertIn("--conversation", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
