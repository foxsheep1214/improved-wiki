"""Pin improved-wiki's Phase 3 orchestration to NashSU 0.6.6 order."""
from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _ingest_write as ingest_write  # noqa: E402


class TestPhase3NashsuOrder(unittest.TestCase):
    def test_phase3_calls_follow_nashsu_order(self):
        source = inspect.getsource(ingest_write._do_write)
        ordered_calls = [
            "stage_3_4_prepare_review_suggestions(",
            "stage_3_1_write_wiki_file(",
            "stage_3_5_aggregate_repair(",
            "stage_3_2_inject_images(",
            "stage_3_4_persist_review_suggestions(",
            "save_cache(",
        ]
        positions = [source.index(call) for call in ordered_calls]
        self.assertEqual(
            positions,
            sorted(positions),
            f"Phase 3 calls are out of NashSU order: {ordered_calls}",
        )


if __name__ == "__main__":
    unittest.main()
