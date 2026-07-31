"""The per-page write ledger is what makes fast path 5 safe.

Conversation mode leaves the write loop on every merge handoff, so the resumed
loop replays FILE blocks it already merged. The ledger records, per page, the
sha256 of the exact incoming block, keyed by a fingerprint of the whole block
set. That gives two properties this module pins down:

  * a replay of the same block set skips the LLM merge (the reason fast path 5
    exists at all — without it the merge prompt hash changes on every pass and
    the page re-merges forever, bug 2026-06-25);
  * a re-ingest, which regenerates the blocks, invalidates the ledger, so a
    corrected source's new body always reaches the merger (bug 2026-07-29 —
    the previous `sources:` superset heuristic could not tell the two apart).

Stdlib unittest only — no pytest, no network, no LLM calls.
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
import _ingest_write as iw  # noqa: E402


def _make_config(tmp: Path) -> _core.Config:
    return _core.Config(
        wiki_root=tmp,
        raw_root=tmp / "raw",
        wiki_dir=tmp / "wiki",
        runtime_dir=tmp / "rt",
        cache_path=tmp / "rt" / "ingest-cache.json",
        progress_dir=tmp / "rt" / "ingest-progress",
        extract_tmp_dir=tmp / "rt" / "extract-tmp",
        llm_model="m",
        caption_api_key="",
        caption_base_url="x",
        caption_model="c",
        chunk_overlap=3000,
        source_budget=100000,
        target_chars=60000,
        target_tokens=30000,
        max_tokens=8192,
        conversation_prefix="ab12cd34",
    )


BLOCKS = [
    ("concepts/alpha.md", "alpha body"),
    ("concepts/beta.md", "beta body"),
]


class TestWriteLedger(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.cfg = _make_config(self.tmp)
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.h = "a" * 64

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_absent_ledger_reads_empty(self):
        fp = iw._blocks_fingerprint(BLOCKS)
        self.assertEqual({}, iw._load_write_ledger(self.cfg, self.h, fp))

    def test_replay_of_same_block_set_is_recognized(self):
        fp = iw._blocks_fingerprint(BLOCKS)
        iw._record_write_ledger(
            self.cfg, self.h, fp, {"concepts/alpha.md": "sha-of-alpha"})

        # A later pass over the identical block set sees the recorded page.
        self.assertEqual(
            {"concepts/alpha.md": "sha-of-alpha"},
            iw._load_write_ledger(self.cfg, self.h, fp),
        )

    def test_regenerated_blocks_invalidate_the_ledger(self):
        """A re-ingest produces different block content → different
        fingerprint → the ledger must not suppress the real merge."""
        old_fp = iw._blocks_fingerprint(BLOCKS)
        iw._record_write_ledger(
            self.cfg, self.h, old_fp, {"concepts/alpha.md": "sha-of-alpha"})

        regenerated = [
            ("concepts/alpha.md", "alpha body, corrected by a re-ingest"),
            ("concepts/beta.md", "beta body"),
        ]
        new_fp = iw._blocks_fingerprint(regenerated)
        self.assertNotEqual(old_fp, new_fp)
        self.assertEqual({}, iw._load_write_ledger(self.cfg, self.h, new_fp))

    def test_fingerprint_is_order_and_path_sensitive(self):
        self.assertNotEqual(
            iw._blocks_fingerprint(BLOCKS),
            iw._blocks_fingerprint(list(reversed(BLOCKS))),
        )
        self.assertNotEqual(
            iw._blocks_fingerprint(BLOCKS),
            iw._blocks_fingerprint(
                [("concepts/alpha.md", "alpha body"),
                 ("entities/beta.md", "beta body")]),
        )

    def test_corrupt_ledger_degrades_to_re_merging(self):
        fp = iw._blocks_fingerprint(BLOCKS)
        iw._write_ledger_path(self.cfg, self.h).write_text(
            "{not json", encoding="utf-8")
        self.assertEqual({}, iw._load_write_ledger(self.cfg, self.h, fp))

    def test_clear_removes_the_ledger(self):
        fp = iw._blocks_fingerprint(BLOCKS)
        iw._record_write_ledger(self.cfg, self.h, fp, {"concepts/alpha.md": "x"})
        self.assertTrue(iw._write_ledger_path(self.cfg, self.h).exists())

        iw._clear_write_ledger(self.cfg, self.h)
        self.assertFalse(iw._write_ledger_path(self.cfg, self.h).exists())
        # Idempotent: a second clear on a finished source must not raise.
        iw._clear_write_ledger(self.cfg, self.h)


if __name__ == "__main__":
    unittest.main()
