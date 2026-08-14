"""The consolidated Stage 2 context must degrade structurally, not mid-JSON.

Stage 2.4 became ONE whole-source generation call (NashSU 0.6.6). Its shared
context carries the final rolling digest, every chunk analysis, and bounded raw
evidence. The first implementation divided the analyses budget evenly and cut
each chunk's payload with a balanced character excerpt, which on a real long
book meant (measured at source_budget=104,000):

    chunks=20  analyses needed 198,130 chars, got ~60,000 → every one cut
    chunks=40  analyses needed 396,390 chars, got ~60,000 → ~1.5K each

and each cut landed in the middle of a JSON object, so the generation model was
handed 20 broken fragments. Raw evidence fared worse: ~1,400 of 48,000 chars per
chunk (≈3%), spent whether or not it was useful.

Fix: drop whole low-value FIELDS in a fixed priority order until the analyses
fit (source_quotes → connections → formulas/key_details → everything but names,
definitions and claims), keep every payload a parseable object, hand the
leftover budget to raw evidence instead of pre-splitting it, and state in the
context itself what was dropped.

Stdlib unittest only — no pytest, no network, no LLM calls.
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _ingest_chunks as chunks  # noqa: E402
from _stage_2_context import (  # noqa: E402
    STAGE_2_CONTEXT_POLICY_VERSION,
    build_consolidated_stage_2_context,
)

_CHUNK_HEADING = re.compile(r"^### (?:Chunk )?(\d+)/(\d+).*$", re.MULTILINE)

# The budget is in TOKENS. Every fixture below is ASCII, where the shared
# estimator measures ~4 chars/token, so this reproduces the same ~104,000-char
# squeeze the degradation ladder was built for.
_TIGHT_BUDGET_TOKENS = 26_000
_ASCII_CHARS_PER_TOKEN = 4


def _digest() -> dict:
    return {
        "book_meta": {"title": "Long Book"},
        "outline": [f"Chapter {i}" for i in range(30)],
        "key_concepts": [f"kc-{i}" for i in range(40)],
        "key_claims": [f"kk-{i}" for i in range(30)],
        "key_entities": [f"ke-{i}" for i in range(40)],
    }


def _analysis(index: int) -> dict:
    """A realistically bulky validated Stage 2.2 analysis (~10-20K chars)."""
    return {
        "chunk_index": index + 1,
        "chunk_total": 20,
        "concepts_found": [
            {
                "name": f"concept-{index}-{j}",
                "importance": "core",
                "definition": f"definition-{index}-{j} " + "d" * 200,
                "key_details": [f"detail-{index}-{j}-{k} " + "k" * 120
                                for k in range(3)],
            }
            for j in range(10)
        ],
        "entities_found": [
            {"name": f"entity-{index}-{j}",
             "significance": f"sig-{index}-{j} " + "s" * 150}
            for j in range(6)
        ],
        "claims": [
            {"claim": f"claim-{index}-{j} " + "c" * 150,
             "evidence": f"evidence-{index}-{j} " + "e" * 120,
             "confidence": "high"}
            for j in range(8)
        ],
        "formulas": [
            {"formula": f"F_{index} = x^{j}", "meaning": "m" * 80}
            for j in range(4)
        ],
        "connections_to_existing_wiki": [
            {"existing_page": f"concepts/old-{index}-{j}",
             "relationship": "extends"}
            for j in range(4)
        ],
        "schema_typed_candidates": [
            {"type": "finding", "name": f"finding-{index}", "folder": "findings",
             "rationale": "r" * 100},
        ],
        "source_quotes": f"quote-{index} " + "q" * 3000,
        "updated_global_digest": "D" * 15000,
        "_chunk_id": f"{index:04d}-abc",
    }


def _meta(index: int, raw_chars: int = 48_000) -> tuple:
    body = f"RAW-HEAD-{index}-" + ("r" * raw_chars) + f"-RAW-TAIL-{index}"
    return (index, body, "", f"Chapter {index}")


def _section(context: str, title: str) -> str:
    start = context.index(title)
    rest = context[start + len(title):]
    following = [m.start() for m in re.finditer(r"^## ", rest, re.MULTILINE)]
    return rest[:following[0]] if following else rest


def _analysis_payloads(context: str) -> list[dict]:
    """Parse each per-chunk analysis payload back out of the rendered section."""
    section = _section(context, "## Per-Chunk Analyses")
    headings = list(_CHUNK_HEADING.finditer(section))
    payloads = []
    for position, match in enumerate(headings):
        end = (headings[position + 1].start()
               if position + 1 < len(headings) else len(section))
        payloads.append(json.loads(section[match.end():end].strip()))
    return payloads


class LongSourceDegradesStructurally(unittest.TestCase):
    def setUp(self):
        self.analyses = [_analysis(i) for i in range(20)]
        self.metas = [_meta(i) for i in range(20)]
        self.context = build_consolidated_stage_2_context(
            _digest(), self.analyses, self.metas, _TIGHT_BUDGET_TOKENS)

    def test_budget_is_respected(self):
        self.assertLessEqual(
            len(self.context),
            _TIGHT_BUDGET_TOKENS * _ASCII_CHARS_PER_TOKEN)

    def test_every_chunk_analysis_stays_a_parseable_object(self):
        payloads = _analysis_payloads(self.context)
        self.assertEqual(20, len(payloads))
        for position, payload in enumerate(payloads):
            self.assertIsInstance(payload, dict)
            self.assertEqual(position + 1, payload["chunk_index"])

    def test_concept_entity_and_claim_names_survive_for_every_chunk(self):
        for index in range(20):
            self.assertIn(f"concept-{index}-0", self.context)
            self.assertIn(f"entity-{index}-0", self.context)
            self.assertIn(f"claim-{index}-0", self.context)
            self.assertIn(f"finding-{index}", self.context)

    def test_low_value_fields_are_dropped_whole(self):
        payloads = _analysis_payloads(self.context)
        self.assertNotIn("source_quotes", payloads[0])
        self.assertTrue(
            all(p.get("concepts_found") for p in payloads),
            "concepts are the highest-priority field and must never be dropped")

    def test_dropped_fields_are_stated_in_the_context(self):
        notes = _section(self.context, "## Context Budget")
        self.assertIn("source_quotes", notes)
        self.assertIn("20", notes)

    def test_deterministic(self):
        again = build_consolidated_stage_2_context(
            _digest(), self.analyses, self.metas, _TIGHT_BUDGET_TOKENS)
        self.assertEqual(self.context, again)


class OversizedDigestStaysStructured(unittest.TestCase):
    def test_digest_truncation_remains_parseable_json(self):
        digest = _digest()
        digest["large_quoted_field"] = (
            'HEAD "quoted" \\\\ ' + ("d" * 40_000) + " TAIL"
        )
        context = build_consolidated_stage_2_context(
            digest,
            [_analysis(0), _analysis(1), _analysis(2)],
            [_meta(0), _meta(1), _meta(2)],
            _TIGHT_BUDGET_TOKENS,
        )

        rendered = _section(context, "## Final Global Digest").strip()
        parsed = json.loads(rendered)
        self.assertIn("_truncated", parsed)
        self.assertTrue(parsed["head"].startswith("{"))
        self.assertTrue(parsed["tail"].endswith("}"))


class ShortSourceKeepsFullDetail(unittest.TestCase):
    def test_single_chunk_keeps_every_analysis_field_and_all_raw_text(self):
        context = build_consolidated_stage_2_context(
            _digest(), [_analysis(0)], [_meta(0, raw_chars=20_000)],
            _TIGHT_BUDGET_TOKENS)

        payload = _analysis_payloads(context)[0]
        self.assertIn("source_quotes", payload)
        self.assertIn("formulas", payload)
        self.assertIn("connections_to_existing_wiki", payload)
        self.assertIn("RAW-HEAD-0", context)
        self.assertIn("RAW-TAIL-0", context)
        self.assertNotIn("middle omitted", context)


class RawEvidenceGetsTheLeftoverBudget(unittest.TestCase):
    def test_raw_share_grows_once_analyses_fit(self):
        """Analyses are sized to what they need; raw takes the remainder."""
        analyses = [_analysis(i) for i in range(3)]
        metas = [_meta(i, raw_chars=40_000) for i in range(3)]
        context = build_consolidated_stage_2_context(
            _digest(), analyses, metas, _TIGHT_BUDGET_TOKENS)

        raw = _section(context, "## Bounded Raw Source Evidence")
        analysis_section = _section(context, "## Per-Chunk Analyses")
        self.assertGreater(
            len(raw), len(analysis_section),
            "a 3-chunk source has room for full analyses plus lots of raw text")
        for index in range(3):
            self.assertIn(f"RAW-HEAD-{index}", raw)
            self.assertIn(f"RAW-TAIL-{index}", raw)


class BudgetIsTokensNotCharacters(unittest.TestCase):
    """The budget must buy the same amount of MEANING in either script.

    NashSU's `maxContextSize` is character-scale; improved-wiki probes tokens.
    Copying NashSU's number onto a token context and spending it as characters
    under-budgeted a Latin-script source ~4x — Stage 2.4 saw ~13% of the window
    while each Stage 2.2 chunk prompt got 32%.
    """

    @staticmethod
    def _context(body: str) -> str:
        return build_consolidated_stage_2_context(
            _digest(), [_analysis(0)], [(0, body, "", "Chapter 0")], 10_000)

    def test_latin_budget_is_about_four_characters_per_token(self):
        context = self._context("RAW-HEAD-0-" + ("r" * 200_000) + "-RAW-TAIL-0")
        self.assertGreater(len(context), 30_000)
        self.assertLessEqual(len(context), 10_000 * 4)

    def test_cjk_budget_is_about_one_character_per_token(self):
        context = self._context("RAW-HEAD-0-" + ("雷达散射截面积" * 30_000)
                                + "-RAW-TAIL-0")
        self.assertLessEqual(len(context), 10_000 * 2)

    def test_same_token_budget_buys_more_characters_in_latin(self):
        latin = self._context("RAW-HEAD-0-" + ("r" * 200_000) + "-RAW-TAIL-0")
        cjk = self._context("RAW-HEAD-0-" + ("雷达散射截面积" * 30_000)
                            + "-RAW-TAIL-0")
        self.assertGreater(
            len(latin), len(cjk) * 2,
            "a character-scale budget would have made these nearly equal")


class PolicyVersionInvalidatesOldGenerationCache(unittest.TestCase):
    def test_version_moved_past_v1(self):
        self.assertNotEqual(
            "nashsu-0.6.6-consolidated-v1", STAGE_2_CONTEXT_POLICY_VERSION,
            "a different context shape must invalidate pre-write 2.4 caches")

    def test_generation_policy_tracks_the_context_policy(self):
        self.assertEqual(
            STAGE_2_CONTEXT_POLICY_VERSION, chunks.GENERATION_POLICY_VERSION)


if __name__ == "__main__":
    unittest.main()
