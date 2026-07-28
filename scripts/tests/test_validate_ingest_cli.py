"""CLI selection for the standalone ingest validator."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import validate_ingest as validator  # noqa: E402


class TestValidateIngestCli(unittest.TestCase):
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
