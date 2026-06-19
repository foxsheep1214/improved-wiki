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
     `wiki/` prefix stripped, and surfaces unsafe/unclosed blocks via stdout
     prints rather than a `warnings` array.

`@unittest.expectedFailure` marks behavior NashSU has but the skill does not yet
match — porting them documents the gap. If one starts passing (someone closed the
gap), unittest reports an "unexpected success" → remove the decorator.

Run:  python3 scripts/tests/test_nashsu_parity.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _core  # noqa: E402


def paths(text: str) -> list[str]:
    return [p for p, _ in _core.parse_file_blocks(text)]


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


class TestParseFileBlocksKnownGaps(unittest.TestCase):
    """NashSU behaviors the skill does NOT yet match. expectedFailure = tracked gap.

    Closing any of these (in `_core.parse_file_blocks`) will turn the case into an
    "unexpected success" — at which point delete the decorator.
    """

    @unittest.expectedFailure
    def test_tolerates_inner_spaces_in_end_marker(self):
        # NashSU accepts `--- END FILE ---`; skill's END_FILE_RE requires exact
        # `---END FILE---`. The skill returns 1 block ONLY via its unclosed-block
        # flush, so the unrecognized marker line leaks into the body — that is the gap.
        text = "---FILE: wiki/concepts/foo.md---\nbody\n--- END FILE ---\n"
        b = _core.parse_file_blocks(text)
        self.assertEqual(len(b), 1)
        self.assertNotIn("END FILE", b[0][1])  # marker must not bleed into content

    @unittest.expectedFailure
    def test_tolerates_lowercase_end_marker(self):
        # NashSU accepts `---end file---` (case-insensitive); skill is uppercase-only,
        # so the marker leaks into the body (block survives only via flush).
        text = "---FILE: wiki/concepts/foo.md---\nbody\n---end file---\n"
        b = _core.parse_file_blocks(text)
        self.assertEqual(len(b), 1)
        self.assertNotIn("end file", b[0][1].lower())

    @unittest.expectedFailure
    def test_tolerates_spaces_after_leading_dashes_in_opener(self):
        # NashSU accepts `--- FILE: path ---`; skill's FILE_HEADER_RE requires `---FILE:`.
        text = "--- FILE: wiki/concepts/foo.md ---\nbody\n---END FILE---\n"
        self.assertEqual(len(_core.parse_file_blocks(text)), 1)

    @unittest.expectedFailure
    def test_commonmark_nested_length_fences(self):
        # CommonMark: a 3-tick fence must NOT close a 4-tick opener. The skill's
        # FENCE_RE `^(```|~~~)` collapses 4-tick and 3-tick to the same marker,
        # so the inner ``` wrongly pops the stack and the real content is dropped.
        text = "\n".join([
            "---FILE: wiki/concepts/foo.md---",
            "````markdown", "```", "---END FILE---", "```", "````", "",
            "real content after the outer fence closes",
            "---END FILE---",
        ])
        b = _core.parse_file_blocks(text)
        self.assertEqual(len(b), 1)
        self.assertIn("real content after the outer fence closes", b[0][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
