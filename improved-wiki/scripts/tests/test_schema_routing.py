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
import _stage_3_write as wr  # noqa: E402


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
        # methodology is a BASE_PAGE_DIR (NashSU parity); only `people` is a
        # schema-declared folder beyond the base set.
        extra = _core.schema_folders(_SCHEMA_WITH_EXTRAS) - _core.BASE_PAGE_DIRS
        self.assertEqual(extra, {"people"})


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


class TestSchemaTypedCandidates(unittest.TestCase):
    """NashSU parity — Stage 2.2 flags schema-typed candidates, Stage 2.4
    surfaces them so generation routes a page into the candidate's folder."""

    def test_stage_2_2_block_only_with_extras(self):
        import _stage_2_analyze as ana
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            self.assertEqual(ana._stage_2_2_schema_types_block(cfg), "")
            (cfg.wiki_root / "schema.md").write_text(_SCHEMA_BASE_ONLY, encoding="utf-8")
            self.assertEqual(ana._stage_2_2_schema_types_block(cfg), "")
            (cfg.wiki_root / "schema.md").write_text(_SCHEMA_WITH_EXTRAS, encoding="utf-8")
            block = ana._stage_2_2_schema_types_block(cfg)
            self.assertIn("Schema-Defined Page Types", block)
            self.assertIn("people", block)
            self.assertIn("schema_typed_candidates", block)

    def test_stage_2_4_prompt_surfaces_candidates(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            (cfg.wiki_dir).mkdir(parents=True, exist_ok=True)
            (cfg.raw_root).mkdir(parents=True, exist_ok=True)
            chunk_analysis = {
                "concepts_found": [],
                "entities_found": [],
                "schema_typed_candidates": [
                    {"type": "person", "name": "Ada Lovelace",
                     "folder": "people", "rationale": "biography in this chunk"},
                ],
            }
            prompt = gen._stage_2_4_build_prompt(
                chunk_analysis, "chunk text", 0, cfg.raw_root / "book.pdf", cfg,
            )
            self.assertIn("Schema-typed pages found in this chunk", prompt)
            self.assertIn("people/ada-lovelace", prompt)
            self.assertIn("Ada Lovelace", prompt)

    def test_stage_2_4_prompt_none_when_no_candidates(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            (cfg.wiki_dir).mkdir(parents=True, exist_ok=True)
            (cfg.raw_root).mkdir(parents=True, exist_ok=True)
            prompt = gen._stage_2_4_build_prompt(
                {"concepts_found": [], "entities_found": []},
                "chunk text", 0, cfg.raw_root / "book.pdf", cfg,
            )
            self.assertIn("Schema-typed pages found in this chunk", prompt)
            self.assertIn("(none)", prompt)


class TestParseWikiSchemaRouting(unittest.TestCase):
    """NashSU parseWikiSchemaRouting parity — build a precise {type: dir} map."""

    def test_builds_type_to_bare_dir_map(self):
        r = _core.parse_wiki_schema_routing(_SCHEMA_WITH_EXTRAS)
        self.assertEqual(r["source"], "sources")
        self.assertEqual(r["methodology"], "methodology")
        self.assertEqual(r["person"], "people")   # trailing-slash form, bare

    def test_no_page_types_heading_is_empty(self):
        # _SCHEMA_BASE_ONLY has rows but NO "## Page Types" heading → no scope.
        self.assertEqual(_core.parse_wiki_schema_routing(_SCHEMA_BASE_ONLY), {})

    def test_empty_text_is_empty(self):
        self.assertEqual(_core.parse_wiki_schema_routing(""), {})

    def test_wiki_root_dir_maps_to_empty_string(self):
        schema = "## Page Types\n\n| type | directory |\n|--|--|\n| overview | wiki |\n"
        self.assertEqual(_core.parse_wiki_schema_routing(schema), {"overview": ""})

    def test_section_scoping_stops_at_next_heading(self):
        schema = (
            "## Page Types\n\n| concept | wiki/concepts |\n\n"
            "## Naming\n\n| notrow | wiki/should-not-parse |\n"
        )
        r = _core.parse_wiki_schema_routing(schema)
        self.assertIn("concept", r)
        self.assertNotIn("notrow", r)

    def test_rejects_path_traversal_dir(self):
        schema = ("## Page Types\n\n| concept | wiki/concepts |\n"
                  "| evil | wiki/../etc |\n| abs | wiki//x |\n")
        r = _core.parse_wiki_schema_routing(schema)
        self.assertIn("concept", r)
        self.assertNotIn("evil", r)   # '..' segment dropped
        self.assertNotIn("abs", r)    # leading-slash dir dropped


class TestSchemaRouteDir(unittest.TestCase):
    def test_schema_type_wins(self):
        r = {"person": "people"}
        self.assertEqual(_core.schema_route_dir("person", r), "people")

    def test_base_type_fallback(self):
        self.assertEqual(_core.schema_route_dir("concept", {}), "concepts")

    def test_unknown_type_is_none(self):
        self.assertIsNone(_core.schema_route_dir("widget", {}))

    def test_empty_type_is_none(self):
        self.assertIsNone(_core.schema_route_dir("", {"person": "people"}))


class TestStage31SchemaRoute(unittest.TestCase):
    """Write-time corrector: route by frontmatter type to its declared dir."""

    def _page(self, fm_type: str) -> str:
        return f"---\ntype: {fm_type}\ntitle: X\n---\n# X\n"

    def test_moves_concept_out_of_schema_folder(self):
        # type:concept written into a schema folder → routed to concepts/.
        out = wr._stage_3_1_schema_route("people/foo.md", self._page("concept"),
                                         {"person": "people"})
        self.assertEqual(out, "concepts/foo.md")

    def test_moves_schema_type_out_of_entities(self):
        out = wr._stage_3_1_schema_route("entities/ada.md", self._page("person"),
                                         {"person": "people"})
        self.assertEqual(out, "people/ada.md")

    def test_correct_page_unchanged(self):
        out = wr._stage_3_1_schema_route("people/ada.md", self._page("person"),
                                         {"person": "people"})
        self.assertEqual(out, "people/ada.md")

    def test_base_type_routed_without_schema(self):
        out = wr._stage_3_1_schema_route("entities/c.md", self._page("concept"), {})
        self.assertEqual(out, "concepts/c.md")

    def test_unknown_type_left_alone(self):
        out = wr._stage_3_1_schema_route("entities/x.md", self._page("widget"), {})
        self.assertEqual(out, "entities/x.md")

    def test_source_subdir_preserved(self):
        out = wr._stage_3_1_schema_route("sources/book/title.md",
                                         self._page("source"), {})
        self.assertEqual(out, "sources/book/title.md")

    def test_root_mapped_type_moved_to_root(self):
        # A schema type routed to the wiki root ("") must be moved there (no
        # leading slash), not treated as an unknown type and left in place.
        out = wr._stage_3_1_schema_route("concepts/foo.md", self._page("dashboard"),
                                         {"dashboard": ""})
        self.assertEqual(out, "foo.md")

    def test_root_mapped_type_already_at_root_unchanged(self):
        out = wr._stage_3_1_schema_route("foo.md", self._page("dashboard"),
                                         {"dashboard": ""})
        self.assertEqual(out, "foo.md")


if __name__ == "__main__":
    unittest.main()
