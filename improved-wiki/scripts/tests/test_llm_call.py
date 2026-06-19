"""Tests for _llm_call conversation-mode handoff (round ii).

_llm_call was retargeted from HTTP-direct to a conversation prompt-file
handoff: each (system, user) call either returns a cached result (resume)
or writes a prompt file and raises ConversationPending. This is the
mechanism dedup_sweep.py and wiki-lint-semantic.py use in conversation mode.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _core import ConversationPending
from _llm_call import make_conversation_llm_call, slug_for


class TestSlugFor(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(
            slug_for("sys", "user"),
            slug_for("sys", "user"),
        )

    def test_different_inputs_different_slug(self):
        self.assertNotEqual(slug_for("sys", "user-a"), slug_for("sys", "user-b"))
        self.assertNotEqual(slug_for("sys-a", "user"), slug_for("sys-b", "user"))

    def test_filesystem_safe(self):
        slug = slug_for("sys" * 100, "user" * 100)
        self.assertRegex(slug, r"^[0-9a-f]{16}$")


class TestConversationHandoff(unittest.TestCase):
    def test_cache_miss_writes_prompt_and_raises(self):
        with tempfile.TemporaryDirectory() as d:
            runtime = Path(d)
            llm_call = make_conversation_llm_call(runtime, stage_prefix="dedup")
            with self.assertRaises(ConversationPending):
                llm_call("detect system", "summaries go here")
            conv_dir = runtime / "conversation" / "dedup"
            md_files = list(conv_dir.glob("*.md"))
            self.assertEqual(len(md_files), 1)
            body = md_files[0].read_text(encoding="utf-8")
            self.assertIn("detect system", body)
            self.assertIn("summaries go here", body)
            # Result file not yet written by the agent
            self.assertEqual(list(conv_dir.glob("*.txt")), [])

    def test_cache_hit_returns_result(self):
        with tempfile.TemporaryDirectory() as d:
            runtime = Path(d)
            llm_call = make_conversation_llm_call(runtime, stage_prefix="dedup")
            with self.assertRaises(ConversationPending):
                llm_call("detect system", "summaries go here")
            # Simulate the calling agent answering:
            conv_dir = runtime / "conversation" / "dedup"
            md = next(conv_dir.glob("*.md"))
            md.with_suffix(".txt").write_text('{"groups":[]}', encoding="utf-8")

            # A fresh callable (same runtime/stage — models a re-invoke) returns
            # the cached result instead of raising again.
            llm_call2 = make_conversation_llm_call(runtime, stage_prefix="dedup")
            self.assertEqual(llm_call2("detect system", "summaries go here"),
                             '{"groups":[]}')

    def test_stage_prefix_isolation(self):
        with tempfile.TemporaryDirectory() as d:
            runtime = Path(d)
            # Same prompt, different stage prefix → different files, no cross-read.
            for stage in ("dedup", "semantic-lint"):
                llm_call = make_conversation_llm_call(runtime, stage_prefix=stage)
                with self.assertRaises(ConversationPending):
                    llm_call("sys", "user")
            self.assertTrue((runtime / "conversation" / "dedup").is_dir())
            self.assertTrue((runtime / "conversation" / "semantic-lint").is_dir())

    def test_different_user_messages_get_distinct_cache_files(self):
        with tempfile.TemporaryDirectory() as d:
            runtime = Path(d)
            llm_call = make_conversation_llm_call(runtime, stage_prefix="dedup")
            with self.assertRaises(ConversationPending):
                llm_call("merge system", "group A content")
            with self.assertRaises(ConversationPending):
                llm_call("merge system", "group B content")
            conv_dir = runtime / "conversation" / "dedup"
            self.assertEqual(len(list(conv_dir.glob("*.md"))), 2)


if __name__ == "__main__":
    unittest.main()
