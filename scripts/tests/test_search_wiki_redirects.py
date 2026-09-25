"""search_wiki.py resolves dedup redirect stubs to their canonical page."""
from __future__ import annotations

import io
import json
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


def _stub(title: str, target: str) -> str:
    return (f'---\ntype: redirect\ntitle: "{title}"\nredirect: "{target}"\n---\n'
            f"# {title}\n\n已合并到 [[{target}]]。\n")


class RedirectResolutionTests(unittest.TestCase):
    def _search(self, root: Path, query: str) -> list[dict]:
        stdout = io.StringIO()
        argv = ["search_wiki.py", query, "--project", str(root),
                "--keyword-only", "--json"]
        with (mock.patch.object(sys, "argv", argv),
              redirect_stdout(stdout), redirect_stderr(io.StringIO())):
            search_wiki.main()
        return json.loads(stdout.getvalue())

    def test_stubs_collapse_into_their_canonical_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            comparisons = root / "wiki" / "comparisons"
            comparisons.mkdir(parents=True)
            title = "源端匹配与终端匹配比较"
            (comparisons / "source-vs-end.md").write_text(
                _stub(title, "comparisons/termination-options"), encoding="utf-8")
            # iCloud conflict copy of the same stub
            (comparisons / "source-vs-end 2.md").write_text(
                _stub(title, "comparisons/termination-options"), encoding="utf-8")
            (comparisons / "termination-options.md").write_text(
                "---\ntype: comparison\ntitle: 传输线端接方式\n---\n\n"
                "# 传输线端接方式\n\n源端匹配吸收反射，终端匹配消除入射端反射。\n",
                encoding="utf-8")

            results = self._search(root, title)

        paths = [r["path"] for r in results]
        self.assertEqual(paths[0], "comparisons/termination-options.md")
        self.assertEqual(paths.count("comparisons/termination-options.md"), 1)
        self.assertNotIn("comparisons/source-vs-end.md", paths)
        self.assertNotIn("comparisons/source-vs-end 2.md", paths)
        self.assertEqual(results[0]["title"], "传输线端接方式")
        self.assertIn(results[0]["redirected_from"],
                      {"comparisons/source-vs-end.md",
                       "comparisons/source-vs-end 2.md"})
        self.assertIn("源端匹配", results[0]["snippet"])

    def test_stub_with_missing_or_escaping_target_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            concepts = root / "wiki" / "concepts"
            concepts.mkdir(parents=True)
            (concepts / "gone.md").write_text(
                _stub("zebra gone", "concepts/does-not-exist"), encoding="utf-8")
            (concepts / "escape.md").write_text(
                _stub("zebra escape", "../../outside"), encoding="utf-8")
            (root / "outside.md").write_text("zebra", encoding="utf-8")

            paths = {r["path"] for r in self._search(root, "zebra")}

        self.assertEqual(paths, {"concepts/gone.md", "concepts/escape.md"})


if __name__ == "__main__":
    unittest.main()
