"""CLI selection for the standalone ingest validator."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
import json
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import validate_ingest as validator  # noqa: E402
from _ingest_events import append_ingest_event, rfc3339_from_ms  # noqa: E402


class TestValidateIngestCli(unittest.TestCase):
    def test_completion_history_cross_check_accepts_matching_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / ".llm-wiki"
            source_hash = "a" * 64
            completed_at_ms = 1_700_000_000_123
            completed_at = rfc3339_from_ms(completed_at_ms)
            event = {
                "schema_version": 1,
                "event": "ingest_completed",
                "run_id": "run-1",
                "source": "raw/Book/x.pdf",
                "source_hash": source_hash,
                "source_page": "wiki/sources/Book/x.md",
                "completed_at": completed_at,
                "completed_at_ms": completed_at_ms,
                "mode": "ingest",
            }
            append_ingest_event(SimpleNamespace(runtime_dir=runtime), event)
            progress = runtime / "ingest-progress"
            progress.mkdir(parents=True)
            progress.joinpath(f"{source_hash[:16]}.stages.json").write_text(
                json.dumps({
                    "ingested": completed_at_ms,
                    "ingested__payload": {"run_id": "run-1"},
                }),
                encoding="utf-8",
            )
            page = root / "wiki/sources/Book/x.md"
            page.parent.mkdir(parents=True)
            page.write_text(
                "---\ntype: source\ntitle: X\ncreated: 2023-01-01\n"
                "updated: 2023-01-01\n"
                f'first_ingested_at: "{completed_at}"\n'
                f'last_ingested_at: "{completed_at}"\n---\n\n# X\n',
                encoding="utf-8",
            )
            with patch.object(validator, "RUNTIME", runtime):
                ok, detail = validator._validate_completion_history(
                    {"key": "Book/x.pdf", "hash": source_hash}, page
                )
            self.assertTrue(ok, detail)

    def test_explicit_root_source_and_cache_key_are_parsed(self):
        with tempfile.TemporaryDirectory() as directory:
            args = validator._parse_args([
                "--root", directory,
                "--source", "Book Name - 2026 - Author",
                "--cache-key", "Book/exact.pdf",
            ])

        self.assertEqual(args.root, directory)
        self.assertEqual(args.source, "Book Name - 2026 - Author")
        self.assertEqual(args.cache_key, "Book/exact.pdf")

    def test_source_is_required_when_environment_is_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            with redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    validator._parse_args(["--root", "/tmp/wiki"])

        self.assertEqual(raised.exception.code, 2)

    def test_runtime_paths_follow_cli_root_instead_of_import_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".llm-wiki").mkdir()
            validator._configure_runtime(
                root,
                "Selected Source",
                "Book/Selected Source.pdf",
            )

            self.assertEqual(validator.PROJECT_ROOT, root.resolve())
            self.assertEqual(validator.WIKI, root.resolve() / "wiki")
            self.assertEqual(validator.RUNTIME, root.resolve() / ".llm-wiki")
            self.assertEqual(validator.SOURCE_SLUG, "Selected Source")
            self.assertEqual(
                validator.CACHE_KEY,
                "Book/Selected Source.pdf",
            )


if __name__ == "__main__":
    unittest.main()
