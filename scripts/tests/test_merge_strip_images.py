"""Regression tests for stripping the ## Embedded Images section before merge.

Historical bug (2026-06-25): re-ingesting a source page whose on-disk version
carries a 50K+ ``## Embedded Images`` section (457 images, auto-injected by
Stage 3.2) triggered an LLM page-merge. The merge prompt truncates each body
to 3K, so the LLM output (~3K) fell below the body-shrink threshold
(0.7 * max(old_len, new_len) ≈ 0.7 * 64K = 45K), and the no-fallback policy
paused the ingest. Fix: ``strip_embedded_images_section`` removes the section
before body comparison/threshold, so a same-book re-ingest hits the
"bodies identical" fast path (the semantic body is unchanged) and 3.2
re-injects images afterward.

Stdlib unittest only — no pytest, no network, no LLM calls.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _frontmatter import strip_embedded_images_section, merge_page_content  # noqa: E402


SEMANTIC_BODY = (
    "## Book Summary\n"
    "A comprehensive reference on phased arrays.\n\n"
    "## Key Takeaways\n"
    "- SEP × array factor = overall pattern.\n"
    "- 2λ/L steering bandwidth rule.\n"
)

IMAGES_SECTION = (
    "\n\n## Embedded Images\n\n"
    "| Page | Caption | File |\n|------|---------|------|\n"
    "| p4 | cover | book/p0004.jpg |\n"
    "| p24 | block diagram | book/p0024.jpg |\n"
)


def _page(body: str, sources: str = "raw/Book/X.pdf") -> str:
    return (
        "---\n"
        "type: source\n"
        'title: "X"\n'
        "domain: rf-electronics\n"
        "tags: [phased-array]\n"
        "related: []\n"
        f'sources: ["{sources}"]\n'
        "---\n\n"
        f"{body}"
    )


class StripEmbeddedImages(unittest.TestCase):
    def test_removes_section_to_end_of_body(self):
        body = SEMANTIC_BODY + IMAGES_SECTION
        stripped = strip_embedded_images_section(body)
        self.assertNotIn("## Embedded Images", stripped)
        self.assertNotIn("p0004.jpg", stripped)
        self.assertIn("## Key Takeaways", stripped)
        self.assertIn("SEP × array factor", stripped)

    def test_no_section_returns_body_unchanged(self):
        self.assertEqual(strip_embedded_images_section(SEMANTIC_BODY), SEMANTIC_BODY)

    def test_strips_trailing_whitespace(self):
        body = SEMANTIC_BODY + IMAGES_SECTION + "\n\n\n"
        stripped = strip_embedded_images_section(body)
        self.assertTrue(stripped.endswith("2λ/L steering bandwidth rule."))
        self.assertFalse(stripped.endswith("\n"))


class MergeFastPathOnImagesOnlyDiff(unittest.TestCase):
    """Same-book re-ingest: existing has images section, new does not."""

    def test_identical_semantic_body_skips_llm_merge(self):
        existing = _page(SEMANTIC_BODY + IMAGES_SECTION)
        new = _page(SEMANTIC_BODY)
        # No merger_fn → if bodies (after strip) are identical, fast path 3
        # returns array_merged without needing an LLM. If the strip weren't
        # applied, the bodies would differ and the function would fall through
        # to the LLM branch (merger_fn=None → returns array_merged with the NEW
        # body, dropping the existing). We assert the semantic body survives.
        result = merge_page_content(new, existing, merger_fn=None)
        self.assertIn("SEP × array factor", result)
        self.assertIn("## Key Takeaways", result)
        # The images section is NOT carried in by the merge (3.2 re-injects).
        self.assertNotIn("## Embedded Images", result)

    def test_threshold_uses_image_stripped_length(self):
        # If strip weren't applied, old_len ~64K → threshold ~45K, and any LLM
        # output would be rejected. With strip, old_len is small. This test
        # ensures a merger_fn that returns the semantic body is accepted (not
        # rejected by an inflated threshold).
        #
        # `new` uses a DIFFERENT source than `existing` so the idempotent
        # re-merge fast path (fast path 5: existing.sources ⊇ new.sources)
        # does NOT short-circuit — otherwise the merger_fn is never called and
        # the threshold logic is never exercised.
        existing = _page(SEMANTIC_BODY + IMAGES_SECTION, sources="raw/Book/X.pdf")
        new = _page(SEMANTIC_BODY + "\n\n## Extra\nNew detail.\n", sources="raw/Book/Y.pdf")

        def merger(prev, merged, src):
            # Return a body that's reasonably sized relative to the STRIPPED
            # bodies (would fail if threshold used the image-laden length).
            return _page(SEMANTIC_BODY + "\n\n## Extra\nNew detail.\n", sources="raw/Book/X.pdf")

        result = merge_page_content(new, existing, merger_fn=merger)
        self.assertIn("New detail", result)


if __name__ == "__main__":
    unittest.main()
