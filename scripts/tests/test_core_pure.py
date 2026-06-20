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


class TestIsSafeIngestPath(unittest.TestCase):
    """NashSU isSafeIngestPath parity — path traversal / garbage-slug guard."""

    def test_accepts_normal_wiki_paths(self):
        for p in ("concepts/buck-converter.md",
                  "entities/ti-tps54560.md",
                  "sources/book/emc-design.md"):
            self.assertTrue(_core.is_safe_ingest_path(p), p)

    def test_rejects_parent_traversal(self):
        self.assertFalse(_core.is_safe_ingest_path("../etc/passwd.md"))
        self.assertFalse(_core.is_safe_ingest_path("concepts/../../x.md"))

    def test_rejects_absolute_and_windows_drive(self):
        self.assertFalse(_core.is_safe_ingest_path("/etc/passwd.md"))
        self.assertFalse(_core.is_safe_ingest_path("C:/Windows/x.md"))
        self.assertFalse(_core.is_safe_ingest_path("\\\\server\\share.md"))

    def test_rejects_windows_reserved_and_illegal_chars(self):
        self.assertFalse(_core.is_safe_ingest_path("concepts/con.md"))
        self.assertFalse(_core.is_safe_ingest_path("concepts/com1.md"))
        self.assertFalse(_core.is_safe_ingest_path('concepts/a"b.md'))

    def test_rejects_garbage_llm_slugs(self):
        # Empty / null / parenthesized-only stems from malformed LLM titles.
        for bad in ("concepts/.md", "concepts/none.md", "concepts/null.md",
                    "concepts/(unknown).md"):
            self.assertFalse(_core.is_safe_ingest_path(bad), bad)

    def test_rejects_segment_trailing_space_or_dot(self):
        self.assertFalse(_core.is_safe_ingest_path("concepts /x.md"))
        self.assertFalse(_core.is_safe_ingest_path("concepts./x.md"))


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
    """NashSU ---FILE:--- parsing: prefix strip, slash/hyphen correction, fences."""

    def test_nashsu_format_with_wiki_prefix(self):
        resp = "---FILE:wiki/concepts/pwm.md---\n# PWM\nbody\n---END FILE---\n"
        blocks = _core.parse_file_blocks(resp)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0][0], "concepts/pwm.md")
        self.assertIn("# PWM", blocks[0][1])

    def test_missing_wiki_prefix_still_parsed(self):
        resp = "---FILE:concepts/pwm.md---\nbody\n---END FILE---\n"
        self.assertEqual(_core.parse_file_blocks(resp)[0][0], "concepts/pwm.md")

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

    def test_end_file_inside_code_fence_does_not_close_block(self):
        resp = (
            "---FILE:wiki/concepts/x.md---\n"
            "```\n---END FILE---\n```\n"   # fenced — must NOT close the block
            "real body\n"
            "---END FILE---\n"
        )
        blocks = _core.parse_file_blocks(resp)
        self.assertEqual(len(blocks), 1)
        self.assertIn("real body", blocks[0][1])

    def test_legacy_header_format(self):
        resp = "### File 1: concepts/pwm.md\n# PWM\nbody\n"
        blocks = _core.parse_file_blocks(resp)
        self.assertEqual(blocks[0][0], "concepts/pwm.md")

    def test_unsafe_path_block_dropped(self):
        resp = "---FILE:wiki/../escape.md---\nbody\n---END FILE---\n"
        self.assertEqual(_core.parse_file_blocks(resp), [])


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


class TestStrDistance(unittest.TestCase):
    def test_levenshtein_basics(self):
        self.assertEqual(_core.str_distance("book", "book"), 0)
        self.assertEqual(_core.str_distance("bok", "book"), 1)
        self.assertEqual(_core.str_distance("", "abc"), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
