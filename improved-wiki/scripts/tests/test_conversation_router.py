"""Tests for the conversation-mode router (round iv, 2026-06-22).

Verifies that:
  * ingest.py registers its `call_anthropic_protocol` as the conversation
    router on `_llm_api` at import time, so the stage modules (which import
    `call_anthropic_protocol` from `_llm_api`) route through conversation mode
    automatically.
  * `_llm_api.call_anthropic_protocol` performs the prompt-file handoff
    (writes prompt, raises ConversationPending) and, on re-invoke with a
    result file present, returns the cached response.

Conversation mode is the only text-gen path now — there is no "without
conversation mode" state to test (see test_llm_api_direct.py for the
no-router-registered error case).
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _llm_api
import ingest  # noqa: F401  (import side-effect: registers the router)
from _core import Config, ConversationPending


def _make_config(tmp: Path) -> Config:
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
            cfg = _make_config(tmp)
            with self.assertRaises(ConversationPending):
                _llm_api.call_anthropic_protocol("analyze this text", cfg)
            conv_dir = cfg.runtime_dir / "conversation" / cfg.conversation_prefix
            md_files = list(conv_dir.glob("*.md"))
            self.assertEqual(len(md_files), 1)
            self.assertIn("analyze this text", md_files[0].read_text(encoding="utf-8"))

    def test_returns_cached_result_on_reinvoke(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            with self.assertRaises(ConversationPending):
                _llm_api.call_anthropic_protocol("build a digest", cfg, max_tokens=2048)
            conv_dir = cfg.runtime_dir / "conversation" / cfg.conversation_prefix
            md = next(conv_dir.glob("*.md"))
            result = md.with_suffix(".txt")
            result.write_text("digest: ready", encoding="utf-8")

            text, stop = _llm_api.call_anthropic_protocol("build a digest", cfg, max_tokens=2048)
            self.assertEqual(text, "digest: ready")
            self.assertEqual(stop, "end_turn")

    def test_cached_result_survives_replay_for_multi_stage_resume(self):
        # Regression: ingest.py replays every stage from the top on each
        # re-invoke. If a consumed .txt is deleted, earlier stages re-prompt
        # on the next invoke and the pipeline never advances past stage 1.
        # A cached result must remain readable on a second consume.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            with self.assertRaises(ConversationPending):
                _llm_api.call_anthropic_protocol("build a digest", cfg, max_tokens=2048)
            conv_dir = cfg.runtime_dir / "conversation" / cfg.conversation_prefix
            md = next(conv_dir.glob("*.md"))
            md.with_suffix(".txt").write_text("digest: ready", encoding="utf-8")

            # First consume (stage 1 of this invoke).
            t1, _ = _llm_api.call_anthropic_protocol("build a digest", cfg, max_tokens=2048)
            self.assertEqual(t1, "digest: ready")
            # Second consume (stage 1 of the NEXT invoke — replay).
            t2, _ = _llm_api.call_anthropic_protocol("build a digest", cfg, max_tokens=2048)
            self.assertEqual(t2, "digest: ready")
            # The .txt must still exist for future replays.
            self.assertTrue(md.with_suffix(".txt").exists())

    def test_distinct_prompts_get_distinct_slugs(self):
        # Regression: _infer_stage maps several distinct Stage-2 calls
        # (source page, main generation, per-concept fallback) to the same
        # 'LLM-task' stage name. They must still get distinct cache files,
        # or the source-page answer gets reused for concept/entity generation
        # (wrong content → 0 valid blocks). A content-hash suffix guarantees
        # distinct prompts → distinct slugs.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            with self.assertRaises(ConversationPending):
                _llm_api.call_anthropic_protocol("write source page for part A", cfg)
            with self.assertRaises(ConversationPending):
                _llm_api.call_anthropic_protocol("write source page for part B", cfg)
            conv_dir = cfg.runtime_dir / "conversation" / cfg.conversation_prefix
            md_files = list(conv_dir.glob("*.md"))
            self.assertEqual(len(md_files), 2,
                             "distinct prompts must get distinct cache files")

    def test_volatile_wiki_page_list_does_not_invalidate_cache(self):
        # Regression: stage prompts embed an "Existing wiki pages" snapshot
        # that changes as the wiki grows (lint pages, new ingests). Hashing
        # the full prompt made the slug change every invoke → cache thrash →
        # Stage 1 re-prompted forever. The slug hash must redact that volatile
        # list so two prompts differing ONLY in the wiki-page list share a slug.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            base = "build a digest\n- Existing wiki pages: overview, schema\n"
            with self.assertRaises(ConversationPending):
                _llm_api.call_anthropic_protocol(base, cfg, max_tokens=2048)
            conv_dir = cfg.runtime_dir / "conversation" / cfg.conversation_prefix
            first_md = next(conv_dir.glob("*.md"))
            # Same prompt but the wiki-page list grew (lint pages added, etc.).
            grown = "build a digest\n- Existing wiki pages: overview, schema, lint-x, lint-y\n"
            with self.assertRaises(ConversationPending):
                _llm_api.call_anthropic_protocol(grown, cfg, max_tokens=2048)
            md_files = list(conv_dir.glob("*.md"))
            # Both prompts must map to the SAME cache file (slug stable across
            # wiki-page-list changes) — not two separate files.
            self.assertEqual(len(md_files), 1,
                             f"volatile wiki list must not split the cache; got {[m.name for m in md_files]}")
            self.assertEqual(md_files[0], first_md)


if __name__ == "__main__":
    unittest.main()
