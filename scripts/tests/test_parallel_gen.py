"""Tests for the eager-inventory + parallel/drain Stage 2.4 gen mode.

DEFAULT ON since 2026-07-09 (user decision) — cross-chunk dedup in 2.4 is a
deterministic slug lookup, not a content dependency like Stage 2.2's rolling
digest, so answer order doesn't matter. Opt-out via
``IMPROVED_WIKI_PARALLEL_GEN=0/false/no/off`` restores the old strictly-serial
accumulation path (for bisecting a regression).

Stdlib `unittest` only — no pytest, no network, no LLM calls.

Run:
    python3 -m unittest tests.test_parallel_gen     # from scripts/
    python3 scripts/tests/test_parallel_gen.py       # from skill root

Covers:
- ``_build_gen_inventory`` first-chunk-owns assignment, entities and
  schema-typed candidates included, blank names skipped.
- ``_other_chunk_slugs`` = all stems except those owned by i, sorted.
- Determinism of the per-chunk other-slug list.
- Unset env var → parallel-safe drain (the new default).
- Explicit opt-out (``0``/``false``/``no``/``off``) → serial accumulation
  (chunk 2 sees chunk 1's produced slugs).
- Drain mode: ≥2 uncached chunks raise ConversationPending exactly once
  after attempting the configured parallel wave; 0 uncached returns the union
  of blocks.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _core  # noqa: E402
import _ingest_chunks  # noqa: E402


def _analysis(concepts=(), entities=()):
    return {
        "concepts_found": [{"name": n} for n in concepts],
        "entities_found": [{"name": n} for n in entities],
    }


def _meta(i, text="chunk-text"):
    # (chunk_idx, chunk_text, overlap_before, heading_path)
    return (i, text, "", "")


class TestBuildGenInventory(unittest.TestCase):
    def test_count_mismatch_is_a_hard_error_not_zip_truncation(self):
        metas = [_meta(0), _meta(1), _meta(2)]
        analyses = [_analysis(concepts=["A"]), _analysis(concepts=["B"])]
        with self.assertRaisesRegex(RuntimeError, "cardinality mismatch"):
            _ingest_chunks._build_gen_inventory(metas, analyses)

    def test_first_chunk_owns(self):
        metas = [_meta(0), _meta(1), _meta(2)]
        analyses = [
            _analysis(concepts=["Alpha", "Beta"]),
            _analysis(concepts=["Gamma"]),
            _analysis(concepts=["Alpha"]),  # appears again → still owned by 0
        ]
        inv = _ingest_chunks._build_gen_inventory(metas, analyses)
        self.assertEqual(inv["alpha"], 0)
        self.assertEqual(inv["beta"], 0)
        self.assertEqual(inv["gamma"], 1)

    def test_concept_in_chunks_0_and_2_owned_by_0(self):
        metas = [_meta(0), _meta(1), _meta(2)]
        analyses = [
            _analysis(concepts=["Shared"]),
            _analysis(concepts=["Other"]),
            _analysis(concepts=["Shared"]),
        ]
        inv = _ingest_chunks._build_gen_inventory(metas, analyses)
        self.assertEqual(inv["shared"], 0)

    def test_entities_included(self):
        metas = [_meta(0), _meta(1)]
        analyses = [
            _analysis(concepts=["Concept One"]),
            _analysis(entities=["Entity Two"]),
        ]
        inv = _ingest_chunks._build_gen_inventory(metas, analyses)
        self.assertEqual(inv["concept-one"], 0)
        self.assertEqual(inv["entity-two"], 1)

    def test_authoritative_schema_candidates_included(self):
        schema = """## Page Types

| type | directory |
|------|-----------|
| concept | wiki/concepts/ |
| finding | wiki/findings/ |
"""
        metas = [_meta(0), _meta(1), _meta(2)]
        analyses = [
            {
                **_analysis(concepts=["Concept One"]),
                "schema_typed_candidates": [{
                    "type": "finding",
                    "name": "Measured Result",
                }],
            },
            {
                **_analysis(),
                "schema_typed_candidates": [{
                    "type": "finding",
                    "name": "Measured Result",
                }],
            },
            {
                **_analysis(),
                "schema_typed_candidates": [{
                    "type": "unknown",
                    "name": "Do Not Inventory",
                }],
            },
        ]
        inv = _ingest_chunks._build_gen_inventory(metas, analyses, schema)
        self.assertEqual(inv["measured-result"], 0)
        self.assertNotIn("do-not-inventory", inv)

    def test_schema_candidate_outranks_earlier_generic_page(self):
        schema = """## Page Types

| type | directory |
|------|-----------|
| concept | wiki/concepts/ |
| finding | wiki/findings/ |
"""
        metas = [_meta(0), _meta(1)]
        analyses = [
            _analysis(concepts=["Measured Result"]),
            {
                **_analysis(),
                "schema_typed_candidates": [{
                    "type": "finding",
                    "name": "Measured Result",
                }],
            },
        ]
        inv = _ingest_chunks._build_gen_inventory(metas, analyses, schema)
        self.assertEqual(inv["measured-result"], 1)

    def test_blank_names_skipped(self):
        metas = [_meta(0)]
        analyses = [_analysis(concepts=["Real", "", "   "], entities=[""])]
        inv = _ingest_chunks._build_gen_inventory(metas, analyses)
        self.assertEqual(set(inv), {"real"})

    def test_uses_chunk_index_from_meta_not_position(self):
        # owner is meta[0], not the loop position.
        metas = [_meta(5), _meta(9)]
        analyses = [_analysis(concepts=["A"]), _analysis(concepts=["B"])]
        inv = _ingest_chunks._build_gen_inventory(metas, analyses)
        self.assertEqual(inv["a"], 5)
        self.assertEqual(inv["b"], 9)

    def test_rejects_string_name_shorthand_from_cached_analysis(self):
        """Live Fitzgerald regression: malformed YAML must never be masked."""
        metas = [_meta(0)]
        analyses = [{
            "concepts_found": [
                'name: "Magnetic Circuit Analysis"',
                "Plain Legacy Concept",
            ],
            "entities_found": ["name: 'A. E. Fitzgerald'"],
        }]
        with self.assertRaisesRegex(RuntimeError, "Unvalidated"):
            _ingest_chunks._build_gen_inventory(metas, analyses)

    def test_rejects_non_mapping_inventory_items(self):
        metas = [_meta(0), _meta(1)]
        analyses = [
            {
                "concepts_found": [None, 7, ["nested"], {"name": "Valid"}],
                "entities_found": "not-a-sequence",
            },
            "not-an-analysis",
        ]
        with self.assertRaisesRegex(RuntimeError, "Unvalidated"):
            _ingest_chunks._build_gen_inventory(metas, analyses)


class TestOtherChunkSlugs(unittest.TestCase):
    def test_other_slugs_excludes_own_sorted(self):
        metas = [_meta(0), _meta(1), _meta(2)]
        analyses = [
            _analysis(concepts=["Alpha", "Beta"]),
            _analysis(concepts=["Gamma"]),
            _analysis(entities=["Delta"]),
        ]
        inv = _ingest_chunks._build_gen_inventory(metas, analyses)
        # chunk 1 owns gamma → its other-slugs are everything else, sorted.
        self.assertEqual(
            _ingest_chunks._other_chunk_slugs(inv, 1),
            sorted(["alpha", "beta", "delta"]),
        )
        # chunk 0 owns alpha+beta.
        self.assertEqual(
            _ingest_chunks._other_chunk_slugs(inv, 0),
            sorted(["gamma", "delta"]),
        )

    def test_determinism(self):
        metas = [_meta(0), _meta(1)]
        analyses = [
            _analysis(concepts=["Zeta", "Alpha"]),
            _analysis(concepts=["Mu"]),
        ]
        inv = _ingest_chunks._build_gen_inventory(metas, analyses)
        a = _ingest_chunks._other_chunk_slugs(inv, 0)
        b = _ingest_chunks._other_chunk_slugs(inv, 0)
        self.assertEqual(a, b)
        self.assertEqual(a, sorted(a))


class _FlagEnv:
    """Context manager to set/unset IMPROVED_WIKI_PARALLEL_GEN."""

    def __init__(self, value):
        self.value = value
        self._prev = None

    def __enter__(self):
        import os
        self._prev = os.environ.get("IMPROVED_WIKI_PARALLEL_GEN")
        if self.value is None:
            os.environ.pop("IMPROVED_WIKI_PARALLEL_GEN", None)
        else:
            os.environ["IMPROVED_WIKI_PARALLEL_GEN"] = self.value
        return self

    def __exit__(self, *exc):
        import os
        if self._prev is None:
            os.environ.pop("IMPROVED_WIKI_PARALLEL_GEN", None)
        else:
            os.environ["IMPROVED_WIKI_PARALLEL_GEN"] = self._prev
        return False


class TestUnsetDefaultsToParallel(unittest.TestCase):
    """Unset env var (the new default, 2026-07-09): eager inventory + drain,
    same as explicit ``IMPROVED_WIKI_PARALLEL_GEN=1``."""

    def test_unset_env_var_uses_eager_inventory_not_accumulation(self):
        metas = [_meta(0), _meta(1), _meta(2)]
        analyses = [
            _analysis(concepts=["A0"]),
            _analysis(concepts=["A1"]),
            _analysis(concepts=["A2"]),
        ]
        seen_generated_slugs = {}

        def fake_gen(analysis, chunk_idx, generated_slugs, *a, **kw):
            seen_generated_slugs[chunk_idx] = list(generated_slugs)
            name = analysis["concepts_found"][0]["name"]
            return [(f"concepts/{name.lower()}.md", "body")]

        orig = _ingest_chunks.stage_2_4_generate_chunk
        _ingest_chunks.stage_2_4_generate_chunk = fake_gen
        try:
            with _FlagEnv(None):
                blocks, slugs, stop = _ingest_chunks._generate_all_chunks(
                    metas, analyses, {}, Path("raw.txt"), object(), "",
                    chunk_total=3, t_start=0.0, verbose=False)
        finally:
            _ingest_chunks.stage_2_4_generate_chunk = orig

        # Eager inventory: EVERY chunk is told about every OTHER chunk's slug
        # up front — chunk 0 already knows about a1/a2, not accumulation-empty.
        self.assertEqual(seen_generated_slugs[0], ["a1", "a2"])
        self.assertEqual(seen_generated_slugs[2], ["a0", "a1"])
        self.assertEqual(sorted(slugs), ["a0", "a1", "a2"])
        self.assertIsNone(stop)


class TestExplicitOptOutSerial(unittest.TestCase):
    """Explicit opt-out (0/false/no/off): chunk i is fed the slugs PRODUCED by
    prior chunks (accumulation), NOT the full eager inventory."""

    def test_serial_accumulation(self):
        metas = [_meta(0), _meta(1), _meta(2)]
        analyses = [
            _analysis(concepts=["A0"]),
            _analysis(concepts=["A1"]),
            _analysis(concepts=["A2"]),
        ]
        seen_generated_slugs = {}

        def fake_gen(analysis, chunk_idx, generated_slugs, *a, **kw):
            # snapshot what this chunk was told was already generated
            seen_generated_slugs[chunk_idx] = list(generated_slugs)
            name = analysis["concepts_found"][0]["name"]
            return [(f"concepts/{name.lower()}.md", "body")]

        orig = _ingest_chunks.stage_2_4_generate_chunk
        _ingest_chunks.stage_2_4_generate_chunk = fake_gen
        try:
            with _FlagEnv("0"):
                blocks, slugs, stop = _ingest_chunks._generate_all_chunks(
                    metas, analyses, {}, Path("raw.txt"), object(), "",
                    chunk_total=3, t_start=0.0, verbose=False)
        finally:
            _ingest_chunks.stage_2_4_generate_chunk = orig

        # chunk 0 saw nothing; chunk 2 saw chunk-0's + chunk-1's PRODUCED slugs.
        self.assertEqual(seen_generated_slugs[0], [])
        self.assertEqual(seen_generated_slugs[2], ["a0", "a1"])
        # NOT the full inventory (which would also contain a2).
        self.assertNotIn("a2", seen_generated_slugs[2])
        self.assertEqual(slugs, ["a0", "a1", "a2"])
        self.assertIsNone(stop)

    def test_other_opt_out_spellings(self):
        for spelling in ("false", "No", "OFF"):
            with self.subTest(spelling=spelling):
                with _FlagEnv(spelling):
                    self.assertFalse(_ingest_chunks._parallel_gen_enabled())


class TestFlagOnDrain(unittest.TestCase):
    """Flag ON: eager inventory + drain."""

    def _run(self, uncached: set):
        metas = [_meta(0), _meta(1), _meta(2)]
        analyses = [
            _analysis(concepts=["A0"]),
            _analysis(concepts=["A1"]),
            _analysis(concepts=["A2"]),
        ]
        calls = {"n": 0, "other_slugs": {}}

        def fake_gen(analysis, chunk_idx, generated_slugs, *a, **kw):
            calls["n"] += 1
            calls["other_slugs"][chunk_idx] = list(generated_slugs)
            if chunk_idx in uncached:
                raise _core.ConversationPending()
            name = analysis["concepts_found"][0]["name"]
            return [(f"concepts/{name.lower()}.md", "body")]

        orig = _ingest_chunks.stage_2_4_generate_chunk
        _ingest_chunks.stage_2_4_generate_chunk = fake_gen
        try:
            with _FlagEnv("1"):
                result = _ingest_chunks._generate_all_chunks(
                    metas, analyses, {}, Path("raw.txt"), object(), "",
                    chunk_total=3, t_start=0.0, verbose=False)
            return result, calls
        finally:
            _ingest_chunks.stage_2_4_generate_chunk = orig

    def test_two_uncached_raises_once_after_all_chunks(self):
        with self.assertRaises(_core.ConversationPending):
            self._run(uncached={0, 2})

    def test_attempts_all_chunks_before_raising(self):
        # chunk 0 raises first, but the drain loop must still attempt 1 and 2.
        with self.assertRaises(_core.ConversationPending):
            _, calls = self._run(uncached={0, 2})
            self.assertEqual(calls["n"], 3)
        # _run re-raises before returning, so re-run to inspect call count.
        seen = {"n": 0}
        metas = [_meta(0), _meta(1), _meta(2)]
        analyses = [
            _analysis(concepts=["A0"]),
            _analysis(concepts=["A1"]),
            _analysis(concepts=["A2"]),
        ]

        def fake_gen(analysis, chunk_idx, generated_slugs, *a, **kw):
            seen["n"] += 1
            if chunk_idx in (0, 2):
                raise _core.ConversationPending()
            return [("concepts/a1.md", "body")]

        orig = _ingest_chunks.stage_2_4_generate_chunk
        _ingest_chunks.stage_2_4_generate_chunk = fake_gen
        try:
            with _FlagEnv("1"):
                with self.assertRaises(_core.ConversationPending):
                    _ingest_chunks._generate_all_chunks(
                        metas, analyses, {}, Path("raw.txt"), object(), "",
                        chunk_total=3, t_start=0.0, verbose=False)
        finally:
            _ingest_chunks.stage_2_4_generate_chunk = orig
        self.assertEqual(seen["n"], 3)

    def test_zero_uncached_returns_union(self):
        (blocks, slugs, stop), calls = self._run(uncached=set())
        self.assertEqual(calls["n"], 3)
        self.assertIsNone(stop)
        # union of all chunks' produced slugs.
        self.assertEqual(sorted(slugs), ["a0", "a1", "a2"])
        self.assertEqual(len(blocks), 3)

    def test_each_chunk_fed_other_chunk_inventory(self):
        # With 0 uncached, verify chunk i is told to skip OTHER chunks' slugs.
        (_b, _s, _stop), calls = self._run(uncached=set())
        self.assertEqual(calls["other_slugs"][0], ["a1", "a2"])
        self.assertEqual(calls["other_slugs"][1], ["a0", "a2"])
        self.assertEqual(calls["other_slugs"][2], ["a0", "a1"])

    def test_parallel_limit_emits_one_bounded_wave_not_serial(self):
        metas = [_meta(i) for i in range(6)]
        analyses = [
            _analysis(concepts=[f"A{i}"]) for i in range(6)
        ]
        seen: list[int] = []

        class Config:
            handoff_parallel_limit = 2

        def fake_gen(_analysis_value, chunk_idx, _slugs, *args, **kwargs):
            seen.append(chunk_idx)
            raise _core.ConversationPending()

        orig = _ingest_chunks.stage_2_4_generate_chunk
        _ingest_chunks.stage_2_4_generate_chunk = fake_gen
        try:
            with _FlagEnv("1"):
                with self.assertRaises(_core.ConversationPending):
                    _ingest_chunks._generate_all_chunks(
                        metas,
                        analyses,
                        {},
                        Path("raw.txt"),
                        Config(),
                        "",
                        chunk_total=6,
                        t_start=0.0,
                        verbose=False,
                    )
        finally:
            _ingest_chunks.stage_2_4_generate_chunk = orig

        # Still parallel: two independent prompts are emitted in this wave.
        # The remaining four wait for later re-invocations.
        self.assertEqual(seen, [0, 1])


if __name__ == "__main__":
    unittest.main()
