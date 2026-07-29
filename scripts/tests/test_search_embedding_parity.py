"""NashSU 0.6.6 vector-search aggregation/fallback behavior."""
from __future__ import annotations

import io
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import search_wiki  # noqa: E402


class SearchFallbackTests(unittest.TestCase):
    def test_vector_failure_warns_and_returns_keyword_results(self):
        keyword = [{
            "path": "concepts/a.md",
            "title": "A",
            "snippet": "keyword hit",
            "score": 20.0,
            "title_match": False,
        }]
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    ["search_wiki.py", "query", "--project", tmp],
                ),
                mock.patch.object(
                    search_wiki, "keyword_search", return_value=keyword
                ),
                mock.patch.object(
                    search_wiki,
                    "_vector_search",
                    return_value=([], "endpoint unavailable"),
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                code = search_wiki.main()
        self.assertEqual(code, 0)
        self.assertIn("mode=keyword", stdout.getvalue())
        self.assertIn("continuing keyword-only", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
