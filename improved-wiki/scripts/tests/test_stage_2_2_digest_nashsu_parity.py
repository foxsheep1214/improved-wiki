"""Stage 2.2 rolling-digest transfer: NashSU parity (2026-07-09 user decision).

History: the accumulated_digest fed from chunk N to chunk N+1 was capped at a
fixed 24,000 chars, which a dense 5-chunk book overflowed (36K by chunk 3),
truncating mid-word. An interim fix made the cap dynamic (config.target_chars,
~256K on a 1M-context model). The user then ruled that the chunk-to-chunk
transfer volume and granularity must MATCH NashSU instead:

  - NashSU ingest.ts: `LONG_SOURCE_DIGEST_MAX = 15_000` — a FIXED cap,
    deliberately NOT scaled to the model context (chunk size is scaled;
    the digest is not), applied via trimLongText() at prompt insertion.
  - NashSU's digest instruction asks for "A compact document-level digest"
    (structured name/one-line lists), NOT verbatim accumulation of every
    prior entry. Detail lives in the per-chunk analyses, which NashSU (and
    improved-wiki, via chunk_analyses / chunk_claims) passes downstream
    separately.

So: cap fixed at 15,000 regardless of target_chars, and the prompt template
instructs a compact digest (condense prior entries; names must survive, prose
need not).

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


def _build(cfg, digest: str) -> str:
    return s2._stage_2_2_build_prompt(
        chunk_text="chunk body text",
        chunk_index=3, chunk_total=5,
        global_digest={}, file_path=Path("book.pdf"), config=cfg,
        accumulated_digest=digest,
    )


class DigestTransferIsNashSUParity(unittest.TestCase):
    def test_cap_is_fixed_15k_even_with_huge_target_chars(self):
        """NashSU deliberately does NOT scale the digest cap with model
        context (LONG_SOURCE_DIGEST_MAX is a constant). A marker at ~20K
        must be cut even when target_chars is generous."""
        with tempfile.TemporaryDirectory() as t:
            cfg = _cfg(Path(t), target_chars=256_000)
            digest = ("x" * 20_000) + "ZZZPAST15K"
            prompt = _build(cfg, digest)
            self.assertNotIn("ZZZPAST15K", prompt)

    def test_digest_under_15k_passes_through_intact(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = _cfg(Path(t), target_chars=256_000)
            digest = ("y" * 14_000) + "ZZZUNDER15K"
            prompt = _build(cfg, digest)
            self.assertIn("ZZZUNDER15K", prompt)

    def test_truncation_is_marked_not_silent(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = _cfg(Path(t), target_chars=256_000)
            prompt = _build(cfg, "z" * 20_000)
            self.assertIn("truncated", prompt)

    def test_prompt_instructs_stable_names(self):
        """NashSU's system prompt (ingest.ts:2543) says 'Keep stable names
        consistent with the existing wiki and prior digest' — the load-bearing
        purpose of the name ledger (stable name → stable slug → 2.4 dedup).
        The compact-ledger design is incoherent without it."""
        with tempfile.TemporaryDirectory() as t:
            cfg = _cfg(Path(t), target_chars=256_000)
            prompt = _build(cfg, "prior digest")
            self.assertIn("stable names", prompt.lower())

    def test_prompt_instructs_gist_compression_under_budget_pressure(self):
        """A dense book's ledger hits the 15K cap mid-book (observed live:
        14,828/15,000 at chunk 2/5 on Hansen, 2026-07-09). The template must
        tell the model HOW to fit: drop older gists to bare names, never drop
        names — otherwise each answering agent improvises its own policy."""
        with tempfile.TemporaryDirectory() as t:
            cfg = _cfg(Path(t), target_chars=256_000)
            prompt = _build(cfg, "prior digest")
            self.assertIn("bare", prompt.lower())
            self.assertIn("non-negotiable", prompt.lower())

    def test_prompt_instructs_compact_digest_not_verbatim_accumulation(self):
        """The digest instruction must follow NashSU's 'compact document-level
        digest' philosophy: condense prior entries, don't demand verbatim
        keep-everything (which forces unbounded growth into the fixed cap)."""
        with tempfile.TemporaryDirectory() as t:
            cfg = _cfg(Path(t), target_chars=256_000)
            prompt = _build(cfg, "prior digest")
            self.assertIn("compact", prompt.lower())
            self.assertNotIn("keep everything from prior chunks", prompt)


if __name__ == "__main__":
    unittest.main()
