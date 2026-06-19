"""End-to-end conversation-handoff test for wiki-lint-semantic (round ii).

The module filename has a hyphen so it is loaded via importlib. Verifies:
  * main() exits 101 on first run (prompt written, ConversationPending).
  * After the calling agent writes the result file, main() resumes to 0 and
    writes lint-semantic.json with the parsed findings.
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "wiki_lint_semantic", _SCRIPTS_DIR / "wiki-lint-semantic.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _page(fm: str, body: str) -> str:
    return f"---\n{fm}\n---\n\n{body}"


class TestSemanticLintConversation(unittest.TestCase):
    def test_main_pending_then_resume(self):
        wls = _load_module()
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            wiki = root / "wiki"
            (wiki / "concepts").mkdir(parents=True)
            (wiki / "concepts" / "buck.md").write_text(_page(
                "type: concept\ntitle: Buck\n",
                "# Buck\nA buck converter steps down voltage.",
            ), encoding="utf-8")

            old_root = os.environ.get("IMPROVED_WIKI_ROOT")
            old_argv = sys.argv
            os.environ["IMPROVED_WIKI_ROOT"] = str(root)
            sys.argv = ["wiki-lint-semantic.py"]
            try:
                # First run: uncached → ConversationPending → 101.
                self.assertEqual(wls.main(), 101)
                conv_dir = root / ".llm-wiki" / "conversation" / "semantic-lint"
                md_files = list(conv_dir.glob("*.md"))
                self.assertEqual(len(md_files), 1)

                # Simulate the agent answering with one LINT block.
                md = md_files[0]
                md.with_suffix(".txt").write_text(
                    "---LINT: suggestion | info | Add a datasheet---\n"
                    "PAGES: concepts/buck.md\n"
                    "Consider linking a switching-regulator datasheet.\n"
                    "---END LINT---\n",
                    encoding="utf-8",
                )

                # Second run: cached result read → findings written → 0.
                self.assertEqual(wls.main(), 0)
                findings = json.loads(
                    (root / ".llm-wiki" / "lint-semantic.json").read_text("utf-8")
                )
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0]["type"], "semantic")
                self.assertEqual(findings[0]["severity"], "info")
                self.assertEqual(findings[0]["page"], "Add a datasheet")
                self.assertEqual(findings[0]["affectedPages"], ["concepts/buck.md"])
            finally:
                sys.argv = old_argv
                if old_root is None:
                    os.environ.pop("IMPROVED_WIKI_ROOT", None)
                else:
                    os.environ["IMPROVED_WIKI_ROOT"] = old_root


if __name__ == "__main__":
    unittest.main()
