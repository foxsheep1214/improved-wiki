"""Tests for the model-namespaced probe prefix + two-layer cache clear.

Regression guard for the 2026-06-29 fix: deleting probed-context.json alone did
NOT force a re-probe (the conversation router replays the cached answer), and a
shared ctxprobe dir replayed a prior model's answer on model change.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _context_probe as cp  # noqa: E402


class _Cfg:
    def __init__(self, runtime_dir, llm_model):
        self.runtime_dir = runtime_dir
        self.llm_model = llm_model


def test_probe_prefix_is_model_namespaced():
    # Same model → same prefix (cache reused); different model → different prefix
    # (fresh probe). Path-unsafe chars sanitized.
    assert cp._probe_prefix("glm-5.2") == "ctxprobe-glm-5.2"
    assert cp._probe_prefix("anthropic/claude-opus-4-8") == "ctxprobe-anthropic-claude-opus-4-8"
    assert cp._probe_prefix("glm-5.2") != cp._probe_prefix("claude-opus-4-8")


def test_probe_prefix_empty_model_is_stable():
    assert cp._probe_prefix("") == "ctxprobe-unknown"
    assert cp._probe_prefix(None) == "ctxprobe-unknown"


def test_clear_probe_cache_removes_both_layers(tmp_path):
    rt = tmp_path / ".llm-wiki"
    probe_dir = rt / "conversation" / "ctxprobe-glm-5.2"
    probe_dir.mkdir(parents=True)
    (probe_dir / "LLM-task-abc.txt").write_text("1000000", encoding="utf-8")
    (rt / "probed-context.json").write_text(
        '{"model":"glm-5.2","context":1000000,"probed_at":0}', encoding="utf-8"
    )

    cp.clear_probe_cache(_Cfg(rt, "glm-5.2"))

    assert not (rt / "probed-context.json").exists()
    assert not probe_dir.exists()


def test_clear_probe_cache_noop_when_absent(tmp_path):
    rt = tmp_path / ".llm-wiki"
    rt.mkdir(parents=True)
    # Must not raise when neither cache layer exists.
    cp.clear_probe_cache(_Cfg(rt, "glm-5.2"))


# ── Self-report parsing + env-trust (don't only trust env model name) ──

def test_parse_probe_two_line_identity_and_context():
    assert cp._parse_probe("glm-5.2\n1000000") == ("glm-5.2", 1000000)
    assert cp._parse_probe("claude-opus-4-8\n200,000") == ("claude-opus-4-8", 200000)


def test_parse_probe_number_only_has_no_identity():
    model_self, ctx = cp._parse_probe("200000")
    assert ctx == 200000 and model_self is None


def test_identities_match_normalizes_and_detects_mismatch():
    assert cp._identities_match("GLM-5.2", "glm-5.2") is True       # case/punct insensitive
    assert cp._identities_match("claude-opus-4-8", "glm-5.2") is False
    assert cp._identities_match("", "glm-5.2") is None              # unknown → don't penalize


def _write_cache(rt, payload):
    import json
    rt.mkdir(parents=True, exist_ok=True)
    (rt / "probed-context.json").write_text(json.dumps(payload), encoding="utf-8")


def test_load_cached_reuses_even_when_env_unreliable(tmp_path):
    # env_reliable=False is INFORMATIONAL (warning only) — the live-probed value is
    # still correct and reused. Blocking would stall multi-handoff ingests (a probe
    # handoff before every stage). Staleness is bounded by the TTL instead.
    rt = tmp_path / ".llm-wiki"
    _write_cache(rt, {"model_env": "glm-5.2", "model_self": "claude-opus-4-8",
                      "env_reliable": False, "context": 1000000, "probed_at": int(time.time())})
    assert cp.load_cached(_Cfg(rt, "glm-5.2")) == 1000000


def test_load_cached_backward_compatible_with_old_schema(tmp_path):
    # Old {model, context, probed_at} entries (no model_self/env_reliable) still reuse.
    rt = tmp_path / ".llm-wiki"
    _write_cache(rt, {"model": "glm-5.2", "context": 200000, "probed_at": int(time.time())})
    assert cp.load_cached(_Cfg(rt, "glm-5.2")) == 200000


import time  # noqa: E402  (used by cache-payload timestamps above)


# ── Known-model validation (pin recognized models to authoritative spec) ──

def test_known_context_matches_normalized_and_proxy_prefixed():
    # case/punct insensitive + substring tolerance (proxy "anthropic/" prefix)
    assert cp._known_context("Claude-Opus-4-8") == 1_000_000
    assert cp._known_context("anthropic/claude-opus-4-8") == 1_000_000
    assert cp._known_context("claude-haiku-4-5") == 200_000
    assert cp._known_context("glm-5.2") == 1_000_000


def test_known_context_unknown_or_missing_returns_none():
    assert cp._known_context("deepseek-v4") is None
    assert cp._known_context("") is None
    assert cp._known_context(None) is None


def test_known_model_corrects_lowballed_self_report(monkeypatch, tmp_path):
    # A cautious opus-4-8 answering 200000 (the probe prompt invites a "confident"
    # lowball) must be pinned UP to its real 1M spec, not sized into tiny chunks.
    rt = tmp_path / ".llm-wiki"
    cfg = _Cfg(rt, "claude-opus-4-8")
    cfg.conversation_prefix = "x"
    import _llm_api  # noqa: E402
    monkeypatch.setattr(_llm_api, "call_anthropic_protocol",
                        lambda *a, **k: ("claude-opus-4-8\n200000", None))
    assert cp.probe_context(cfg) == 1_000_000
    import json
    assert json.loads((rt / "probed-context.json").read_text())["context"] == 1_000_000


def test_unknown_model_keeps_self_report(monkeypatch, tmp_path):
    # Unknown models are NOT pinned — their in-range self-report is trusted as-is.
    rt = tmp_path / ".llm-wiki"
    cfg = _Cfg(rt, "deepseek-v4")
    cfg.conversation_prefix = "x"
    import _llm_api  # noqa: E402
    monkeypatch.setattr(_llm_api, "call_anthropic_protocol",
                        lambda *a, **k: ("deepseek-v4\n524288", None))
    assert cp.probe_context(cfg) == 524288
