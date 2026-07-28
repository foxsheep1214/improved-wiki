"""NashSU-parity targeted recovery for truncated FILE blocks."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _file_block_repair as repair  # noqa: E402


def _block(path: str, body: str = "body") -> str:
    return (
        f"---FILE:wiki/{path}---\n"
        f"{body}\n"
        "---END FILE---\n"
    )


class TestTargetedFileRepair(unittest.TestCase):
    def test_complete_generation_needs_no_repair_call(self):
        calls: list[str] = []

        def llm_call(*args, **kwargs):
            calls.append(args[0])
            raise AssertionError("repair should not run")

        result = repair.repair_truncated_file_blocks(
            _block("concepts/a.md"),
            original_prompt="original",
            source_identity="raw/book.pdf",
            config=SimpleNamespace(),
            max_tokens=4096,
            label="test",
            llm_call=llm_call,
        )
        self.assertEqual([p for p, _ in result.blocks], ["concepts/a.md"])
        self.assertEqual(result.requested_paths, [])
        self.assertEqual(calls, [])

    def test_repairs_only_the_unclosed_path(self):
        initial = (
            _block("concepts/a.md")
            + "---FILE:wiki/concepts/b.md---\npartial"
        )
        calls: list[str] = []

        def llm_call(prompt, config, max_tokens=None, label=None):
            calls.append(prompt)
            return _block("concepts/b.md", "repaired"), "end_turn"

        result = repair.repair_truncated_file_blocks(
            initial,
            original_prompt="ORIGINAL TASK CONTEXT",
            source_identity="raw/book.pdf",
            config=SimpleNamespace(),
            max_tokens=4096,
            label="test",
            llm_call=llm_call,
        )
        self.assertEqual(
            [p for p, _ in result.blocks],
            ["concepts/a.md", "concepts/b.md"],
        )
        self.assertEqual(result.requested_paths, ["concepts/b.md"])
        self.assertEqual(result.recovered_paths, ["concepts/b.md"])
        self.assertEqual(result.unrecovered_paths, [])
        self.assertEqual(len(calls), 1)
        self.assertIn("- wiki/concepts/b.md", calls[0])
        self.assertNotIn("- wiki/concepts/a.md", calls[0])
        self.assertIn("ORIGINAL TASK CONTEXT", calls[0])

    def test_unrequested_repair_pages_are_dropped(self):
        initial = "---FILE:wiki/concepts/b.md---\npartial"

        def llm_call(prompt, config, max_tokens=None, label=None):
            return (
                _block("concepts/b.md", "repaired")
                + _block("concepts/unrequested.md", "must not escape")
            ), "end_turn"

        result = repair.repair_truncated_file_blocks(
            initial,
            original_prompt="original",
            source_identity="raw/book.pdf",
            config=SimpleNamespace(),
            max_tokens=4096,
            label="test",
            llm_call=llm_call,
        )
        self.assertEqual(
            [p for p, _ in result.blocks],
            ["concepts/b.md"],
        )
        self.assertTrue(
            any("unrequested" in warning for warning in result.warnings)
        )

    def test_failed_repair_remains_explicitly_unrecovered(self):
        def llm_call(prompt, config, max_tokens=None, label=None):
            return "not a FILE block", "end_turn"

        result = repair.repair_truncated_file_blocks(
            "---FILE:wiki/concepts/b.md---\npartial",
            original_prompt="original",
            source_identity="raw/book.pdf",
            config=SimpleNamespace(),
            max_tokens=4096,
            label="test",
            llm_call=llm_call,
        )
        self.assertEqual(result.blocks, [])
        self.assertEqual(
            result.unrecovered_paths,
            ["concepts/b.md"],
        )


if __name__ == "__main__":
    unittest.main()
