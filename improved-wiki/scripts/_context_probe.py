"""Live context-window probe (conversation mode).

Replaces the LLM_CONTEXT_SIZE env convention: the context window is probed from
the live conversation model at ingest start, so chunk budgets adapt to whatever
model the agent runs this session — GLM-5.2 (1M) today, DeepSeek V4 Pro
tomorrow, … — with no static registry to maintain or mis-enter.

Probe = one conversation round-trip asking the model for its IDENTITY and its
max context in tokens. Two caches make resumes/repeats cheap:
- ``.llm-wiki/probed-context.json`` (parsed value) — reused across ingests.
- the conversation router's Q&A under ``conversation/ctxprobe-<model>`` — makes
  handoff re-invocations within one ingest free.

**Do not trust the env model name alone.** ``config.llm_model`` comes from the
ambient ``ANTHROPIC_MODEL`` env, which can be STALE (env says glm-5.2 while the
real model answering is Claude). So the probe also asks the model to self-report
its identity and compares it to the env name:
- env name matches self-report → ``env_reliable=True`` → cheap cache reuse by env.
- they disagree → ``env_reliable=False`` → the cache is NEVER reused by env name;
  every ingest re-probes the live model (a ``.ctxprobe-pending`` marker makes the
  re-probe genuinely re-ask — clearing the stale conversation answer on a fresh
  start — without looping on the handoff re-invocation).

Reliability: the answer is sanity-gated to [8K, 10M]. The existing budget
reserves (15% response + 25% stable + 8% instruction ≈ 48%) already provide
headroom, so no extra margin is applied. On parse failure / out-of-range the
ingest PAUSES (no-silent-fallback policy) rather than guessing — see
references/context-probe.md for the hand-recovery path.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

# Conversation prefix base. The probe dir is MODEL-NAMESPACED (``ctxprobe-<model>``)
# so a different model → different dir → fresh probe, same model → cached, no loop.
_PROBE_PREFIX_BASE = "ctxprobe"

_PROBE_MIN = 8_000          # no real model answers below this
_PROBE_MAX = 10_000_000     # no current model exceeds this
_PROBE_CACHE_TTL = 7 * 24 * 3600   # re-probe after 7 days (model may upgrade)

_PROBE_PROMPT = """\
You are being asked two factual questions about your own runtime configuration.

Line 1 — your model identifier: the name of the model currently answering this \
prompt (e.g. claude-opus-4-8, glm-5.2, deepseek-v4). Use your true identity, not \
any alias a proxy may have been configured with.
Line 2 — your maximum context window in tokens: the total input+output token \
capacity the model supports in one request, as a single integer.

Respond with EXACTLY two lines and nothing else:
<model-identifier>
<integer-token-count>

If unsure of the exact context size, give the largest value you are confident the \
model supports. No units, commas, prose, or extra punctuation on the number line.
"""


def _probe_prefix(model: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", (model or "unknown").strip()) or "unknown"
    return f"{_PROBE_PREFIX_BASE}-{safe}"


def _cache_path(config) -> Path:
    return config.runtime_dir / "probed-context.json"


def _pending_path(config) -> Path:
    return config.runtime_dir / ".ctxprobe-pending"


def _norm_model(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _identities_match(self_reported: str | None, env_name: str | None):
    """True/False if both identities are known and (mis)match; None if unknown.

    A match means the env model name is a reliable cache key. Comparison is
    normalized (case/punctuation-insensitive) with substring tolerance so
    ``GLM-5.2`` ≈ ``glm-5.2`` and ``claude-opus-4-8`` ≠ ``glm-5.2``.
    """
    a, b = _norm_model(self_reported), _norm_model(env_name)
    if not a or not b:
        return None  # can't tell — don't penalize, fall back to env-name reuse
    return a == b or a in b or b in a


def load_cached(config) -> int | None:
    """Return a cached context for the current model, or None if stale/missing.

    Never reuses across ingests when the env name was proven unreliable
    (``env_reliable`` is False); that forces a fresh live probe. Backward
    compatible with the old ``{model, context, probed_at}`` schema.
    """
    p = _cache_path(config)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    cached_env = d.get("model_env", d.get("model"))  # new schema, else old
    if cached_env != config.llm_model:
        return None
    if time.time() - d.get("probed_at", 0) > _PROBE_CACHE_TTL:
        return None
    if d.get("env_reliable") is False:
        return None  # env name proven unreliable for this model → always re-probe
    try:
        return int(d.get("context"))
    except (TypeError, ValueError):
        return None


def save_cached(config, context: int, model_self: str | None, env_reliable) -> None:
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    d = {
        "model_env": config.llm_model,
        "model_self": model_self,
        "env_reliable": env_reliable,
        "context": int(context),
        "probed_at": int(time.time()),
        # kept for any old reader that still looks for "model"
        "model": config.llm_model,
    }
    _cache_path(config).write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


def _clear_ctxprobe_dirs(config) -> None:
    conv_root = config.runtime_dir / "conversation"
    if conv_root.exists():
        for d in conv_root.glob(f"{_PROBE_PREFIX_BASE}*"):
            shutil.rmtree(d, ignore_errors=True)


def clear_probe_cache(config) -> None:
    """Force a genuine fresh probe by clearing ALL probe state.

    Removes the parsed value (probed-context.json), every conversation-router
    probe dir (ctxprobe*), and the pending marker. Deleting only
    probed-context.json does NOT re-probe — the router replays the cached answer.
    Wired to ``ingest.py --reprobe``.
    """
    try:
        _cache_path(config).unlink()
    except FileNotFoundError:
        pass
    try:
        _pending_path(config).unlink()
    except FileNotFoundError:
        pass
    _clear_ctxprobe_dirs(config)


def _parse_probe(text: str):
    """Parse (model_self, context) from the two-line probe response.

    Context = first 4+ digit integer (tolerates prose/commas). Identity = the
    first non-empty line that isn't purely that number; None if unobtainable.
    """
    raw_lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    cleaned = (text or "").replace(",", "").replace(" ", "")
    m = re.search(r"\d{4,}", cleaned)
    context = int(m.group()) if m else 0
    model_self = None
    for ln in raw_lines:
        if re.fullmatch(r"[\d,\s]+", ln):
            continue  # the number line
        model_self = ln
        break
    return model_self, context


def probe_context(config) -> int:
    """Probe the live model's identity + context window. Raises ConversationPending
    on the first pass (normal handoff)."""
    from _llm_api import call_anthropic_protocol

    pending = _pending_path(config)
    if not pending.exists():
        # Fresh probe (not a mid-probe handoff resume): discard any stale answer
        # from a prior ingest so we genuinely re-ask the LIVE model, then mark
        # the probe in-flight so the handoff re-invocation does NOT re-clear
        # (which would wipe the agent's just-written answer and loop).
        _clear_ctxprobe_dirs(config)
        config.runtime_dir.mkdir(parents=True, exist_ok=True)
        pending.write_text("", encoding="utf-8")

    saved_prefix = config.conversation_prefix
    config.conversation_prefix = _probe_prefix(config.llm_model)
    try:
        resp, _stop = call_anthropic_protocol(
            _PROBE_PROMPT, config, max_tokens=64, label="context-probe"
        )
    finally:
        config.conversation_prefix = saved_prefix

    # An answer came back (no exit-101) — consume it.
    model_self, raw = _parse_probe(resp)
    if not (_PROBE_MIN <= raw <= _PROBE_MAX):
        raise RuntimeError(
            f"[context-probe] model '{config.llm_model}' returned an implausible "
            f"context value ({raw!r} parsed from {resp!r}). Expected an integer in "
            f"[{_PROBE_MIN}, {_PROBE_MAX}]. No silent fallback — the ingest pauses "
            f"here. Recovery: hand-edit {_cache_path(config)} with "
            f'{{"model_env": "{config.llm_model}", "context": <int>, "env_reliable": true, '
            f'"probed_at": {int(time.time())}}} and re-run, or run ingest.py --reprobe. '
            f"See references/context-probe.md."
        )

    env_reliable = _identities_match(model_self, config.llm_model)
    save_cached(config, raw, model_self, env_reliable)
    try:
        pending.unlink()
    except FileNotFoundError:
        pass

    if model_self and env_reliable is False:
        print(f"[context-probe] WARNING: env model name '{config.llm_model}' disagrees "
              f"with the live model's self-report '{model_self}'. Using the live probe "
              f"({raw:,} tokens); the env name will NOT be trusted as a cache key "
              f"(re-probes each ingest). Fix ANTHROPIC_MODEL or run --reprobe.")
    print(f"[context-probe] model_self={model_self!r} env={config.llm_model!r} "
          f"reported={raw:,} tokens → using as-is (reserves already provide headroom)")
    return raw


def resolve_context(config) -> int:
    """Return the live context for the current model: cache hit or one-shot probe."""
    cached = load_cached(config)
    if cached is not None:
        print(f"[context-probe] cached: model={config.llm_model} context={cached:,} (reuse)")
        return cached
    return probe_context(config)
