"""NashSU v0.6.7 AnyTXT collection semantics for search_local.py."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import search_local  # noqa: E402


def _hit(title: str, url: str) -> dict[str, str]:
    return {"title": title, "url": url, "snippet": title, "source": "AnyTXT"}


class TestQueryPreparation(unittest.TestCase):
    def test_unique_case_insensitive_and_max_three(self):
        self.assertEqual(
            search_local.unique_local_queries(["  GaN driver  ", "gan DRIVER", "雷达", "BMS", "extra"]),
            ["GaN driver", "雷达", "BMS"],
        )


class TestCollection(unittest.TestCase):
    @patch("search_local._search_one_query")
    def test_query_order_url_dedup_and_one_global_cap(self, search_one):
        search_one.side_effect = [
            [_hit("A", "FILE:///A"), _hit("B", "file:///b")],
            [_hit("A duplicate", "file:///a"), _hit("C", "file:///c")],
            [_hit("D", "file:///d")],
        ]
        results = search_local.search_local(Path("/project"), ["q1", "q2", "q3"], top=3)
        self.assertEqual([item["title"] for item in results], ["A", "B", "C"])
        self.assertEqual(search_one.call_count, 2)

    def test_wiki_hit_has_exact_web_search_result_shape(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            page = root / "wiki" / "concepts" / "gan.md"
            page.parent.mkdir(parents=True)
            page.write_text("---\ntitle: GaN Driver\n---\n# GaN Driver\nGaN gate driver design", encoding="utf-8")
            results = search_local._search_wiki(root, "GaN driver", 15)
            self.assertEqual(len(results), 1)
            self.assertEqual(set(results[0]), {"title", "url", "snippet", "source"})
            self.assertEqual(results[0]["source"], "AnyTXT")
            self.assertEqual(results[0]["url"], page.resolve().as_uri())


class TestCli(unittest.TestCase):
    @patch("search_local.search_local", return_value=[])
    def test_clean_zero_results_is_success_and_json_array(self, _search):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            status = search_local.main([
                "no hits",
                "--project", "/project",
                "--json",
            ])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout.getvalue()), [])


if __name__ == "__main__":
    unittest.main()
