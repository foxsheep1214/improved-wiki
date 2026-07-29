"""Outer-code-fence strip must cover the shapes NashSU covers.

_ingest_sanitize is a port of NashSU ingest-sanitize.ts, but the opener regex
had dropped three tolerances (BOM, blank lines before the fence, a mixed-case
info string) and the `frontmatterOnly` branch entirely. A page matching any of
those shapes kept its ``` first line on disk permanently: frontmatter then
failed to parse, so type/tags/sources went invisible to the graph, index
rebuild and dedup, and `wiki-lint --fix` stacked a placeholder frontmatter
block on top of the fence instead of removing it.

Reference: ingest-sanitize.ts:93-95 (opener) and :107-111 (frontmatter-only).

Stdlib unittest only — no pytest, no network, no LLM calls.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _frontmatter import parse_frontmatter  # noqa: E402
from _ingest_sanitize import sanitize_ingested_file_content  # noqa: E402

FM = '---\ntype: concept\ntitle: "Matched Filter"\ntags: [radar]\n---\n'
BODY = "\n# Matched Filter\n\nCorrelates the echo with a replica.\n"


class TestOuterFenceShapes(unittest.TestCase):
    def assert_clean(self, raw: str, label: str):
        cleaned = sanitize_ingested_file_content(raw)
        self.assertTrue(
            cleaned.startswith("---"),
            f"{label}: expected frontmatter at start, got {cleaned[:40]!r}")
        fm, _ = parse_frontmatter(cleaned)
        self.assertEqual("concept", fm.get("type"), label)
        self.assertIn("Correlates the echo", cleaned, label)

    def test_plain_whole_document_wrap(self):
        self.assert_clean(f"```markdown\n{FM}{BODY}```\n", "plain wrap")

    def test_fence_closed_right_after_frontmatter(self):
        # Shape A: model closes the fence after the frontmatter, body unfenced.
        self.assert_clean(f"```yaml\n{FM}```\n{BODY}", "frontmatter-only")

    def test_blank_lines_before_fence(self):
        # Shape B: one or more blank lines precede the opening fence.
        self.assert_clean(f"\n\n```markdown\n{FM}{BODY}```\n", "leading blanks")

    def test_uppercase_info_string(self):
        # Shape D: ```YAML rather than ```yaml.
        self.assert_clean(f"```YAML\n{FM}{BODY}```\n", "uppercase info string")

    def test_bom_before_fence(self):
        # Shape C: a UTF-8 BOM survives ahead of the fence.
        self.assert_clean(f"﻿```markdown\n{FM}{BODY}```\n", "BOM")

    def test_is_idempotent(self):
        once = sanitize_ingested_file_content(f"```markdown\n{FM}{BODY}```\n")
        self.assertEqual(once, sanitize_ingested_file_content(once))

    def test_leaves_unfenced_page_untouched(self):
        page = f"{FM}{BODY}"
        self.assertEqual(page, sanitize_ingested_file_content(page))

    def test_leaves_mid_body_fence_untouched(self):
        page = f"{FM}\n# Title\n\n```python\nprint('hi')\n```\n"
        self.assertEqual(page, sanitize_ingested_file_content(page))

    def test_unclosed_leading_fence_is_left_alone(self):
        # NashSU deliberately does not "fix" mid-stream truncation: with no
        # closing fence and no complete frontmatter block, leave it as-is.
        raw = "```markdown\n# Half a page, stream died here"
        self.assertEqual(raw, sanitize_ingested_file_content(raw))


class TestReadTimeFallbackMatchesWriteTime(unittest.TestCase):
    """Already-written corrupt pages must still parse on read."""

    def test_blank_lines_before_fence(self):
        fm, _ = parse_frontmatter(f"\n\n```markdown\n{FM}{BODY}```\n")
        self.assertEqual("concept", fm.get("type"))

    def test_uppercase_info_string(self):
        fm, _ = parse_frontmatter(f"```MD\n{FM}{BODY}```\n")
        self.assertEqual("concept", fm.get("type"))

    def test_bom_before_fence(self):
        fm, _ = parse_frontmatter(f"﻿```markdown\n{FM}{BODY}```\n")
        self.assertEqual("concept", fm.get("type"))


if __name__ == "__main__":
    unittest.main()
