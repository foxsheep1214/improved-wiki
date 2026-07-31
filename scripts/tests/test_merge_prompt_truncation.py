"""Regression: the LLM page-merge prompt truncated each body to a hardcoded 3K.

Observed re-ingesting 《硬件十万个为什么 无源器件篇》 (2026-06-30): merging the
existing English `ohms-law` page (body > 3K) showed the LLM only the first 3000
chars (truncated mid-word — "Vend", "throu"), yet the body-shrink threshold in
_frontmatter.merge_page_content uses the FULL body length (0.7 * max(old,new)).
The LLM, never shown the tail, cannot reproduce ≥70% of the full body, so the
no-fallback policy raised RuntimeError on a legitimate merge.

Fix: the per-side prompt cap is no longer a hardcoded 3000 — normal-sized pages
are shown in full so the threshold is achievable, and the merge call gets the
model's full output budget instead of a hardcoded 4096.

Stdlib unittest only — call_anthropic_protocol is monkeypatched (no LLM/network).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _core  # noqa: E402
import _stage_3_write as sw  # noqa: E402


def _cfg(tmp: Path) -> _core.Config:
    return _core.Config(
        wiki_root=tmp, raw_root=tmp / "raw", wiki_dir=tmp / "wiki",
        runtime_dir=tmp / "rt", cache_path=tmp / "rt" / "c.json",
        progress_dir=tmp / "rt" / "p", extract_tmp_dir=tmp / "rt" / "e",
        llm_model="m",
        caption_api_key="", caption_base_url="x", caption_model="c",
        chunk_overlap=3000, source_budget=100000,
        target_chars=768000, target_tokens=192000, max_tokens=8192,
        conversation_prefix="ab12cd34",
    )


def _page(body: str, sources: str) -> str:
    return (
        "---\n"
        "type: concept\n"
        'title: "Ohm Law"\n'
        "tags: [x]\n"
        "related: []\n"
        f'sources: ["{sources}"]\n'
        "created: 2026-01-01\n"
        "---\n\n"
        f"{body}"
    )


class MergePromptTruncation(unittest.TestCase):
    def test_full_body_shown_and_merge_not_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            cfg = _cfg(Path(t))
            # Existing body > 3K with a unique marker placed PAST char 3000.
            existing_body = ("Existing detail. " * 250) + "\nZZZTAILMARKER at the end.\n"
            self.assertGreater(len(existing_body), 4000)
            existing = _page(existing_body, "raw/book/A.pdf")
            new = _page("New ingest detail. " * 60, "raw/book/B.pdf")  # different source

            captured = {}

            def fake_call(prompt, config, **kw):
                captured["prompt"] = prompt
                captured["max_tokens"] = kw.get("max_tokens")
                # A faithful merge preserves the existing body + adds new info.
                return existing_body + "\n\n## New\nNew ingest detail merged.\n", None

            orig = sw.call_anthropic_protocol
            sw.call_anthropic_protocol = fake_call
            try:
                result = sw._stage_3_1_merge_page_content(existing, new, cfg)
            finally:
                sw.call_anthropic_protocol = orig

            # The LLM must have seen the tail of the existing body (cap > 3000).
            self.assertIn("ZZZTAILMARKER", captured["prompt"])
            # The merge call gets the model's real output budget, not 4096.
            self.assertEqual(captured["max_tokens"], cfg.max_tokens)
            # Merge succeeded (no RuntimeError) and kept existing content.
            self.assertIn("ZZZTAILMARKER", result)
            self.assertIn("New ingest detail merged", result)


if __name__ == "__main__":
    unittest.main()
