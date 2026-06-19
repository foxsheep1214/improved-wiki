"""NashSU parity tests — ported from llm_wiki-0.4.25/src/lib/ingest-parse.test.ts.

Verifies the skill's `_core.parse_file_blocks` / `is_safe_ingest_path` against the
black-box cases the NashSU app tests. Stdlib `unittest` only.

Two architectural differences from NashSU are by design, NOT gaps:
  1. NashSU `isSafeIngestPath` validates the FULL path incl. the `wiki/` prefix
     and rejects anything without it. The skill strips `wiki/` in
     `parse_file_blocks` first, then validates the relative path
     (e.g. "concepts/foo.md"). So the "must start with wiki/" cases are ported
     against the stripped form.
  2. NashSU `parseFileBlocks` returns `{blocks, warnings}` and KEEPS the `wiki/`
     prefix on `block.path`. The skill returns `list[(path, content)]` with the
     `wiki/` prefix stripped, and surfaces warnings via stderr prints rather
     than a `warnings` array.

Parser gaps closed 2026-06-19:
  G1–G4 (tolerant markers): case-insensitive + whitespace-tolerant markers,
  CommonMark fence-length tracking (TestParseFileBlocksTolerantMarkers).
  G5–G8 (stream warnings + coverage, this edit): H2 stream-truncation warnings,
  H6 empty-path warnings, trailing-whitespace opener, hyphenated-path test.

Run:  python3 scripts/tests/test_nashsu_parity.py
"""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _core  # noqa: E402


def paths(text: str) -> list[str]:
    return [p for p, _ in _core.parse_file_blocks(text)]


@contextmanager
def capture_parse_stdout():
    """Capture stdout during a parse_file_blocks call to inspect warnings.

    The skill surfaces warnings via print() (stdout). This context manager
    captures stdout so tests can assert on warning content (NashSU
    ``warnings[]`` array parity).
    """
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        yield buf
    finally:
        sys.stdout = old_stdout


class TestIsSafeIngestPathParity(unittest.TestCase):
    """Ported from ingest-parse.test.ts "isSafeIngestPath" (stripped-path form)."""

    def test_accepts_canonical_wiki_paths(self):
        for p in ("concepts/foo.md", "index.md",
                  "sources/some-paper.md", "entities/transformer.md"):
            self.assertTrue(_core.is_safe_ingest_path(p), p)

    def test_rejects_empty_or_whitespace(self):
        for p in ("", "   ", "\t\n"):
            self.assertFalse(_core.is_safe_ingest_path(p), repr(p))

    def test_rejects_absolute_posix(self):
        self.assertFalse(_core.is_safe_ingest_path("/etc/passwd"))
        self.assertFalse(_core.is_safe_ingest_path("/wiki/foo.md"))

    def test_rejects_windows_absolute_and_unc(self):
        self.assertFalse(_core.is_safe_ingest_path("C:/Windows/System32/config"))
        self.assertFalse(_core.is_safe_ingest_path("c:\\Users\\victim\\evil.txt"))
        self.assertFalse(_core.is_safe_ingest_path("\\Users\\victim\\evil.txt"))
        self.assertFalse(_core.is_safe_ingest_path("\\\\server\\share\\file.md"))

    def test_rejects_dotdot_segment_every_position(self):
        for p in ("concepts/../../etc/passwd", "..",
                  "concepts\\..\\etc\\passwd"):
            self.assertFalse(_core.is_safe_ingest_path(p), p)

    def test_allows_double_dot_inside_filename(self):
        # ".." is a path SEGMENT, not a substring — version-suffix names are legal.
        self.assertTrue(_core.is_safe_ingest_path("concepts/qwen-2.5..notes.md"))
        self.assertTrue(_core.is_safe_ingest_path("concepts/foo..bar.md"))

    def test_rejects_nul_and_control_chars(self):
        self.assertFalse(_core.is_safe_ingest_path("concepts/foo\x00.md"))
        self.assertFalse(_core.is_safe_ingest_path("concepts/foo\nbar.md"))
        self.assertFalse(_core.is_safe_ingest_path("\x07alarm.md"))

    def test_rejects_windows_invalid_chars(self):
        for p in ("concepts/Article: Why It Matters.md",
                  'concepts/quoted"name.md', "concepts/a|b.md",
                  "concepts/a?b.md", "concepts/a*b.md", "concepts/a<b>.md"):
            self.assertFalse(_core.is_safe_ingest_path(p), p)

    def test_rejects_reserved_device_names_with_extensions(self):
        # The hardening added in this session — NashSU parity.
        for p in ("concepts/con.md", "concepts/NUL.pdf.md",
                  "concepts/com1.md", "concepts/LPT9.notes.md"):
            self.assertFalse(_core.is_safe_ingest_path(p), p)
        self.assertTrue(_core.is_safe_ingest_path("concepts/auxiliary.md"))

    def test_segment_trailing_space_or_dot(self):
        self.assertTrue(_core.is_safe_ingest_path("concepts/topic .md"))   # space inside, not at end
        self.assertFalse(_core.is_safe_ingest_path("concepts/topic."))
        self.assertFalse(_core.is_safe_ingest_path("concepts/topic "))
        self.assertFalse(_core.is_safe_ingest_path("concepts/folder./topic.md"))
        self.assertFalse(_core.is_safe_ingest_path("concepts/folder /topic.md"))


class TestParseFileBlocksParity(unittest.TestCase):
    """Ported from ingest-parse.test.ts (skill strips the wiki/ prefix)."""

    def test_single_well_formed_block(self):
        text = "---FILE: wiki/entities/qwen.md---\n# Qwen\n---END FILE---\n"
        b = _core.parse_file_blocks(text)
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0][0], "entities/qwen.md")
        self.assertIn("# Qwen", b[0][1])

    def test_multiple_consecutive_blocks(self):
        text = ("---FILE: wiki/entities/qwen.md---\n# Qwen\n---END FILE---\n\n"
                "---FILE: wiki/concepts/moe.md---\n# MoE\n---END FILE---\n")
        self.assertEqual(paths(text), ["entities/qwen.md", "concepts/moe.md"])

    def test_ignores_preamble_prose(self):
        text = "Here are the pages:\n\n---FILE: wiki/concepts/foo.md---\nbody\n---END FILE---\n"
        self.assertEqual(paths(text), ["concepts/foo.md"])

    def test_crlf_line_endings(self):
        text = "\r\n".join([
            "---FILE: wiki/entities/qwen.md---", "# Qwen", "---END FILE---", "",
            "---FILE: wiki/concepts/moe.md---", "# MoE", "---END FILE---",
        ])
        b = _core.parse_file_blocks(text)
        self.assertEqual([p for p, _ in b], ["entities/qwen.md", "concepts/moe.md"])
        for _, content in b:
            self.assertNotIn("\r", content)  # CRLF normalized to LF

    def test_mixed_crlf_body(self):
        text = "---FILE: wiki/concepts/foo.md---\nline1\r\nline2\r\n---END FILE---"
        content = _core.parse_file_blocks(text)[0][1]
        self.assertEqual(content.strip(), "line1\nline2")

    def test_end_file_in_prose_list_item_does_not_close(self):
        text = "\n".join([
            "---FILE: wiki/concepts/foo.md---",
            "Not to be written:",
            "- `---END FILE---` in backticks (this is prose)",
            "real content continues",
            "---END FILE---",
        ])
        b = _core.parse_file_blocks(text)
        self.assertEqual(len(b), 1)
        self.assertIn("real content continues", b[0][1])

    def test_fence_aware_end_file_inside_code_block(self):
        text = "\n".join([
            "---FILE: wiki/concepts/ingest-format.md---", "# Ingest Format", "",
            "```plaintext", "---FILE: wiki/path/to/page.md---", "body content",
            "---END FILE---", "```", "", "More explanation after the example.",
            "---END FILE---",
        ])
        b = _core.parse_file_blocks(text)
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0][0], "concepts/ingest-format.md")
        self.assertIn("```plaintext", b[0][1])
        self.assertIn("More explanation after the example.", b[0][1])

    def test_multiple_fenced_blocks_one_page(self):
        text = "\n".join([
            "---FILE: wiki/concepts/foo.md---",
            "```", "---END FILE---", "```", "", "prose", "",
            "~~~", "---END FILE---", "~~~", "", "more prose",
            "---END FILE---",
        ])
        b = _core.parse_file_blocks(text)
        self.assertEqual(len(b), 1)
        self.assertIn("more prose", b[0][1])

    # ── path-traversal guard, end-to-end (blocks-level; skill prints, no warnings array) ──

    def test_drops_traversal_keeps_legit(self):
        text = "\n".join([
            "---FILE: wiki/concepts/legit.md---", "Real page.", "---END FILE---",
            "---FILE: ../../etc/passwd---", "attacker:x:0:0::/root:/bin/bash", "---END FILE---",
        ])
        self.assertEqual(paths(text), ["concepts/legit.md"])

    def test_drops_absolute_path_block(self):
        text = "---FILE: /etc/passwd---\nevil\n---END FILE---\n"
        self.assertEqual(_core.parse_file_blocks(text), [])

    def test_mixing_safe_and_unsafe_keeps_only_safe(self):
        text = "\n".join([
            "---FILE: wiki/concepts/topic-a.md---", "topic A page", "---END FILE---",
            "---FILE: ../config.json---", '{"hijacked": true}', "---END FILE---",
            "---FILE: wiki/entities/topic-b.md---", "topic B page", "---END FILE---",
        ])
        self.assertEqual(paths(text), ["concepts/topic-a.md", "entities/topic-b.md"])

    def test_accepts_hyphenated_paths(self):
        """NashSU canonical: parser accepts paths with hyphens in the filename."""
        text = "\n".join([
            "---FILE: wiki/concepts/multi-head-attention.md---",
            "body",
            "---END FILE---",
        ])
        b = _core.parse_file_blocks(text)
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0][0], "concepts/multi-head-attention.md")


class TestParseFileBlocksTolerantMarkers(unittest.TestCase):
    """Marker/fence tolerance closed 2026-06-19 to reach NashSU parity.

    These four cases were previously tracked as `expectedFailure` gaps. The fixes
    (case-insensitive + whitespace-tolerant FILE/END markers, CommonMark
    fence-length tracking in `_core.parse_file_blocks`) made them pass, so the
    decorators were removed. A regression now fails normally here.
    """

    def test_tolerates_inner_spaces_in_end_marker(self):
        # NashSU CLOSER_LINE accepts `--- END FILE ---` (interior whitespace).
        text = "---FILE: wiki/concepts/foo.md---\nbody\n--- END FILE ---\n"
        b = _core.parse_file_blocks(text)
        self.assertEqual(len(b), 1)
        self.assertNotIn("END FILE", b[0][1])  # marker must not bleed into content

    def test_tolerates_lowercase_end_marker(self):
        # NashSU CLOSER_LINE is case-insensitive — `---end file---` closes the block.
        text = "---FILE: wiki/concepts/foo.md---\nbody\n---end file---\n"
        b = _core.parse_file_blocks(text)
        self.assertEqual(len(b), 1)
        self.assertNotIn("end file", b[0][1].lower())

    def test_tolerates_spaces_after_leading_dashes_in_opener(self):
        # NashSU OPENER_LINE accepts `--- FILE: path ---` (space after the dashes).
        text = "--- FILE: wiki/concepts/foo.md ---\nbody\n---END FILE---\n"
        self.assertEqual(len(_core.parse_file_blocks(text)), 1)

    def test_commonmark_nested_length_fences(self):
        # CommonMark: a 3-tick fence must NOT close a 4-tick opener. FENCE_RE now
        # tracks the fence char + run length, so the inner ``` stays as body.
        text = "\n".join([
            "---FILE: wiki/concepts/foo.md---",
            "````markdown", "```", "---END FILE---", "```", "````", "",
            "real content after the outer fence closes",
            "---END FILE---",
        ])
        b = _core.parse_file_blocks(text)
        self.assertEqual(len(b), 1)
        self.assertIn("real content after the outer fence closes", b[0][1])

    def test_tolerates_trailing_whitespace_on_opener(self):
        """NashSU H3: ``---FILE: wiki/...---   `` (trailing spaces) is accepted."""
        text = "---FILE: wiki/concepts/foo.md---   \nbody\n---END FILE---"
        self.assertEqual(len(_core.parse_file_blocks(text)), 1)


class TestParseFileBlocksStreamWarnings(unittest.TestCase):
    """H2/H6: stream-truncation and empty-path warnings (NashSU parity).

    The skill prints warnings via stdout rather than returning a warnings[]
    array. These tests capture stdout to verify the warnings fire.
    Note: unlike NashSU which DROPS unclosed blocks, the skill keeps them
    (defensive: partial content > nothing). The key parity is that warnings
    are surfaced, not silently lost.
    """

    def test_warns_unclosed_final_block_truncation(self):
        """H2: final block without ``---END FILE---`` emits a truncation warning."""
        text = "\n".join([
            "---FILE: wiki/entities/qwen.md---",
            "# Qwen",
            "---END FILE---",
            "",
            "---FILE: wiki/concepts/moe.md---",
            "# Mixture of Exp",  # stream cut here — no closer
        ])
        with capture_parse_stdout() as buf:
            blocks = _core.parse_file_blocks(text)
        # Both blocks are extracted (skill keeps partial content, unlike NashSU
        # which drops unclosed blocks — defensive choice).
        got = [p for p, _ in blocks]
        self.assertIn("entities/qwen.md", got)
        self.assertIn("concepts/moe.md", got)
        # Unclosed block is surfaced as a stdout warning.
        # The skill strips the wiki/ prefix from paths (architectural diff),
        # so the warning references "concepts/moe.md" not "wiki/concepts/moe.md".
        output = buf.getvalue()
        self.assertIn("concepts/moe.md", output)
        self.assertIn("not closed", output.lower())

    def test_warns_only_unclosed_block(self):
        """H2: when the ONLY block lacks ``---END FILE---``, warn but keep content."""
        text = "---FILE: wiki/concepts/rope.md---\n# RoPE\nIt rotates"
        with capture_parse_stdout() as buf:
            blocks = _core.parse_file_blocks(text)
        # Skill keeps the unclosed block (defensive: partial content better than nothing).
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0][0], "concepts/rope.md")
        output = buf.getvalue()
        self.assertIn("rope.md", output)
        self.assertIn("not closed", output.lower())

    def test_warns_empty_path_block_skipped(self):
        """H6: ``---FILE:   ---`` with whitespace-only path emits a warning."""
        text = "---FILE:   ---\nsome body\n---END FILE---"
        with capture_parse_stdout() as buf:
            blocks = _core.parse_file_blocks(text)
        # Empty-path block must NOT produce a silent write.
        self.assertEqual(len(blocks), 0)
        output = buf.getvalue()
        self.assertIn("empty path", output.lower())

    def test_warns_unclosed_block_between_others(self):
        """H2 variant: a block interleaved between two good blocks without END FILE."""
        text = "\n".join([
            "---FILE: wiki/concepts/a.md---",
            "page A",
            "---END FILE---",
            "---FILE: wiki/concepts/broken.md---",
            "page B (no closer)",
            "---FILE: wiki/concepts/c.md---",
            "page C",
            "---END FILE---",
        ])
        with capture_parse_stdout() as buf:
            blocks = _core.parse_file_blocks(text)
        # All three blocks are extracted (a, broken, c).
        got = [p for p, _ in blocks]
        self.assertIn("concepts/a.md", got)
        self.assertIn("concepts/broken.md", got)
        self.assertIn("concepts/c.md", got)
        # broken block triggers a warning about missing END FILE.
        output = buf.getvalue()
        self.assertIn("concepts/broken.md", output)
        self.assertIn("not closed", output.lower())

    def test_empty_path_inside_content_does_not_warn(self):
        """Empty path only triggers for FILE header, not body lines."""
        text = "---FILE: wiki/concepts/real.md---\n---FILE:   ---  # this is body prose\n---END FILE---"
        with capture_parse_stdout() as buf:
            blocks = _core.parse_file_blocks(text)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0][0], "concepts/real.md")
        # Body line starting with ---FILE: is harmless (treated as content).
        self.assertIn("---FILE:", blocks[0][1])


class TestSourceSlugFromRawPath(unittest.TestCase):
    """Dedup helper: derive expected source page path from raw file path.

    Ported from NashSU source-identity.ts parity — the skill mirrors the
    raw/ directory structure into wiki/sources/ (naming-conventions.md §2.1).
    """

    def setUp(self):
        self.root = Path("/tmp/test-wiki")

    def test_book_type_subdir(self):
        """Mirrors raw/Book/<name>.pdf → wiki/sources/Book/<name>.md"""
        result = _core.source_slug_from_raw_path(
            "/tmp/test-wiki/raw/Book/RF Circuit Design - 2008 - Bowick.pdf",
            self.root,
        )
        self.assertIsNotNone(result)
        self.assertEqual(
            result,
            self.root / "wiki/sources/Book/RF Circuit Design - 2008 - Bowick.md",
        )

    def test_nested_datasheet_subdir(self):
        """Preserves extra nesting: raw/Datasheet/ADI/ADL8113.pdf"""
        result = _core.source_slug_from_raw_path(
            "/tmp/test-wiki/raw/Datasheet/ADI/ADL8113.pdf",
            self.root,
        )
        self.assertIsNotNone(result)
        self.assertEqual(
            result,
            self.root / "wiki/sources/Datasheet/ADI/ADL8113.md",
        )

    def test_str_input_accepted(self):
        """Both str and Path inputs work."""
        result = _core.source_slug_from_raw_path(
            "raw/Book/Test.pdf", str(self.root),
        )
        self.assertIsNotNone(result)

    def test_expands_tilde_in_root(self):
        """~ is expanded in wiki_root. Raw file lives under the expanded root's raw/."""
        import os
        home = os.path.expanduser("~")
        raw_path = f"{home}/test-wiki/raw/Book/Foo.pdf"
        result = _core.source_slug_from_raw_path(raw_path, "~/test-wiki")
        self.assertIsNotNone(result)
        self.assertFalse(str(result).startswith("~"))
        self.assertTrue(str(result).endswith("wiki/sources/Book/Foo.md"))

    def test_file_under_raw_with_relative_path(self):
        """File living under raw/ expressed relatively → canonical result."""
        result = _core.source_slug_from_raw_path(
            "/tmp/test-wiki/raw/SomeFile.pdf",
            self.root,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "SomeFile.md")

    def test_file_outside_raw_returns_none(self):
        """Absolute path outside raw/ returns None."""
        result = _core.source_slug_from_raw_path(
            "/etc/passwd",
            self.root,
        )
        self.assertIsNone(result)

    def test_relative_path_outside_raw_returns_none(self):
        """Relative path that resolves outside raw/ returns None."""
        result = _core.source_slug_from_raw_path(
            "../../outside/file.pdf",
            self.root,
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
