"""Tests for lint_verify_semantic.py — adversarial re-verification of
severity=="warning" semantic-lint findings against FULL page content
(the semantic pass itself only ever sees a 500-char preview per page).

The module filename has underscores (not hyphens like wiki-lint-semantic.py)
so it's importable directly.
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
        "lint_verify_semantic", _SCRIPTS_DIR / "lint_verify_semantic.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _page(fm: str, body: str) -> str:
    return f"---\n{fm}\n---\n\n{body}"


class TestFindingsToVerify(unittest.TestCase):
    def test_filters_to_unverified_warnings_only(self):
        lvs = _load_module()
        findings = [
            {"id": "a", "severity": "warning", "detail": "[stale] x"},
            {"id": "b", "severity": "info", "detail": "[suggestion] y"},
            {"id": "c", "severity": "warning", "detail": "[contradiction] z",
             "verified": "confirmed"},
        ]
        out = lvs.findings_to_verify(findings)
        self.assertEqual([f["id"] for f in out], ["a"])


class TestReadAffectedPages(unittest.TestCase):
    def test_reads_existing_marks_missing_pages_none(self):
        lvs = _load_module()
        with tempfile.TemporaryDirectory() as t:
            wiki = Path(t) / "wiki"
            (wiki / "concepts").mkdir(parents=True)
            (wiki / "concepts" / "a.md").write_text(
                _page("type: concept\ntitle: A", "# A\nbody"), encoding="utf-8")
            out = lvs.read_affected_pages(
                wiki, ["concepts/a.md", "concepts/does-not-exist.md"])
            self.assertIn("# A", out["concepts/a.md"])
            self.assertIsNone(out["concepts/does-not-exist.md"])


class TestParseVerifyBlocks(unittest.TestCase):
    def test_parses_hyphenated_finding_id(self):
        """Regression guard: finding ids look like 'lint-semantic-12' (hyphens
        are common). The header format here keys off id=<token>--- rather than
        a bare |-delimited field, specifically to avoid the same silent-drop
        bug fixed in wiki-lint-semantic.py's LINT_BLOCK_REGEX."""
        lvs = _load_module()
        raw = (
            "---VERIFY id=lint-semantic-12---\n"
            "VERDICT: confirmed\n"
            "REASON: 两页确实描述同一概念，且互不链接。\n"
            "---END VERIFY---\n"
        )
        out = lvs.parse_verify_blocks(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out["lint-semantic-12"]["verdict"], "confirmed")
        self.assertIn("同一概念", out["lint-semantic-12"]["reason"])

    def test_parses_multiple_blocks_mixed_verdicts(self):
        lvs = _load_module()
        raw = (
            "---VERIFY id=lint-semantic-1---\n"
            "VERDICT: refuted\n"
            "REASON: 两页实际内容不同，不是重复。\n"
            "---END VERIFY---\n"
            "---VERIFY id=lint-semantic-2---\n"
            "VERDICT: uncertain\n"
            "REASON: 给出的页面内容不足以判断。\n"
            "---END VERIFY---\n"
        )
        out = lvs.parse_verify_blocks(raw)
        self.assertEqual(set(out.keys()), {"lint-semantic-1", "lint-semantic-2"})
        self.assertEqual(out["lint-semantic-1"]["verdict"], "refuted")
        self.assertEqual(out["lint-semantic-2"]["verdict"], "uncertain")

    def test_unknown_verdict_word_falls_back_to_uncertain(self):
        lvs = _load_module()
        raw = (
            "---VERIFY id=x---\nVERDICT: maybe-ish\nREASON: 不清楚。\n---END VERIFY---\n"
        )
        out = lvs.parse_verify_blocks(raw)
        self.assertEqual(out["x"]["verdict"], "uncertain")


class TestBuildVerifyPrompt(unittest.TestCase):
    def test_prompt_includes_full_page_content_not_truncated(self):
        lvs = _load_module()
        long_body = "详细内容 " * 400  # far longer than the 500-char summary cap
        finding = {
            "id": "lint-semantic-1",
            "detail": "[stale] 两页描述冲突",
            "affectedPages": ["concepts/a.md"],
        }
        pages = {"concepts/a.md": long_body}
        system, user = lvs.build_verify_prompt([finding], pages)
        self.assertIn(long_body, user)
        self.assertIn("lint-semantic-1", user)
        self.assertIn("VERIFY", system)


class TestConversationHandoffRoundtrip(unittest.TestCase):
    def test_pending_then_resume_writes_verified_findings(self):
        lvs = _load_module()
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            wiki = root / "wiki" / "concepts"
            wiki.mkdir(parents=True)
            (wiki / "a.md").write_text(
                _page("type: concept\ntitle: A", "# A\nfirst"), encoding="utf-8")
            (wiki / "b.md").write_text(
                _page("type: concept\ntitle: B", "# B\nsecond"), encoding="utf-8")

            runtime = root / ".llm-wiki"
            runtime.mkdir(parents=True)
            (runtime / "lint-semantic.json").write_text(json.dumps([
                {"id": "lint-semantic-1", "type": "semantic", "severity": "warning",
                 "page": "A vs B duplicate", "detail": "[stale] A 和 B 重复",
                 "affectedPages": ["concepts/a.md", "concepts/b.md"]},
                {"id": "lint-semantic-2", "type": "semantic", "severity": "info",
                 "page": "minor suggestion", "detail": "[suggestion] 加个链接",
                 "affectedPages": ["concepts/a.md"]},
            ]), encoding="utf-8")

            old_root = os.environ.get("IMPROVED_WIKI_ROOT")
            old_argv = sys.argv
            os.environ["IMPROVED_WIKI_ROOT"] = str(root)
            sys.argv = ["lint_verify_semantic.py"]
            try:
                self.assertEqual(lvs.main(), 101)
                conv_dir = runtime / "conversation" / "lint-verify"
                md_files = list(conv_dir.glob("*.md"))
                self.assertEqual(len(md_files), 1)
                md_files[0].with_suffix(".txt").write_text(
                    "---VERIFY id=lint-semantic-1---\n"
                    "VERDICT: refuted\n"
                    "REASON: A 和 B 实际讲的是不同的东西。\n"
                    "---END VERIFY---\n",
                    encoding="utf-8",
                )

                self.assertEqual(lvs.main(), 0)
                findings = json.loads(
                    (runtime / "lint-semantic.json").read_text("utf-8"))
                by_id = {f["id"]: f for f in findings}
                self.assertEqual(by_id["lint-semantic-1"]["verified"], "refuted")
                self.assertIn("不同的东西", by_id["lint-semantic-1"]["verify_reason"])
                # info-severity finding was never sent for verification
                self.assertNotIn("verified", by_id["lint-semantic-2"])
            finally:
                sys.argv = old_argv
                if old_root is None:
                    os.environ.pop("IMPROVED_WIKI_ROOT", None)
                else:
                    os.environ["IMPROVED_WIKI_ROOT"] = old_root

    def test_rerun_with_nothing_left_to_verify_returns_0_noop(self):
        lvs = _load_module()
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "wiki").mkdir(parents=True)
            runtime = root / ".llm-wiki"
            runtime.mkdir(parents=True)
            (runtime / "lint-semantic.json").write_text(json.dumps([
                {"id": "x", "type": "semantic", "severity": "info",
                 "page": "p", "detail": "[suggestion] s", "affectedPages": []},
            ]), encoding="utf-8")

            old_root = os.environ.get("IMPROVED_WIKI_ROOT")
            old_argv = sys.argv
            os.environ["IMPROVED_WIKI_ROOT"] = str(root)
            sys.argv = ["lint_verify_semantic.py"]
            try:
                self.assertEqual(lvs.main(), 0)
            finally:
                sys.argv = old_argv
                if old_root is None:
                    os.environ.pop("IMPROVED_WIKI_ROOT", None)
                else:
                    os.environ["IMPROVED_WIKI_ROOT"] = old_root


if __name__ == "__main__":
    unittest.main()
