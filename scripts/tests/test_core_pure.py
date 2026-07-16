"""Regression tests for pure functions in _core.py.

Stdlib `unittest` only — no pytest, no network, no LLM calls — so this runs
with the same `python3` the pipeline uses (NashSU "avoid pip install" rule).

Run:
    python3 -m unittest tests.test_core_pure   # from scripts/
    python3 scripts/tests/test_core_pure.py     # from skill root

Each test name maps to a historical bug in references/known-issues.md so a
regression is obvious from the failure label.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Make `_core` importable whether run from scripts/ or skill root.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _core  # noqa: E402
from _core import ConversationPending  # noqa: E402


class TestConversationPendingNotSwallowed(unittest.TestCase):
    """ConversationPending is a control-flow signal (pause for the calling
    agent), not an error. It must propagate through the broad ``except
    Exception`` retry/fallback blocks that wrap LLM calls in the stage
    modules — otherwise Stage 2 concept/entity generation silently produces
    0 blocks and the ingest never advances.

    Regression: ConversationPending subclassed Exception, so every
    ``except Exception`` around an LLM call swallowed it. Fix: subclass
    BaseException (like KeyboardInterrupt) so ``except Exception`` no longer
    catches it; the top-level ``except ConversationPending`` handler still does.
    """

    def test_broad_except_does_not_swallow_pending(self):
        def llm_call():
            raise ConversationPending()

        def stage_fn():
            try:
                llm_call()
            except Exception:
                return []  # HTTP-retry style swallow — must NOT catch Pending
            return ["block"]

        with self.assertRaises(ConversationPending):
            stage_fn()

    def test_explicit_except_pending_still_catches(self):
        caught = []
        try:
            raise ConversationPending()
        except ConversationPending:
            caught.append(True)
        self.assertEqual(caught, [True])


class TestComputeChunkTargetsHardCeil(unittest.TestCase):
    """2026-07-10: hard_ceil is now a parameter (default = ingest's own 64K
    _TARGET_TOKENS_HARD_CEIL) so other callers (wiki-lint-semantic.py) can
    request a different ceiling without touching the ingest-tuned constant."""

    def test_default_hard_ceil_matches_ingest_constant(self):
        target_tokens, _ = _core._compute_chunk_targets(0, 1_000_000)
        self.assertEqual(target_tokens, _core._TARGET_TOKENS_HARD_CEIL)

    def test_custom_hard_ceil_overrides_default(self):
        target_tokens, target_chars = _core._compute_chunk_targets(
            0, 1_000_000, hard_ceil=256_000)
        self.assertEqual(target_tokens, 256_000)
        self.assertEqual(target_chars, _core._TARGET_CHARS_HARD_CEIL)

    def test_small_context_still_respects_floor_under_custom_ceil(self):
        target_tokens, _ = _core._compute_chunk_targets(
            0, 20_000, hard_ceil=256_000)
        self.assertEqual(target_tokens, _core._TARGET_TOKENS_MIN)


class TestParseSimpleYaml(unittest.TestCase):
    """Fallback YAML parser (used when PyYAML missing or safe_load crashes)."""

    def test_scalar_and_list(self):
        text = "title: Buck Converter\nconcepts_found:\n  - PWM\n  - duty cycle\n"
        out = _core.parse_simple_yaml(text)
        self.assertEqual(out["title"], "Buck Converter")
        self.assertEqual(out["concepts_found"], ["PWM", "duty cycle"])

    def test_ignores_comments_and_blanks(self):
        out = _core.parse_simple_yaml("# header\n\nkey: val\n")
        self.assertEqual(out, {"key": "val"})

    def test_stage_2_2_analysis_yaml(self):
        # Regression for the real Stage 2.2 shape: the prior flat parser
        # collapsed each list item to a bare string, so concepts_found became
        # list[str] and Stage 2.4's `isinstance(c, dict)` filter dropped every
        # concept ("(none)"). One block exercises nested list-of-dicts,
        # inline-flow lists, and a block scalar together.
        text = (
            "tags: [a, b, c]\n"
            "concepts_found:\n"
            '  - name: "Robust Chaotic Map"\n'
            '    importance: "core"\n'
            "    key_details:\n"
            '      - "detail one"\n'
            '      - "detail two"\n'
            '  - name: "Chaos Theory"\n'
            '    importance: "supporting"\n'
            "digest: |\n"
            "  line one\n"
            "  line two\n"
        )
        out = _core.parse_simple_yaml(text)
        self.assertEqual(out["tags"], ["a", "b", "c"])
        self.assertEqual(out["digest"], "line one\nline two")
        concepts = out["concepts_found"]
        self.assertTrue(all(isinstance(c, dict) for c in concepts))
        self.assertEqual(concepts[0]["key_details"], ["detail one", "detail two"])
        self.assertEqual(concepts[1]["importance"], "supporting")

    def test_top_level_list_of_dicts(self):
        # Stage 3.4 review YAML is a top-level list; the prior parser returned
        # {} for it, so review always produced "0 review pages".
        text = (
            "- id: 1\n"
            '  affected_pages: ["concepts/x.md", "concepts/y.md"]\n'
            "  search_queries: []\n"
            "- id: 2\n"
            '  search_queries: ["q one", "q two"]\n'
        )
        out = _core.parse_simple_yaml(text)
        self.assertIsInstance(out, list)
        self.assertEqual(out[0]["affected_pages"], ["concepts/x.md", "concepts/y.md"])
        self.assertEqual(out[1]["search_queries"], ["q one", "q two"])


class TestParseYamlBlock(unittest.TestCase):
    """Extract first ```yaml fenced block; fall back on CJK-quote crash."""

    def test_extracts_fenced_block(self):
        resp = "preamble\n```yaml\ntitle: X\n```\ntrailer"
        self.assertEqual(_core.parse_yaml_block(resp)["title"], "X")

    def test_cjk_curly_quotes_do_not_crash(self):
        # known-issues.md: yaml.safe_load crashed on nested CJK curly quotes.
        resp = '```yaml\ntitle: 9.2 "正激"和"反激"\nconcepts_found:\n  - 正激\n```'
        out = _core.parse_yaml_block(resp)
        self.assertIn("concepts_found", out)
        self.assertEqual(out["concepts_found"], ["正激"])


class TestParseFileBlocks(unittest.TestCase):
    """Skill-specific ``---FILE:---`` parsing regressions NOT covered by the
    NashSU parity suite (test_nashsu_parity.py): hyphen→slash autocorrect,
    CJK slashes, and the legacy ``### File 1:`` header. The common cases
    (prefix strip, fence-aware END FILE, traversal drop) live there.
    """

    def test_hyphen_for_slash_autocorrect(self):
        # LLM writes concepts-pwm.md instead of concepts/pwm.md.
        resp = "---FILE:concepts-pwm.md---\nbody\n---END FILE---\n"
        self.assertEqual(_core.parse_file_blocks(resp)[0][0], "concepts/pwm.md")

    def test_slash_inside_cjk_slug_merged(self):
        # known-issues.md: [[热仿真(Cauer/Foster模型)]] → / inside the name.
        resp = "---FILE:wiki/concepts/热仿真(Cauer/Foster模型).md---\nbody\n---END FILE---\n"
        path = _core.parse_file_blocks(resp)[0][0]
        self.assertTrue(path.startswith("concepts/"))
        self.assertNotIn("/", path[len("concepts/"):])  # slug has no bare slash

    def test_legacy_header_format(self):
        resp = "### File 1: concepts/pwm.md\n# PWM\nbody\n"
        blocks = _core.parse_file_blocks(resp)
        self.assertEqual(blocks[0][0], "concepts/pwm.md")


class TestDetectTemplateType(unittest.TestCase):
    """raw/ layout → digest template mapping (Layouts A/B/C)."""

    RAW = Path("/proj/raw")

    def test_explicit_override_wins(self):
        self.assertEqual(
            _core.detect_template_type(self.RAW / "Book/x.pdf", self.RAW, "digest-paper"),
            "digest-paper")

    def test_layout_a_type_subdir_case_insensitive(self):
        self.assertEqual(
            _core.detect_template_type(self.RAW / "book/dsp/x.pdf", self.RAW, None),
            "digest-book")
        self.assertEqual(
            _core.detect_template_type(self.RAW / "PAPER/x.pdf", self.RAW, None),
            "digest-paper")

    def test_layout_b_sources_type_subdir(self):
        self.assertEqual(
            _core.detect_template_type(self.RAW / "sources/datasheet/x.pdf", self.RAW, None),
            "digest-datasheet")

    def test_layout_c_flat_defaults_to_book(self):
        self.assertEqual(
            _core.detect_template_type(self.RAW / "x.pdf", self.RAW, None),
            "digest-book")

    def test_unknown_folder_fuzzy_matches_nearest(self):
        # "Bok" (typo) is one edit from "Book".
        self.assertEqual(
            _core.detect_template_type(self.RAW / "Bok/x.pdf", self.RAW, None),
            "digest-book")


class TestIsQueryBridgeSource(unittest.TestCase):
    """wiki/queries/*.md deep-research pages (2026-07-16: ingested directly —
    no more raw/queries/ bridge copy, NashSU autoIngest path-agnostic parity)
    plus backward-compat recognition of pre-2026-07-16 raw/queries/*.md bridge
    copies still sitting in older wikis."""

    def setUp(self):
        self.config = _make_config(Path("/proj"))

    def test_wiki_queries_is_a_bridge_source(self):
        self.assertTrue(_core.is_query_bridge_source(
            self.config.wiki_dir / "queries/research-x.md", self.config))

    def test_wiki_queries_case_insensitive(self):
        self.assertTrue(_core.is_query_bridge_source(
            self.config.wiki_dir / "Queries/research-x.md", self.config))

    def test_legacy_raw_queries_bridge_copy_still_recognized(self):
        self.assertTrue(_core.is_query_bridge_source(
            self.config.raw_root / "queries/research-x.md", self.config))

    def test_book_is_not_a_bridge(self):
        self.assertFalse(_core.is_query_bridge_source(
            self.config.raw_root / "Book/x.pdf", self.config))

    def test_path_outside_wiki_and_raw_root_is_not_a_bridge(self):
        self.assertFalse(_core.is_query_bridge_source(
            Path("/other/queries/x.md"), self.config))


class TestStrDistance(unittest.TestCase):
    def test_levenshtein_basics(self):
        self.assertEqual(_core.str_distance("book", "book"), 0)
        self.assertEqual(_core.str_distance("bok", "book"), 1)
        self.assertEqual(_core.str_distance("", "abc"), 3)


def _make_config(tmp: Path) -> _core.Config:
    return _core.Config(
        wiki_root=tmp / "wiki", raw_root=tmp / "raw",
        wiki_dir=tmp / "wiki", runtime_dir=tmp / "rt",
        cache_path=tmp / "rt" / "ingest-cache.json",
        progress_dir=tmp / "rt" / "ingest-progress",
        extract_tmp_dir=tmp / "rt" / "extract-tmp",
        llm_model="m",
        caption_api_key="", caption_base_url="x",
        caption_model="c", chunk_overlap=3000,
        source_budget=100000, target_chars=60000, target_tokens=30000,
        max_tokens=8192, conversation_prefix="ab12cd34",
    )


class TestSaveProgressMergeWrite(unittest.TestCase):
    """save_progress merge-writes (not overwrites). Regression for the
    2026-06-25 stage-marker resume loop: the old overwrite-write meant a
    save_progress call that forgot to re-carry a cumulative key silently
    erased it. Stage-completion state now lives in stages.json, not in the
    artifact cache — so the cache must never carry a ``stage`` field either.
    """

    def test_merge_preserves_existing_keys(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            h = "deadbeef" * 8
            _core.save_progress(cfg, h, {"extracted_text": "abc", "extract_method": "mineru"})
            _core.save_progress(cfg, h, {"stage_1_2": {"count": 3}})
            p = _core.load_progress(cfg, h)
            self.assertEqual(p["extracted_text"], "abc")   # first write survives
            self.assertEqual(p["extract_method"], "mineru")
            self.assertEqual(p["stage_1_2"], {"count": 3}) # second write merged in

    def test_corrupted_cache_does_not_raise(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            h = "cafebabe" * 8
            pp = _core.progress_path(cfg, h)
            pp.parent.mkdir(parents=True, exist_ok=True)
            pp.write_text("{not valid json", encoding="utf-8")
            _core.save_progress(cfg, h, {"extracted_text": "x"})  # must not raise
            self.assertEqual(_core.load_progress(cfg, h)["extracted_text"], "x")

    def test_load_progress_corrupted_warns_and_returns_none(self):
        # Policy exception (2026-06-24): a corrupted state file is a loud
        # warning + reset (None), never a raised JSONDecodeError.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            h = "0badf00d" * 8
            pp = _core.progress_path(cfg, h)
            pp.parent.mkdir(parents=True, exist_ok=True)
            pp.write_text("{not valid json", encoding="utf-8")
            self.assertIsNone(_core.load_progress(cfg, h))

    def test_delete_progress_keys_truly_removes(self):
        # save_progress is merge-write and cannot express deletion —
        # delete_progress_keys must actually remove keys from storage.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            h = "feedc0de" * 8
            _core.save_progress(cfg, h, {"chunk_analyses": [1, 2], "extracted_text": "x"})
            _core.delete_progress_keys(cfg, h, ["chunk_analyses", "never-existed"])
            p = _core.load_progress(cfg, h)
            self.assertNotIn("chunk_analyses", p)
            self.assertEqual(p["extracted_text"], "x")  # untouched keys survive
            # A later merge-write must NOT resurrect the deleted key.
            _core.save_progress(cfg, h, {"stage_1_2": {"count": 1}})
            self.assertNotIn("chunk_analyses", _core.load_progress(cfg, h))

    def test_delete_progress_keys_noop_without_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            h = "abad1dea" * 8
            _core.delete_progress_keys(cfg, h, ["anything"])  # must not raise
            self.assertIsNone(_core.load_progress(cfg, h))
            self.assertFalse(_core.progress_path(cfg, h).exists())


class TestStageMarkers(unittest.TestCase):
    """stages.json is the single source of truth for stage completion. A
    marker set in one phase must be readable in another (cross-function
    resume), and the artifact cache must NOT carry a ``stage`` field.
    """

    def test_mark_and_check_marker(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            h = "1234abcd" * 8
            self.assertFalse(_core.is_stage_done(cfg, h, "stage_2_3_done"))
            _core.mark_stage_done(cfg, h, "stage_2_3_done")
            self.assertTrue(_core.is_stage_done(cfg, h, "stage_2_3_done"))
            # artifact cache stays stage-free
            _core.save_progress(cfg, h, {"chunk_analyses": []})
            self.assertNotIn("stage", _core.load_progress(cfg, h))

    def test_marker_payload_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            h = "5678ef90" * 8
            _core.mark_stage_done(cfg, h, "write_loop_done",
                                  payload={"files_written": ["concepts/a.md"]})
            self.assertEqual(
                _core.get_stage_payload(cfg, h, "write_loop_done"),
                {"files_written": ["concepts/a.md"]})


class TestListExistingSlugsExcludesLint(unittest.TestCase):
    """list_existing_slugs must exclude wiki/lint/** placeholder stubs.

    Regression for the 2026-06-25 RadarWiki finding: 230+ lint-generated stub
    pages (orphan-lint-*, broken-link-*, no-outlinks-lint-*) under wiki/lint/
    were rglob'd into the Stage 2.4 "Linkable pages" context, crowding out real
    pages and leaking lint-namespace slugs into the LLM's link context.
    """

    def test_lint_dir_excluded(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            (cfg.wiki_dir / "concepts").mkdir(parents=True)
            (cfg.wiki_dir / "lint").mkdir(parents=True)
            (cfg.wiki_dir / "concepts" / "real-concept.md").write_text("# x")
            (cfg.wiki_dir / "lint" / "orphan-lint-broken-link-foo.md").write_text("# stub")
            (cfg.wiki_dir / "lint" / "broken-link-overview.md").write_text("# stub")
            slugs = _core.list_existing_slugs(cfg)
            self.assertIn("real-concept", slugs)
            self.assertNotIn("orphan-lint-broken-link-foo", slugs)
            self.assertNotIn("broken-link-overview", slugs)

    def test_review_dir_still_excluded(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            (cfg.wiki_dir / "REVIEW").mkdir(parents=True)
            (cfg.wiki_dir / "concepts").mkdir(parents=True)
            (cfg.wiki_dir / "REVIEW" / "2026-01-01-suggestion.md").write_text("# r")
            (cfg.wiki_dir / "concepts" / "ok.md").write_text("# x")
            slugs = _core.list_existing_slugs(cfg)
            self.assertIn("ok", slugs)
            self.assertNotIn("2026-01-01-suggestion", slugs)


class TestSlugifyBracketHygiene(unittest.TestCase):
    """slugify must not leave interior parentheses/brackets in slugs.

    Regression for the 2026-06-25 Orin re-ingest finding: a concept named
    "Total Module Power (TMP)" produced the malformed slug
    "total-module-power-(tmp" (interior "(" kept, trailing ")" edge-stripped),
    polluting the wiki with parenthesis filenames and fragile wikilinks.
    """

    def test_parenthetical_abbreviation_stripped(self):
        self.assertEqual(_core.slugify("Total Module Power (TMP)"),
                         "total-module-power-tmp")
        self.assertEqual(_core.slugify("Jetson AGX Orin Industrial (JAOi)"),
                         "jetson-agx-orin-industrial-jaoi")
        self.assertEqual(
            _core.slugify("Software thermal management (DVFS throttling)"),
            "software-thermal-management-dvfs-throttling")

    def test_no_doubled_or_edge_hyphens(self):
        s = _core.slugify("S-Parameters (Two-Port Analysis)")
        self.assertEqual(s, "s-parameters-two-port-analysis")
        self.assertNotIn("--", s)
        self.assertFalse(s.startswith("-") or s.endswith("-"))

    def test_no_residual_brackets(self):
        for name in ["A [B] C", "Foo {bar}", "热电制冷 (Peltier-TEC)",
                     "loop gain (voltage/current injection)"]:
            s = _core.slugify(name)
            for ch in "()[]{}（）【】":
                self.assertNotIn(ch, s, f"{name!r} -> {s!r} kept {ch!r}")

    def test_underscore_section_slugs_unchanged(self):
        # Underscore-bearing section pages must keep their underscores.
        self.assertEqual(_core.slugify("01_Numeration_Systems"),
                         "01_numeration_systems")

    def test_colon_and_trailing_period_unchanged_behavior(self):
        self.assertEqual(_core.slugify("Volume III: Physics-Based Methods"),
                         "volume-iii-physics-based-methods")
        self.assertEqual(_core.slugify("Tron Future Tech Inc."),
                         "tron-future-tech-inc")

    def test_comma_ampersand_period_stripped(self):
        """Regression for 2026-06-25 Fardo re-ingest: commas/ampersands/periods
        were not stripped, producing slugs like "energy,-work,-and-power",
        "taylor-&-francis-ltd", "the-fairmont-press,-inc"."""
        self.assertEqual(_core.slugify("Energy, Work, and Power"),
                         "energy-work-and-power")
        self.assertEqual(_core.slugify("Taylor & Francis Ltd."),
                         "taylor-francis-ltd")
        self.assertEqual(_core.slugify("The Fairmont Press, Inc."),
                         "the-fairmont-press-inc")
        # No comma or ampersand survives in any slug.
        for name in ["A, B, C", "X & Y", "Foo, Inc.", "R&D Spending"]:
            s = _core.slugify(name)
            self.assertNotIn(",", s, f"{name!r} -> {s!r} kept comma")
            self.assertNotIn("&", s, f"{name!r} -> {s!r} kept ampersand")

    def test_cjk_titles_preserved_not_emptied(self):
        """Regression for the 2026-06-30 无源器件篇 re-ingest: the ASCII-only
        edge-strips (^[^a-z0-9]+ / [^a-z0-9]+$) deleted leading/trailing CJK, so
        pure-Chinese concept names collapsed to "" (colliding empty slugs) and
        mixed names like "电感DCR" lost their Chinese prefix → "dcr". NashSU
        wiki-filename.ts keeps Unicode letters across all scripts."""
        self.assertEqual(_core.slugify("贴片电阻"), "贴片电阻")
        self.assertEqual(_core.slugify("磁珠"), "磁珠")
        self.assertEqual(_core.slugify("上拉电阻阻值选择原则"), "上拉电阻阻值选择原则")
        # Mixed CJK + Latin keeps both, lowercasing the Latin run.
        self.assertEqual(_core.slugify("电感DCR"), "电感dcr")
        self.assertEqual(_core.slugify("MLCC的ESR"), "mlcc的esr")
        # Hyphenated Chinese keeps its internal hyphen.
        self.assertEqual(_core.slugify("金属膜电阻-碳膜电阻"), "金属膜电阻-碳膜电阻")
        # CJK + trailing parenthetical abbreviation: keep the Chinese, strip brackets.
        self.assertEqual(_core.slugify("热电制冷 (Peltier-TEC)"), "热电制冷-peltier-tec")
        # No pure-CJK title yields an empty slug.
        for name in ["贴片电阻", "磁珠", "电感", "上拉电阻"]:
            self.assertNotEqual(_core.slugify(name), "", f"{name!r} collapsed to empty")


if __name__ == "__main__":
    unittest.main(verbosity=2)
