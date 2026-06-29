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
                self.assertEqual(findings[0]["severity"], "info")
                self.assertEqual(findings[0]["page"], "Add a datasheet")
                self.assertEqual(findings[0]["affectedPages"], ["concepts/buck.md"])
            finally:
                sys.argv = old_argv
                if old_root is None:
                    os.environ.pop("IMPROVED_WIKI_ROOT", None)
                else:
                    os.environ["IMPROVED_WIKI_ROOT"] = old_root


class TestBatching(unittest.TestCase):
    def test_chunk_batches_small_returns_single(self):
        wls = _load_module()
        summaries = [("p%d.md" % i, "text") for i in range(5)]
        batches = wls.chunk_batches(summaries, batch_pages=200)
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]), 5)

    def test_chunk_batches_splits_at_boundary(self):
        wls = _load_module()
        summaries = [("p%d.md" % i, "text") for i in range(7)]
        batches = wls.chunk_batches(summaries, batch_pages=3)
        self.assertEqual(len(batches), 3)
        self.assertEqual([len(b) for b in batches], [3, 3, 1])

    def test_dedup_findings_collapses_cross_batch_dupes(self):
        wls = _load_module()
        findings = [
            {"page": "Add a datasheet", "detail": "[suggestion] link a datasheet"},
            {"page": "add a datasheet", "detail": "[suggestion] link a datasheet"},
            {"page": "Add a datasheet", "detail": "[stale] outdated ref"},
            {"page": "Other", "detail": "[suggestion] something else"},
        ]
        out = wls.dedup_findings(findings)
        self.assertEqual(len(out), 3)


class TestLanguageDirectiveInPrompt(unittest.TestCase):
    """The enriched language directive (proper-noun / identifier preservation
    clauses + override) must reach the semantic-lint system prompt."""

    def test_preservation_clauses_present_in_system_prompt(self):
        wls = _load_module()
        summaries = [("concepts/buck.md", "A buck converter steps down voltage.")]
        system_prompt, _ = wls.build_prompt(summaries)
        self.assertIn("MANDATORY OUTPUT LANGUAGE", system_prompt)
        self.assertIn("Do not translate, transliterate", system_prompt)
        self.assertIn("paper titles", system_prompt)
        self.assertIn("URLs", system_prompt)

    def test_output_language_override_reaches_prompt(self):
        wls = _load_module()
        old = os.environ.get("IMPROVED_WIKI_OUTPUT_LANGUAGE")
        os.environ["IMPROVED_WIKI_OUTPUT_LANGUAGE"] = "French"
        try:
            summaries = [("concepts/buck.md", "A buck converter steps down voltage.")]
            system_prompt, _ = wls.build_prompt(summaries)
            self.assertIn("French", system_prompt)
        finally:
            if old is None:
                os.environ.pop("IMPROVED_WIKI_OUTPUT_LANGUAGE", None)
            else:
                os.environ["IMPROVED_WIKI_OUTPUT_LANGUAGE"] = old


class TestSemanticLintBatchedE2E(unittest.TestCase):
    def test_two_batches_resume_separately(self):
        """3 pages, batch size 2 → 2 batches. Round 1: batch 1 pending (101).
        Answer it; round 2: batch 2 pending (101). Answer it; round 3: both
        cached → 0, findings merged + deduped + renumbered."""
        wls = _load_module()
        original_batch = wls.SEMANTIC_BATCH_PAGES
        wls.SEMANTIC_BATCH_PAGES = 2
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            wiki = root / "wiki" / "concepts"
            wiki.mkdir(parents=True)
            for name in ("buck", "boost", "flyback"):
                (wiki / f"{name}.md").write_text(_page(
                    f"type: concept\ntitle: {name}\n",
                    f"# {name}\nA {name} converter.",
                ), encoding="utf-8")

            old_root = os.environ.get("IMPROVED_WIKI_ROOT")
            old_argv = sys.argv
            os.environ["IMPROVED_WIKI_ROOT"] = str(root)
            sys.argv = ["wiki-lint-semantic.py"]
            conv_dir = root / ".llm-wiki" / "conversation" / "semantic-lint"
            try:
                self.assertEqual(wls.main(), 101)
                md1 = [p for p in conv_dir.glob("*.md")
                       if not p.with_suffix(".txt").exists()]
                self.assertEqual(len(md1), 1)
                md1[0].with_suffix(".txt").write_text(
                    "---LINT: suggestion | info | Buck note---\n"
                    "PAGES: concepts/buck.md\n Buck detail.\n---END LINT---\n",
                    encoding="utf-8")

                self.assertEqual(wls.main(), 101)
                md2 = [p for p in conv_dir.glob("*.md")
                       if not p.with_suffix(".txt").exists()]
                self.assertEqual(len(md2), 1)
                md2[0].with_suffix(".txt").write_text(
                    "---LINT: suggestion | info | Flyback note---\n"
                    "PAGES: concepts/flyback.md\n Flyback detail.\n---END LINT---\n",
                    encoding="utf-8")

                self.assertEqual(wls.main(), 0)
                findings = json.loads(
                    (root / ".llm-wiki" / "lint-semantic.json").read_text("utf-8"))
                self.assertEqual(len(findings), 2)
                self.assertEqual({f["page"] for f in findings},
                                 {"Buck note", "Flyback note"})
                ids = [f["id"] for f in findings]
                self.assertEqual(len(set(ids)), len(ids))
            finally:
                wls.SEMANTIC_BATCH_PAGES = original_batch
                sys.argv = old_argv
                if old_root is None:
                    os.environ.pop("IMPROVED_WIKI_ROOT", None)
                else:
                    os.environ["IMPROVED_WIKI_ROOT"] = old_root


if __name__ == "__main__":
    unittest.main()
