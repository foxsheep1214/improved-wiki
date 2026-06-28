"""Live context-window probe (conversation mode).

Replaces the LLM_CONTEXT_SIZE env convention: the context window is probed from
the live conversation model at ingest start, so chunk budgets adapt to whatever
model the agent runs this session — GLM-5.2 (1M) today, DeepSeek V4 Pro
tomorrow, … — with no static registry to maintain or mis-enter.

Probe = one conversation round-trip asking the model for its max context in
tokens. The router caches the answer by prompt hash, so a resume re-reads it
without a new round-trip. We additionally cache the parsed value per-model in
``.llm-wiki/probed-context.json`` (7-day TTL) so repeat ingests and resumes pay
zero round-trips; a model change triggers exactly one probe.

Reliability: the answer is sanity-gated to [8K, 10M]. The existing budget
reserves (15% response + 25% stable + 8% instruction ≈ 48%) already provide
headroom, so no extra margin is applied. On parse failure / out-of-range the
ingest PAUSES (no-silent-fallback policy) rather than guessing — see
references/context-probe.md for the hand-recovery path.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

# Dedicated conversation prefix so the probe is shared across all files in a
# batch and isolated from per-source (per-file-hash) conversation dirs. The
# prefix is MODEL-NAMESPACED (``ctxprobe-<model>``): the conversation router
# caches the probe Q&A independently of probed-context.json, so a shared
# ``ctxprobe`` dir would replay the prior model's answer when the model changes
# (making "a model change triggers one probe" false). Namespacing by model means
# a different model → different dir → fresh probe, while the same model stays
# cached (no re-probe loop). See clear_probe_cache() for the same-model force path.
_PROBE_PREFIX_BASE = "ctxprobe"


def _probe_prefix(model: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", (model or "unknown").strip()) or "unknown"
    return f"{_PROBE_PREFIX_BASE}-{safe}"

_PROBE_MIN = 8_000          # no real model answers below this
_PROBE_MAX = 10_000_000     # no current model exceeds this
_PROBE_CACHE_TTL = 7 * 24 * 3600   # re-probe after 7 days (model may upgrade)

_PROBE_PROMPT = """\
You are being asked a single factual question about your own runtime configuration.

What is the maximum context window (in tokens) of the model currently answering \
this prompt? This is the total input+output token capacity the model supports in \
one request.

Respond with ONLY a single integer — the token count — and nothing else. \
Examples of valid responses: 128000, 200000, 1000000, 1048576. \
Do not include units, commas, prose, or punctuation. \
If you are uncertain of the exact figure, respond with the largest value you are \
confident the model supports.
"""


def _cache_path(config) -> Path:
    return config.runtime_dir / "probed-context.json"


def load_cached(config) -> int | None:
    """Return a cached context for the current model, or None if stale/missing."""
    p = _cache_path(config)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if d.get("model") != config.llm_model:
        return None
    if time.time() - d.get("probed_at", 0) > _PROBE_CACHE_TTL:
        return None
    try:
        return int(d.get("context"))
    except (TypeError, ValueError):
        return None


def save_cached(config, context: int) -> None:
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    d = {
        "model": config.llm_model,
        "context": int(context),
        "probed_at": int(time.time()),
    }
    _cache_path(config).write_text(json.dumps(d, indent=2), encoding="utf-8")


def clear_probe_cache(config) -> None:
    """Force a genuine fresh probe by clearing BOTH cache layers.

    The probe has two independent caches: the parsed value in
    ``probed-context.json`` and the conversation router's Q&A under
    ``conversation/ctxprobe*``. Deleting only the former does NOT re-probe — the
    router replays the cached answer. This clears both (all model-namespaced
    ctxprobe dirs), so the next ``resolve_context`` does a real round-trip.
    Wired to ``ingest.py --reprobe``.
    """
    import shutil

    try:
        _cache_path(config).unlink()
    except FileNotFoundError:
        pass
    conv_root = config.runtime_dir / "conversation"
    if conv_root.exists():
        for d in conv_root.glob(f"{_PROBE_PREFIX_BASE}*"):
            shutil.rmtree(d, ignore_errors=True)


def _parse_context(text: str) -> int:
    """Extract the first 4+ digit integer from the response (tolerates prose/commas)."""
    cleaned = (text or "").replace(",", "").replace(" ", "")
    m = re.search(r"\d{4,}", cleaned)
    return int(m.group()) if m else 0


def probe_context(config) -> int:
    """Probe the live model's context window. Raises ConversationPending on first pass."""
    from _llm_api import call_anthropic_protocol

    saved_prefix = config.conversation_prefix
    config.conversation_prefix = _probe_prefix(config.llm_model)
    try:
        resp, _stop = call_anthropic_protocol(
            _PROBE_PROMPT, config, max_tokens=64, label="context-probe"
        )
    finally:
        config.conversation_prefix = saved_prefix

    raw = _parse_context(resp)
    if not (_PROBE_MIN <= raw <= _PROBE_MAX):
        raise RuntimeError(
            f"[context-probe] model '{config.llm_model}' returned an implausible "
            f"context value ({raw!r} parsed from {resp!r}). Expected an integer in "
            f"[{_PROBE_MIN}, {_PROBE_MAX}]. No silent fallback — the ingest pauses "
            f"here. Recovery: hand-edit {_cache_path(config)} with "
            f'{{"model": "{config.llm_model}", "context": <int>, "probed_at": {int(time.time())}}} '
            f"and re-run, or adjust the probe prompt. See references/context-probe.md."
        )

    save_cached(config, raw)
    print(f"[context-probe] model={config.llm_model} reported={raw:,} tokens → using as-is "
          f"(reserves already provide headroom)")
    return raw


def resolve_context(config) -> int:
    """Return the live context for the current model: cache hit or one-shot probe."""
    cached = load_cached(config)
    if cached is not None:
        print(f"[context-probe] cached: model={config.llm_model} context={cached:,} (reuse)")
        return cached
    return probe_context(config)
