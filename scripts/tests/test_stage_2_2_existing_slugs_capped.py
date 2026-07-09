"""Redundancy fix A (2026-07-09): cap the existing-wiki slug list in 2.2 prompts.

_stage_2_2_build_prompt embedded the FULL list_existing_slugs() joined on one
line — 6,253 pages → a single 259KB prompt line, repeated in EVERY chunk
prompt (5-12 per book), 5× NashSU's 40K Current-Wiki-Index trim. It also broke
answering subagents' Read tooling (observed live 2026-07-09). 2.4 and 2.6
already rank-and-cap their linkable lists; 2.2 was the only uncapped one.

Fix: rank by relevance to THIS chunk's text (slug-token containment in the
chunk token set — deterministic, prompt-hash stable across resumes) and keep
the best _EXISTING_SLUGS_CAP.

Stdlib unittest only — no LLM/network calls.
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
import _stage_2_analyze as s2  # noqa: E402


def _cfg(tmp: Path) -> _core.Config:
    return _core.Config(
        wiki_root=tmp, raw_root=tmp / "raw", wiki_dir=tmp / "wiki",
        runtime_dir=tmp / "rt", cache_path=tmp / "rt" / "c.json",
        progress_dir=tmp / "rt" / "p", extract_tmp_dir=tmp / "rt" / "e",
        llm_base_url="x", llm_model="m", llm_api_key="", llm_protocol="anthropic",
        caption_api_key="", caption_base_url="x", caption_model="c",
        chunk_size=60000, chunk_overlap=3000, source_budget=100000,
        target_chars=256000, target_tokens=192000, max_tokens=8192,
        conversation_prefix="ab12cd34",
    )


CHUNK_TEXT = (
    "This chapter develops the matched filter for phased array antenna "
    "systems, following Hansen's treatment of grating lobes. " * 50
)

RELEVANT = [
    "concepts/matched-filter",
    "concepts/phased-array-antenna",
    "concepts/grating-lobes",
]


def _build_with_slugs(slugs: list[str]) -> str:
    orig = s2.list_existing_slugs
    s2.list_existing_slugs = lambda config: list(slugs)
    try:
        with tempfile.TemporaryDirectory() as t:
            return s2._stage_2_2_build_prompt(
                chunk_text=CHUNK_TEXT, chunk_index=1, chunk_total=3,
                global_digest={}, file_path=Path("book.pdf"),
                config=_cfg(Path(t)), accumulated_digest="prior digest",
            )
    finally:
        s2.list_existing_slugs = orig


class ExistingSlugsAreCapped(unittest.TestCase):
    def test_over_cap_keeps_relevant_drops_irrelevant_bulk(self):
        filler = [f"concepts/unrelated-filler-topic-{i:05d}"
                  for i in range(s2._EXISTING_SLUGS_CAP + 500)]
        prompt = _build_with_slugs(filler + RELEVANT)
        for slug in RELEVANT:
            self.assertIn(slug, prompt)
        # The filler overflow must NOT all be present: count bounded by cap.
        self.assertLessEqual(prompt.count("concepts/unrelated-filler-topic-"),
                             s2._EXISTING_SLUGS_CAP)

    def test_under_cap_passes_all_through(self):
        slugs = RELEVANT + [f"concepts/other-{i}" for i in range(20)]
        prompt = _build_with_slugs(slugs)
        for slug in slugs:
            self.assertIn(slug, prompt)

    def test_ranking_is_deterministic(self):
        filler = [f"concepts/unrelated-filler-topic-{i:05d}"
                  for i in range(s2._EXISTING_SLUGS_CAP + 200)]
        p1 = _build_with_slugs(filler + RELEVANT)
        p2 = _build_with_slugs(filler + RELEVANT)
        self.assertEqual(p1, p2)


if __name__ == "__main__":
    unittest.main()
