"""Stage 2.1 digest no longer requires the dead `chunk_plan` key.

chunk_plan was asked of the LLM and required by the validator, but nothing ever
consumed it: only book_meta/outline/key_entities/key_concepts are forwarded
downstream and chunking is pure char-count (_stage_2_1_chunk_text). Removed the
wasted output; a digest without chunk_plan must now pass verification.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _stage_validators as v  # noqa: E402


_DIGEST = {
    "book_meta": {"title": "X"},
    "outline": [{"chapter": 1, "title": "Intro"}],
    "key_entities": [{"name": "E"}],
    "key_concepts": [{"name": "C", "importance": "core"}],
    "key_claims": [{"claim": "c"}],
}


class DigestNoChunkPlan(unittest.TestCase):
    def test_digest_without_chunk_plan_passes(self):
        # Must not raise even though chunk_plan is absent.
        v._verify_stage_2_1_digest(dict(_DIGEST), Path("book.pdf"))

    def test_still_fails_on_a_genuinely_required_key(self):
        broken = dict(_DIGEST)
        del broken["key_concepts"]
        with self.assertRaises(RuntimeError):
            v._verify_stage_2_1_digest(broken, Path("book.pdf"))


if __name__ == "__main__":
    unittest.main()
