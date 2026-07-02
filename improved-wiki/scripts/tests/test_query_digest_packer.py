"""Stage 2.7 digest packing: structure-aware trim, never a raw slice.

The old code did `digest_str[:12000] + "... (truncated)"` — a mid-JSON cut
(observed live truncating mid-claim). The packer serializes keys whole in
priority order (book_meta, outline, key_claims, key_entities, key_concepts)
within the char budget; a key that does not fit is skipped and reported in a
one-line trailing note instead of being cut mid-structure.

Stdlib unittest only.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _stage_2_7_query_generation as q  # noqa: E402


def _digest(n_concepts=3, concept_pad=""):
    return {
        "book_meta": {"title": "示例书", "author": "某人"},
        "outline": ["第一章 概述", "第二章 原理"],
        "key_claims": [{"claim": "claim one"}, {"claim": "claim two"}],
        "key_entities": ["MC6840", "AD667"],
        "key_concepts": [f"concept-{i}{concept_pad}" for i in range(n_concepts)],
    }


class TestSmallDigestPassesThrough(unittest.TestCase):
    def test_small_digest_complete_and_parseable(self):
        digest = _digest()
        packed = q._stage_2_7_pack_digest(digest)
        self.assertEqual(json.loads(packed), digest)
        self.assertNotIn("omitted keys", packed)

    def test_priority_key_order(self):
        packed = q._stage_2_7_pack_digest(_digest())
        positions = [packed.index(f'"{k}"') for k in q._STAGE_2_7_DIGEST_KEY_PRIORITY]
        self.assertEqual(positions, sorted(positions))


class TestOversizedKeyOmitted(unittest.TestCase):
    def test_huge_key_concepts_omitted_with_note_others_whole(self):
        digest = _digest(n_concepts=50, concept_pad="x" * 1000)  # ~50K chars
        packed = q._stage_2_7_pack_digest(digest)

        note_idx = packed.rindex("\n...")
        parsed = json.loads(packed[:note_idx])
        self.assertEqual(parsed["book_meta"], digest["book_meta"])
        self.assertEqual(parsed["outline"], digest["outline"])
        self.assertEqual(parsed["key_claims"], digest["key_claims"])
        self.assertEqual(parsed["key_entities"], digest["key_entities"])
        self.assertNotIn("key_concepts", parsed)

        self.assertIn("...(omitted keys: key_concepts (50 items))", packed)
        self.assertLessEqual(note_idx, q._STAGE_2_7_DIGEST_CHAR_BUDGET)

    def test_never_cuts_mid_structure(self):
        # Every included key round-trips; nothing is a prefix slice.
        digest = _digest(n_concepts=200, concept_pad="y" * 300)
        packed = q._stage_2_7_pack_digest(digest, budget=2000)
        json_part = packed.split("\n...(omitted keys:")[0]
        parsed = json.loads(json_part)  # raises if any structure was cut
        for key, value in parsed.items():
            self.assertEqual(value, digest[key])


if __name__ == "__main__":
    unittest.main()
