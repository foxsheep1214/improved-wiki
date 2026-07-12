"""Regression test for the 2026-07-09 batch-mode conversation_prefix bug.

ingest_one() (single-file path) sets config.conversation_prefix = h[-8:] right
after computing the file hash, so each book's LLM prompt/response files land in
their own conversation/<hash-suffix>/ directory (per delegate-mode.md: "Multiple
simultaneous ingests are safe — each has a unique conversation directory").

batch_ingest() (the multi-file path, used for every batch ingest — e.g. `ingest.py
a.pdf b.pdf c.pdf`) never set conversation_prefix at all. It stayed at the Config
default (""), so _conversation_router.py's `config.conversation_prefix or
"00000000"` fallback fired for every book, in every batch ingest ever run,
dumping every book's Stage 2.2+ prompts/answers and its tasks.json pending-task
manifest into one shared "00000000" directory — cross-book file/task
cross-contamination, discovered when a live re-ingest's qc_stage22.py scan came
back full of unrelated books' stale content.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _core  # noqa: E402
import ingest  # noqa: E402


def _make_config(tmp: Path) -> _core.Config:
    return _core.Config(
        wiki_root=tmp / "wiki", raw_root=tmp / "raw",
        wiki_dir=tmp / "wiki", runtime_dir=tmp / "rt",
        cache_path=tmp / "rt" / "ingest-cache.json",
        progress_dir=tmp / "rt" / "ingest-progress",
        extract_tmp_dir=tmp / "rt" / "extract-tmp",
        llm_model="m",
        caption_api_key="", caption_base_url="x",
        caption_model="c", chunk_overlap=3000,
        source_budget=100000, target_chars=60000, target_tokens=30000,
        max_tokens=8192, conversation_prefix="",
    )


class TestBatchIngestConversationPrefix(unittest.TestCase):
    def test_batch_ingest_sets_per_book_prefix_before_prepare(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_config(tmp)
            raw1 = tmp / "raw" / "Book" / "a.pdf"
            raw2 = tmp / "raw" / "Book" / "b.pdf"
            raw1.parent.mkdir(parents=True, exist_ok=True)
            raw1.write_bytes(b"%PDF-1.4 fake a")
            raw2.write_bytes(b"%PDF-1.4 fake b")
            h1 = _core.file_sha256(raw1)[-8:]
            h2 = _core.file_sha256(raw2)[-8:]

            seen_prefixes = []
            orig_is_stage_done = ingest.is_stage_done
            orig_do_prepare = ingest._do_prepare

            # stage_1_3_done "True" skips the bg-extract wait/launch entirely —
            # the bug under test is upstream of that, in the per-book prefix
            # assignment, so extraction machinery is irrelevant here.
            ingest.is_stage_done = lambda cfg_, h_, stage: stage == "stage_1_3_done"

            def _fake_prepare(f, cfg_, template_override, verbose, *rest):
                seen_prefixes.append(cfg_.conversation_prefix)
                if rest:  # prefetch call (analyze_only=True) — 5th positional arg
                    raise ingest.PrepareStopAfter("1.5")
                return None  # spine call: pretend already complete, skip _do_write

            ingest._do_prepare = _fake_prepare
            try:
                ingest.batch_ingest([raw1, raw2], cfg)
            finally:
                ingest.is_stage_done = orig_is_stage_done
                ingest._do_prepare = orig_do_prepare

            # Each book's prefetch + spine _do_prepare calls must see THAT book's
            # own prefix — never "" (which would fall through to the shared
            # "00000000" bucket in _conversation_router.py).
            self.assertNotIn("", seen_prefixes)
            self.assertEqual(seen_prefixes, [h1, h1, h2, h2])


if __name__ == "__main__":
    unittest.main()
