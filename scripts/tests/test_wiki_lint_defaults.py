"""Regression tests for wiki-lint.sh's user-selected default workflow."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "wiki-lint.sh"
_SCRIPT = _SCRIPT_PATH.read_text(encoding="utf-8")


class TestWikiLintDefaults(unittest.TestCase):
    def assert_default(self, name: str, value: str) -> None:
        self.assertRegex(
            _SCRIPT,
            rf"(?m)^{re.escape(name)}={re.escape(value)}(?:\s|$)",
        )

    def test_structural_and_semantic_scans_remain_default(self):
        self.assert_default("SEMANTIC", "true")

    def test_five_maintenance_actions_are_default_on(self):
        for name in (
            "EMIT_REVIEW",
            "AUTO_FIX",
            "FIX_LINKS",
            "SWEEP",
            "DEDUP",
        ):
            with self.subTest(name=name):
                self.assert_default(name, "true")

    def test_delete_orphans_defaults_to_confirmation_checkpoint(self):
        self.assert_default("DELETE_ORPHANS", "ask")
        self.assertIn("DELETE_ORPHANS_CONFIRMATION_REQUIRED", _SCRIPT)
        self.assertIn("exit 102", _SCRIPT)

    def test_override_and_continuation_flags_are_supported(self):
        for flag in (
            "--emit-review",
            "--fix",
            "--fix-links",
            "--sweep",
            "--dedup",
            "--delete-orphans",
            "--no-delete-orphans",
            "--diagnostic-only",
            "--structural-only",
            "--delete-orphans-only",
            "--reset-lint-run",
        ):
            with self.subTest(flag=flag):
                self.assertIn(flag, _SCRIPT)

    def test_plain_scan_does_not_migrate_legacy_wiki_lint_pages(self):
        self.assertNotIn('mv "$WIKI_DIR/lint"', _SCRIPT)

    def test_exit_101_stages_use_one_durable_logical_run(self):
        self.assertIn("_lint_run_state.py", _SCRIPT)
        self.assertIn('lint_stage_done "semantic"', _SCRIPT)
        self.assertIn('lint_mark_done "semantic"', _SCRIPT)
        self.assertIn('lint_stage_done "sweep"', _SCRIPT)
        self.assertIn('--run-id "$LINT_RUN_ID"', _SCRIPT)

    def test_graph_is_not_part_of_lint(self):
        self.assertNotIn("graph.py", _SCRIPT)

    def test_noninteractive_checkpoint_waits_then_confirmed_continuation_runs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            page = root / "wiki" / "concepts" / "lonely.md"
            page.parent.mkdir(parents=True)
            page.write_text(
                "---\ntype: concept\ntitle: Lonely\n---\n\n# Lonely\nNo links.",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["IMPROVED_WIKI_ROOT"] = str(root)
            diagnostic = subprocess.run(
                [
                    "/bin/bash",
                    str(_SCRIPT_PATH),
                    "--diagnostic-only",
                    "--no-semantic",
                ],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(
                diagnostic.returncode,
                0,
                msg=diagnostic.stdout + diagnostic.stderr,
            )
            self.assertFalse((root / "wiki" / "REVIEW").exists())

            disable_preceding_actions = [
                "--no-semantic",
                "--no-emit-review",
                "--no-fix",
                "--no-fix-links",
                "--no-sweep",
                "--no-dedup",
            ]
            pending = subprocess.run(
                ["/bin/bash", str(_SCRIPT_PATH), *disable_preceding_actions],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(pending.returncode, 102)
            self.assertIn(
                "DELETE_ORPHANS_CONFIRMATION_REQUIRED",
                pending.stdout + pending.stderr,
            )
            self.assertTrue(page.exists())

            confirmed = subprocess.run(
                ["/bin/bash", str(_SCRIPT_PATH), "--delete-orphans-only"],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(
                confirmed.returncode,
                0,
                msg=confirmed.stdout + confirmed.stderr,
            )
            self.assertIn("Delete-orphans", confirmed.stdout + confirmed.stderr)
            self.assertTrue(page.exists(), "preview mode must not delete the page")



def _write(root: Path, rel: str, text: str) -> Path:
    path = root / "wiki" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _linked_pair(root: Path) -> None:
    _write(root, "concepts/a.md", "---\ntype: concept\ntitle: A\n---\n\nSee [[concepts/b]].\n")
    _write(root, "concepts/b.md", "---\ntype: concept\ntitle: B\n---\n\nSee [[concepts/a]].\n")


class TestWikiLintRunOutcomes(unittest.TestCase):
    ONLY_SEMANTIC = ["--no-fix", "--no-fix-links", "--no-sweep", "--no-dedup",
                     "--no-delete-orphans"]

    def run_lint(self, root: Path, *args: str, env_extra=None):
        env = os.environ.copy()
        env["IMPROVED_WIKI_ROOT"] = str(root)
        env.update(env_extra or {})
        return subprocess.run(
            ["/bin/bash", str(_SCRIPT_PATH), *args], cwd=root, env=env,
            text=True, capture_output=True, timeout=60, check=False)

    def test_failed_stage_exits_2_instead_of_reaching_the_checkpoint(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _linked_pair(root)
            _write(root, "concepts/lonely.md", "---\ntype: concept\n---\n\nalone\n")
            runtime = root / ".llm-wiki"
            runtime.mkdir()
            (runtime / "conversation").write_text("not a dir", encoding="utf-8")
            result = self.run_lint(root, "--no-fix", "--no-fix-links",
                                   "--no-sweep", "--no-dedup")
            out = result.stdout + result.stderr
            self.assertEqual(result.returncode, 2, out)
            self.assertIn("Failed stage(s): semantic", out)
            self.assertNotIn("DELETE_ORPHANS_CONFIRMATION_REQUIRED", out)
            self.assertTrue((runtime / "lint-run-state.json").exists())

    def test_no_orphans_needs_no_confirmation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _linked_pair(root)
            result = self.run_lint(root, "--no-semantic", "--no-emit-review",
                                   "--no-fix", "--no-fix-links", "--no-sweep",
                                   "--no-dedup")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("No orphan pages remain", result.stdout)

    def test_structural_run_keeps_semantic_lint_pages(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _linked_pair(root)
            _write(root, "concepts/lonely.md", "---\ntype: concept\n---\n\nalone\n")
            lint_dir = root / ".llm-wiki" / "lint"
            lint_dir.mkdir(parents=True)
            semantic_page = lint_dir / "semantic-contradiction-x.md"
            semantic_page.write_text("kept", encoding="utf-8")
            (lint_dir / "orphan-stale.md").write_text("old", encoding="utf-8")
            result = self.run_lint(root, "--structural-only")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(semantic_page.exists())
            self.assertFalse((lint_dir / "orphan-stale.md").exists())
            self.assertTrue(list(lint_dir.glob("orphan-concepts-lonely*.md")))

    def test_diagnostic_run_leaves_maintenance_checkpoint_alone(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _linked_pair(root)
            state = root / ".llm-wiki" / "lint-run-state.json"
            state.parent.mkdir(parents=True)
            scripts = _SCRIPT_PATH.parent
            subprocess.run([sys.executable, str(scripts / "_lint_run_state.py"),
                            "begin", str(state)], check=True, capture_output=True)
            subprocess.run([sys.executable, str(scripts / "_lint_run_state.py"),
                            "mark-done", str(state), "semantic"], check=True)
            before = state.read_text(encoding="utf-8")
            result = self.run_lint(root, "--diagnostic-only", "--no-semantic")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(state.read_text(encoding="utf-8"), before)

    def test_findings_are_refreshed_after_maintenance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _linked_pair(root)
            _write(root, "concepts/c.md", "# C\nSee [[concepts/a]].\n")
            result = self.run_lint(root, "--no-semantic", "--no-emit-review",
                                   "--no-fix-links", "--no-sweep", "--no-dedup",
                                   "--no-delete-orphans")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("After maintenance:", result.stdout)
            cache = json.loads((root / ".llm-wiki" / "lint-cache.json").read_text("utf-8"))
            self.assertNotIn("missing-frontmatter", {f["type"] for f in cache})
            lint_dir = root / ".llm-wiki" / "lint"
            self.assertFalse(list(lint_dir.glob("missing-frontmatter-*.md")))

    def test_fix_links_clears_dangling_related_entries(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _linked_pair(root)
            page = _write(root, "concepts/c.md",
                          '---\ntype: concept\ntitle: Gamma Page\n'
                          'related: ["concepts/a", "concepts/never-written"]\n---\n\n'
                          'See [[concepts/a]].\n')
            result = self.run_lint(root, "--no-semantic", "--no-emit-review", "--no-fix",
                                   "--no-sweep", "--no-dedup", "--no-delete-orphans")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('related: ["concepts/a"]', page.read_text(encoding="utf-8"))
            cache = json.loads((root / ".llm-wiki/lint-cache.json").read_text("utf-8"))
            self.assertNotIn("broken-related", {f["type"] for f in cache})

    def test_auto_fix_takes_page_type_from_schema(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "schema.md").write_text(
                "# Schema\n\n## Page Types\n\n| Type | Directory | Purpose |\n"
                "|---|---|---|\n| device | wiki/devices/ | Devices |\n",
                encoding="utf-8")
            page = _write(root, "devices/amp.md", "# Amp\n")
            result = self.run_lint(root, "--no-semantic", "--no-emit-review",
                                   "--no-fix-links", "--no-sweep", "--no-dedup",
                                   "--no-delete-orphans")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("type: device\n", page.read_text(encoding="utf-8"))

    def test_improved_wiki_python_runs_every_python_step(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _linked_pair(root)
            log = root / "python-calls.log"
            wrapper = root / "py"
            wrapper.write_text(
                f'#!/bin/bash\necho "$1" >> "{log}"\nexec "{sys.executable}" "$@"\n',
                encoding="utf-8")
            wrapper.chmod(0o755)
            result = self.run_lint(root, "--structural-only",
                                   env_extra={"IMPROVED_WIKI_PYTHON": str(wrapper)})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            calls = log.read_text(encoding="utf-8")
            self.assertIn("_maintenance_lock.py", calls)
            self.assertIn("lint-", calls)          # the structural scan script
            self.assertIn("-c", calls.split())      # summary / lint-page helpers

    def test_refuted_semantic_warning_is_not_routed_to_review(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _linked_pair(root)
            conv = root / ".llm-wiki" / "conversation"

            def answer(stage: str, text: str) -> None:
                pending = [p for p in (conv / stage).glob("*.md")
                           if not p.with_suffix(".txt").exists()]
                self.assertEqual(len(pending), 1, stage)
                pending[0].with_suffix(".txt").write_text(text, encoding="utf-8")

            first = self.run_lint(root, *self.ONLY_SEMANTIC)
            self.assertEqual(first.returncode, 101, first.stdout + first.stderr)
            answer("semantic-lint",
                   "---LINT: contradiction | warning | Real clash---\n"
                   "A and B disagree.\nPAGES: concepts/a.md, concepts/b.md\n"
                   "---END LINT---\n"
                   "---LINT: contradiction | warning | False alarm---\n"
                   "A and B disagree again.\nPAGES: concepts/a.md\n"
                   "---END LINT---\n")
            second = self.run_lint(root, *self.ONLY_SEMANTIC)
            self.assertEqual(second.returncode, 101, second.stdout + second.stderr)
            self.assertFalse((root / "wiki" / "REVIEW").exists())
            answer("lint-verify",
                   "---VERIFY id=lint-semantic-0---\nVERDICT: confirmed\n"
                   "REASON: 两页确实矛盾\n---END VERIFY---\n"
                   "---VERIFY id=lint-semantic-1---\nVERDICT: refuted\n"
                   "REASON: 并不矛盾\n---END VERIFY---\n")
            third = self.run_lint(root, *self.ONLY_SEMANTIC)
            self.assertEqual(third.returncode, 0, third.stdout + third.stderr)
            items = list((root / "wiki" / "REVIEW" / "contradiction").glob("*.md"))
            self.assertEqual(len(items), 1)
            self.assertIn("Real clash", items[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
