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
