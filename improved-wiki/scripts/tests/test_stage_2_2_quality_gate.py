"""Stage 2.2 C1 claim-quality hard gate (2026-07-08).

Regression: the C1 validators (source_quotes present, >=3 claims, >50% claims
with specific evidence anchors) were originally placed INSIDE the LLM-call
retry try/except. Their RuntimeErrors were swallowed by the except handler into
an {"error": ...} dict, so the gate never actually paused the ingest — the
agent could emit claim-less analyses and the pipeline happily continued.

Fix: the validators now run AFTER the retry loop exits successfully, so a
quality failure raises to the caller (_ingest_chunks has no try/except around
_stage_2_2_analyze_chunk, so it propagates and pauses the ingest).

Stdlib unittest only — call_anthropic_protocol is monkeypatched (no LLM/network).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _core  # noqa: E402
import _stage_2_analyze as s22  # noqa: E402


def _cfg(tmp: Path) -> _core.Config:
    return _core.Config(
        wiki_root=tmp, raw_root=tmp / "raw", wiki_dir=tmp / "wiki",
        runtime_dir=tmp / "rt", cache_path=tmp / "rt" / "c.json",
        progress_dir=tmp / "rt" / "p", extract_tmp_dir=tmp / "rt" / "e",
        llm_base_url="x", llm_model="m", llm_api_key="", llm_protocol="anthropic",
        caption_api_key="", caption_base_url="x", caption_model="c",
        chunk_size=60000, chunk_overlap=3000, source_budget=100000,
        target_chars=768000, target_tokens=192000, max_tokens=8192,
        conversation_prefix="ab12cd34",
    )


def _yaml(*, quotes: str, claims: list[dict]) -> str:
    import json as _j
    claims_yaml = "\n".join(
        f"  - claim: {c['claim']!r}\n"
        f"    evidence: {c['evidence']!r}\n"
        f"    confidence: {c.get('confidence', 'high')}"
        for c in claims
    )
    return (
        "```yaml\n"
        "chunk_index: 1\n"
        "chunk_total: 1\n"
        "concepts_found: []\n"
        "entities_found: []\n"
        f"source_quotes: {quotes!r}\n"
        f"claims:\n{claims_yaml}\n"
        'updated_global_digest: "cumulative digest longer than fifty chars xxxxx"\n'
        "```\n"
    )


_VALID_CLAIMS = [
    {"claim": "Barker-13 gives -1/N sidelobe", "evidence": "§2.3.4"},
    {"claim": "Matched filter maximizes SNR", "evidence": "式(3.6)"},
    {"claim": "Noise bandwidth equals signal bandwidth", "evidence": "Figure 4.2"},
]


class QualityGateHardGate(unittest.TestCase):
    """C1 validators must RAISE to the caller, not be swallowed into error dict."""

    def _run(self, yaml_response: str, heading_path: str = "第1章 概论") -> dict:
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            cfg = _cfg(Path(t))
            raw_file = Path(t) / "raw" / "book.pdf"

            def fake_call(prompt, config, **kw):
                return yaml_response, None

            orig = s22.call_anthropic_protocol
            s22.call_anthropic_protocol = fake_call
            try:
                return s22._stage_2_2_analyze_chunk(
                    "chunk body text", 0, 1, {}, "", "", heading_path,
                    raw_file, cfg, template="", max_retries=0, verbose=False)
            finally:
                s22.call_anthropic_protocol = orig

    def test_valid_analysis_passes_gate(self):
        result = self._run(_yaml(quotes="§2.3.4: quote", claims=_VALID_CLAIMS))
        self.assertNotIn("error", result)
        self.assertEqual(len(result["claims"]), 3)

    def test_raises_when_source_quotes_empty(self):
        with self.assertRaises(RuntimeError) as cm:
            self._run(_yaml(quotes="", claims=_VALID_CLAIMS))
        self.assertIn("source_quotes", str(cm.exception))

    def test_raises_when_too_few_claims(self):
        one = _VALID_CLAIMS[:1]
        with self.assertRaises(RuntimeError) as cm:
            self._run(_yaml(quotes="§2.3.4: quote", claims=one))
        self.assertIn("minimum 3", str(cm.exception))

    def test_raises_when_majority_of_claims_lack_anchors(self):
        generic = [
            {"claim": "a", "evidence": "Ch.3"},
            {"claim": "b", "evidence": "Ch.4"},
            {"claim": "c", "evidence": "Ch.5"},
        ]
        with self.assertRaises(RuntimeError) as cm:
            self._run(_yaml(quotes="§2.3.4: quote", claims=generic))
        self.assertIn("evidence anchors", str(cm.exception))


class FrontMatterExemption(unittest.TestCase):
    """Front-matter chunks (preface/TOC before chapter 1) get a relaxed gate:
    min 1 claim (not 3) and the evidence-anchor ratio gate is skipped — but
    source_quotes is still required."""

    def _run(self, yaml_response: str) -> dict:
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            cfg = _cfg(Path(t))
            raw_file = Path(t) / "raw" / "book.pdf"

            def fake_call(prompt, config, **kw):
                return yaml_response, None

            orig = s22.call_anthropic_protocol
            s22.call_anthropic_protocol = fake_call
            try:
                return s22._stage_2_2_analyze_chunk(
                    "preface body text", 0, 1, {}, "", "",
                    s22._FRONT_MATTER_LABEL,
                    raw_file, cfg, template="", max_retries=0, verbose=False)
            finally:
                s22.call_anthropic_protocol = orig

    def test_single_claim_without_anchor_passes(self):
        # 1 claim, generic evidence — would fail the normal anchor gate, but
        # front-matter chunks skip it.
        one = [{"claim": "book targets graduate students",
                "evidence": "Preface"}]
        result = self._run(_yaml(quotes="Preface: for graduate readers", claims=one))
        self.assertNotIn("error", result)
        self.assertEqual(len(result["claims"]), 1)

    def test_still_requires_source_quotes(self):
        one = [{"claim": "x", "evidence": "Preface"}]
        with self.assertRaises(RuntimeError) as cm:
            self._run(_yaml(quotes="", claims=one))
        self.assertIn("source_quotes", str(cm.exception))

    def test_zero_claims_still_raises(self):
        with self.assertRaises(RuntimeError) as cm:
            self._run(_yaml(quotes="Preface: a sentence", claims=[]))
        self.assertIn("minimum 1", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
