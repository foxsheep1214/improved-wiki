"""Regression test for the 2026-07-09 merge-prompt-cap-too-small bug.

MERGE_PROMPT_BODY_CAP was a fixed 24,000 chars. Re-ingesting a comprehensive
source page (67 claims, 38 entities — body ~65K chars) truncated the "new
content" side of the merge prompt well before the LLM ever saw Key Entities /
Main Arguments / Connections / etc., so the merge fell back to the OLD page's
stale content for everything past the truncation point — even though the
fresh content for those sections existed and was simply never shown to the
merger.

Fix: the per-side cap is config.target_chars (the same live-probed,
context-aware "how much text is safe in one prompt" budget chunking already
uses) when available, falling back to the fixed constant only for configs
that lack it.

Stdlib unittest only — call_anthropic_protocol is monkeypatched (no LLM/network).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _core  # noqa: E402
import _stage_3_write as sw  # noqa: E402


def _cfg(tmp: Path, target_chars) -> _core.Config:
    return _core.Config(
        wiki_root=tmp, raw_root=tmp / "raw", wiki_dir=tmp / "wiki",
        runtime_dir=tmp / "rt", cache_path=tmp / "rt" / "c.json",
        progress_dir=tmp / "rt" / "p", extract_tmp_dir=tmp / "rt" / "e",
        llm_model="m",
        caption_api_key="", caption_base_url="x", caption_model="c",
        chunk_overlap=3000, source_budget=100000,
        target_chars=target_chars, target_tokens=192000, max_tokens=8192,
        conversation_prefix="ab12cd34",
    )


def _page(fm_type: str, body: str, sources: str) -> str:
    return (
        "---\n"
        f"type: {fm_type}\n"
        'title: "X"\n'
        "tags: [x]\n"
        "related: []\n"
        f'sources: ["{sources}"]\n'
        "created: 2026-01-01\n"
        "---\n\n"
        f"{body}"
    )


def _merge_and_capture(existing: str, new: str, cfg: _core.Config) -> tuple[str, dict]:
    captured = {}

    def fake_call(prompt, config, **kw):
        captured["prompt"] = prompt
        # Long enough to clear both the >100-char guard and the body-shrink
        # threshold (0.7 * max(old,new)) for every body size used in this
        # file's tests — the assertions only inspect the PROMPT, not this
        # response's content.
        return "merged output content. " * 3000, None

    orig = sw.call_anthropic_protocol
    sw.call_anthropic_protocol = fake_call
    try:
        sw._stage_3_1_merge_page_content(existing, new, cfg)
    finally:
        sw.call_anthropic_protocol = orig
    return captured.get("prompt", ""), captured


class MergePromptCapIsDynamic(unittest.TestCase):
    def test_large_target_chars_shows_body_beyond_old_fixed_cap(self):
        """A body well past the old fixed 24,000-char cap must appear IN FULL
        in the merge prompt when config.target_chars is generous (e.g. from a
        1M-context probe)."""
        with tempfile.TemporaryDirectory() as t:
            cfg = _cfg(Path(t), target_chars=300_000)
            # Marker placed well past the old fixed 24K cap.
            new_body = ("Filler content. " * 2000) + "ZZZPASTOLDCAP marker\n"
            self.assertGreater(len(new_body), 24_000)
            existing = _page("source", "Old thin body.", "raw/Book/X.pdf")
            new = _page("source", new_body, "raw/Book/X.pdf")

            prompt, _ = _merge_and_capture(existing, new, cfg)
            self.assertIn("ZZZPASTOLDCAP", prompt)

    def test_small_target_chars_truncates_tighter_than_old_fixed_cap(self):
        """A small, deliberately tight target_chars must be honored — the cap
        is genuinely dynamic, not silently floored at the old constant."""
        with tempfile.TemporaryDirectory() as t:
            cfg = _cfg(Path(t), target_chars=100)
            new_body = ("x" * 200) + "ZZZBEYONDSMALLCAP\n"
            existing = _page("source", "Old body.", "raw/Book/X.pdf")
            new = _page("source", new_body, "raw/Book/X.pdf")

            prompt, _ = _merge_and_capture(existing, new, cfg)
            self.assertNotIn("ZZZBEYONDSMALLCAP", prompt)

    def test_missing_target_chars_falls_back_to_fixed_constant(self):
        """A config with no target_chars (e.g. a minimal test config) must
        still work, falling back to MERGE_PROMPT_BODY_CAP."""
        with tempfile.TemporaryDirectory() as t:
            cfg = _cfg(Path(t), target_chars=0)
            new_body = ("y" * (sw.MERGE_PROMPT_BODY_CAP + 500)) + "ZZZFALLBACK\n"
            existing = _page("source", "Old body.", "raw/Book/X.pdf")
            new = _page("source", new_body, "raw/Book/X.pdf")

            prompt, _ = _merge_and_capture(existing, new, cfg)
            self.assertNotIn("ZZZFALLBACK", prompt)


if __name__ == "__main__":
    unittest.main()
