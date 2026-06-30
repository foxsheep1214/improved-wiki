"""Tests for _source_filter.py — NashSU source-filter.ts.

Verifies the sensitive-config gate is narrow: only config extensions under
known agent/tool config dirs are refused. Stdlib unittest only.

Run:  python3 scripts/tests/test_source_filter.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _source_filter import (  # noqa: E402
    is_hidden_source_entry,
    is_sensitive_config_source_file,
)


class TestHiddenSourceEntry(unittest.TestCase):
    def test_ds_store_and_cache_are_hidden(self):
        self.assertTrue(is_hidden_source_entry(".DS_Store"))
        self.assertTrue(is_hidden_source_entry(".cache"))

    def test_normal_names_not_hidden(self):
        self.assertFalse(is_hidden_source_entry("chapter1.pdf"))
        self.assertFalse(is_hidden_source_entry(".gitignore"))


class TestSensitiveConfigSourceFile(unittest.TestCase):
    def test_config_file_under_claude_dir_is_sensitive(self):
        self.assertTrue(is_sensitive_config_source_file("raw/src/.claude/settings.json"))
        self.assertTrue(is_sensitive_config_source_file("/abs/.mcp/config.yaml"))
        self.assertTrue(is_sensitive_config_source_file("proj/.codex/.env"))

    def test_case_insensitive_dir_match(self):
        self.assertTrue(is_sensitive_config_source_file("x/.CLAUDE/settings.json"))

    def test_config_ext_outside_config_dir_is_not_sensitive(self):
        # notes.json next to a book is fine — both conditions required.
        self.assertFalse(is_sensitive_config_source_file("raw/sources/book/notes.json"))
        self.assertFalse(is_sensitive_config_source_file("raw/sources/book/meta.yaml"))

    def test_non_config_ext_under_config_dir_is_not_sensitive(self):
        self.assertFalse(is_sensitive_config_source_file("raw/.claude/notes.md"))
        self.assertFalse(is_sensitive_config_source_file("raw/.claude/readme.txt"))

    def test_pdf_is_never_sensitive(self):
        self.assertFalse(is_sensitive_config_source_file("raw/.claude/paper.pdf"))

    def test_backslash_paths(self):
        self.assertTrue(is_sensitive_config_source_file("raw\\.cursor\\mcp.json"))

    def test_empty_path(self):
        self.assertFalse(is_sensitive_config_source_file(""))


if __name__ == "__main__":
    unittest.main()
