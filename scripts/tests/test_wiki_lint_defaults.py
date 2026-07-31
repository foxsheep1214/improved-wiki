"""Regression tests for wiki-lint.sh's user-selected default workflow."""
from __future__ import annotations

import os
import re
import subprocess
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


if __name__ == "__main__":
    unittest.main()
