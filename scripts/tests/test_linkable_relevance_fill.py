"""Fix 2026-07-02: relevance-ranked Linkable-list fill (was alphabetical).

The Stage 2.6 linkable[:1500] cap and the Stage 2.4 background-fill caps
truncated an ALPHABETICALLY sorted list, so late-sorting slugs — CJK sorts
after ASCII — systematically vanished as the wiki grew (observed live: 4 valid
CJK slugs fell outside a source-page prompt's 1500 cap; same disease as the
fixed [:200]/[:300] caps). When candidates exceed the cap, the fill is now
ranked by token/CJK-bigram overlap with the book's own generated slugs/titles
(deterministic: score desc, slug asc — prompt-hash stable within one ingest).
Must-link semantics unchanged. Stdlib unittest only; LLM calls are spied.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _core  # noqa: E402
import _stage_2_4_generation as gen  # noqa: E402
import _source_page as s26  # noqa: E402


def _make_config(tmp: Path) -> _core.Config:
    return _core.Config(
        wiki_root=tmp, raw_root=tmp / "raw",
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


class TestRankLinkableFill(unittest.TestCase):
    def test_relevant_cjk_outranks_irrelevant_ascii(self):
        ranked = gen._rank_linkable_fill(
            ["concepts/aaa-filter", "concepts/多目标跟踪算法"],
            ["多目标跟踪"])
        self.assertEqual(ranked[0], "concepts/多目标跟踪算法")

    def test_ascii_word_overlap_scores(self):
        ranked = gen._rank_linkable_fill(
            ["concepts/zzz-matched-filter", "concepts/aaa-unrelated"],
            ["Matched Filter"])
        # zzz- sorts last alphabetically but wins on word overlap.
        self.assertEqual(ranked[0], "concepts/zzz-matched-filter")

    def test_no_references_falls_back_to_slug_order(self):
        # All scores 0 → deterministic alphabetical tie-break (old behavior).
        self.assertEqual(
            gen._rank_linkable_fill(["b-slug", "a-slug"], []),
            ["a-slug", "b-slug"])


class TestBuildPromptRelevanceFill(unittest.TestCase):
    """Per-chunk builder: fill over room keeps book-relevant CJK slugs."""

    def _build(self, tmp: Path) -> str:
        cfg = _make_config(tmp)
        cfg.wiki_dir.mkdir(parents=True, exist_ok=True)
        cfg.raw_root.mkdir(parents=True, exist_ok=True)
        return gen._stage_2_4_build_prompt(
            {"concepts_found": [{"name": "多目标跟踪", "definition": "x",
                                 "importance": "core"}],
             "entities_found": []},
            "chunk text", 1, cfg.raw_root / "book.pdf", cfg,
            generated_slugs=[],
        )

    def test_relevant_cjk_fill_survives_cap(self):
        # 451 fill candidates > room (400 - 1 must-link): the CJK slug sorts
        # last, so the old alphabetical cut dropped it. Relevance keeps it.
        bg = [f"concepts/aaa-{i:04d}" for i in range(450)]
        orig = gen.list_existing_slugs
        gen.list_existing_slugs = lambda config: bg + ["concepts/多目标跟踪算法"]
        try:
            with tempfile.TemporaryDirectory() as d:
                prompt = self._build(Path(d))
                prompt2 = self._build(Path(d))
        finally:
            gen.list_existing_slugs = orig
        linkable = prompt[prompt.index("# Linkable pages"):]
        self.assertIn("concepts/多目标跟踪算法", linkable)
        # A zero-score tail candidate was displaced to make room.
        self.assertNotIn("concepts/aaa-0449", linkable)
        # Deterministic: identical prompt across rebuilds (cache-key stable).
        self.assertEqual(prompt, prompt2)


class TestBuildAllPromptRelevanceFill(unittest.TestCase):
    """Single-shot builder: same relevance fill; must-link never dropped."""

    def test_relevant_cjk_fill_survives_cap(self):
        bg = [f"concepts/aaa-{i:04d}" for i in range(350)]
        orig = gen.list_existing_slugs
        gen.list_existing_slugs = lambda config: bg + ["concepts/多目标跟踪算法"]
        try:
            with tempfile.TemporaryDirectory() as d:
                tmp = Path(d)
                cfg = _make_config(tmp)
                cfg.wiki_dir.mkdir(parents=True, exist_ok=True)
                cfg.raw_root.mkdir(parents=True, exist_ok=True)
                prompt = gen._stage_2_4_build_all_prompt(
                    [{"concepts_found": [{"name": "多目标跟踪", "definition": "x",
                                          "importance": "core"}],
                      "entities_found": []}],
                    cfg.raw_root / "book.pdf", cfg,
                    existing_refs={"Zeta Theorem": ["concepts/zzz-target"]},
                )
        finally:
            gen.list_existing_slugs = orig
        linkable = prompt[prompt.index("# Linkable pages"):]
        self.assertIn("concepts/多目标跟踪算法", linkable)
        self.assertNotIn("concepts/aaa-0349", linkable)
        # Must-link target (Stage 2.3 existing_refs) never displaced by fill.
        self.assertIn("concepts/zzz-target", linkable)


class TestSourcePageLinkableRelevance(unittest.TestCase):
    """Stage 2.6: over the 1500 cap, relevant CJK slugs and the book's own
    generated slugs survive; irrelevant alphabetical tail is displaced."""

    # test_cjk_slugs_survive_1500_cap was removed 2026-08-01: it exercised
    # _rank_linkable_fill through the retired Stage 2.6 source-page prompt.
    # The ranking contract itself (CJK-relevant slugs kept, zero-score
    # alphabetical tail dropped) is covered directly by the unit tests above.

if __name__ == "__main__":
    unittest.main()
