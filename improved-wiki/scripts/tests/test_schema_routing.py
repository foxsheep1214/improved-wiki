"""Tests for NashSU schema-driven routing (schema.md → typed folders).

Stdlib unittest only — no network, no LLM.

Covers:
  - schema_folders() parsing (both `wiki/x` and `wiki/x/` table forms)
  - load_schema_md() root location + wiki/ back-compat + absent → ''
  - _schema_routing_block() emits guidance only when the schema adds EXTRA folders
  - the writer's valid-subdir set = base ∪ schema folders (so schema pages are
    accepted instead of dropped)
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _core  # noqa: E402
import _stage_2_4_generation as gen  # noqa: E402


def _make_config(tmp: Path) -> _core.Config:
    return _core.Config(
        wiki_root=tmp, raw_root=tmp / "raw",
        wiki_dir=tmp / "wiki", runtime_dir=tmp / "rt",
        cache_path=tmp / "rt" / "ingest-cache.json",
        progress_dir=tmp / "rt" / "ingest-progress",
        extract_tmp_dir=tmp / "rt" / "extract-tmp",
        llm_base_url="https://example.invalid", llm_model="m", llm_api_key="",
        llm_protocol="anthropic", caption_api_key="", caption_base_url="x",
        caption_model="c", chunk_size=60000, chunk_overlap=3000,
        source_budget=100000, target_chars=60000, target_tokens=30000,
        max_tokens=8192, conversation_prefix="ab12cd34",
    )


_SCHEMA_WITH_EXTRAS = """# Schema

## Page Types

| type | directory |
|------|-----------|
| source | wiki/sources |
| concept | wiki/concepts |
| methodology | wiki/methodology |
| person | wiki/people/ |
"""

_SCHEMA_BASE_ONLY = """# Schema
| source | wiki/sources |
| concept | wiki/concepts |
| entity | wiki/entities |
"""


class TestSchemaFolders(unittest.TestCase):
    def test_parses_both_table_forms(self):
        folders = _core.schema_folders(_SCHEMA_WITH_EXTRAS)
        self.assertIn("methodology", folders)
        self.assertIn("people", folders)       # trailing slash form
        self.assertIn("sources", folders)

    def test_empty_text_is_empty_set(self):
        self.assertEqual(_core.schema_folders(""), set())

    def test_extras_are_folders_beyond_base(self):
        extra = _core.schema_folders(_SCHEMA_WITH_EXTRAS) - _core.BASE_PAGE_DIRS
        self.assertEqual(extra, {"methodology", "people"})


class TestLoadSchemaMd(unittest.TestCase):
    def test_reads_root_then_wiki_fallback_then_empty(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            (cfg.wiki_dir).mkdir(parents=True, exist_ok=True)

            self.assertEqual(_core.load_schema_md(cfg), "")  # absent

            (cfg.wiki_dir / "schema.md").write_text("legacy wiki/ loc", encoding="utf-8")
            self.assertIn("legacy", _core.load_schema_md(cfg))  # back-compat

            (cfg.wiki_root / "schema.md").write_text("root loc", encoding="utf-8")
            self.assertIn("root", _core.load_schema_md(cfg))    # root wins


class TestSchemaRoutingBlock(unittest.TestCase):
    def test_block_emitted_only_with_extra_folders(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)

            # No schema → no block.
            self.assertEqual(gen._schema_routing_block(cfg), "")

            # Base-only schema → still no block (no noise on default projects).
            (cfg.wiki_root / "schema.md").write_text(_SCHEMA_BASE_ONLY, encoding="utf-8")
            self.assertEqual(gen._schema_routing_block(cfg), "")

            # Schema with extras → block names the extra folders.
            (cfg.wiki_root / "schema.md").write_text(_SCHEMA_WITH_EXTRAS, encoding="utf-8")
            block = gen._schema_routing_block(cfg)
            self.assertIn("Schema-Defined Folders", block)
            self.assertIn("methodology", block)
            self.assertIn("people", block)


class TestWriterAcceptsSchemaFolders(unittest.TestCase):
    def test_valid_subdirs_includes_schema_folders(self):
        valid = set(_core.BASE_PAGE_DIRS) | _core.schema_folders(_SCHEMA_WITH_EXTRAS)
        self.assertIn("methodology", valid)   # would have been dropped before
        self.assertIn("people", valid)
        self.assertIn("concepts", valid)      # base preserved


if __name__ == "__main__":
    unittest.main()
