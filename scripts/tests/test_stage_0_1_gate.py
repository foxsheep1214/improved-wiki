"""Stage 0.1 per-file naming gate — wired into ingest.py Phase 0 (2026-07-08).

Previously `normalize_raw_names.py --check` was agent-discipline only (the one
gate not enforced by code). `stage_0_1_check_file` closes that: _do_prepare
calls it before the Stage 0.2 dedup check and raises on violations.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from normalize_raw_names import stage_0_1_check_file


SCHEMA = """# Project schema

```yaml
forbidden_chars:
  - ","
rules:
  Book:
    pattern: Title - Year - Author
    min_parts: 3
    year_field: 1
```
"""


def _mk_project(tmp: Path, schema: str | None = SCHEMA) -> Path:
    if schema is not None:
        (tmp / "schema.md").write_text(schema, encoding="utf-8")
    (tmp / "raw" / "Book").mkdir(parents=True)
    return tmp


def _touch(project: Path, rel: str) -> Path:
    f = project / "raw" / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"%PDF-1.4 gate test" if rel.endswith(".pdf") else b"md")
    return f


class TestStage01Gate(unittest.TestCase):
    def test_missing_schema_raises(self):
        with tempfile.TemporaryDirectory() as d:
            project = _mk_project(Path(d), schema=None)
            f = _touch(project, "Book/Radar Handbook - 2008 - Skolnik.pdf")
            with self.assertRaises(RuntimeError):
                stage_0_1_check_file(f, project)

    def test_schema_without_rules_block_raises(self):
        with tempfile.TemporaryDirectory() as d:
            project = _mk_project(Path(d), schema="# schema, no yaml block\n")
            f = _touch(project, "Book/Radar Handbook - 2008 - Skolnik.pdf")
            with self.assertRaises(RuntimeError):
                stage_0_1_check_file(f, project)

    def test_compliant_pdf_passes(self):
        with tempfile.TemporaryDirectory() as d:
            project = _mk_project(Path(d))
            f = _touch(project, "Book/Radar Handbook - 2008 - Skolnik.pdf")
            self.assertEqual(stage_0_1_check_file(f, project), [])

    def test_violating_pdf_reports_errors(self):
        with tempfile.TemporaryDirectory() as d:
            project = _mk_project(Path(d))
            f = _touch(project, "Book/badname.pdf")  # 1 part < min_parts 3
            errors = stage_0_1_check_file(f, project)
            self.assertTrue(errors)
            self.assertIn("格式不符合", errors[0])

    def test_forbidden_comma_reports_error(self):
        with tempfile.TemporaryDirectory() as d:
            project = _mk_project(Path(d))
            f = _touch(project, "Book/Radar, Handbook - 2008 - Skolnik.pdf")
            errors = stage_0_1_check_file(f, project)
            self.assertTrue(any("禁用字符" in e for e in errors))

    def test_non_pdf_not_checked(self):
        # deep-research bridge copies (raw/queries/*.md) must never be blocked.
        with tempfile.TemporaryDirectory() as d:
            project = _mk_project(Path(d))
            f = _touch(project, "queries/some-research-topic.md")
            self.assertEqual(stage_0_1_check_file(f, project), [])

    def test_uncovered_folder_passes(self):
        # Mirrors stage_0_1_scan_raw scope: only folders declared in rules.
        with tempfile.TemporaryDirectory() as d:
            project = _mk_project(Path(d))
            f = _touch(project, "Misc/whatever.pdf")
            self.assertEqual(stage_0_1_check_file(f, project), [])


if __name__ == "__main__":
    unittest.main()
