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
real model answering is Claude Opus 4.8). So the probe also asks the model to
self-report its identity and compares it to the env name:
- env name matches self-report → ``env_reliable=True``.
- they disagree → ``env_reliable=False`` → a loud WARNING that the env name is
  stale, so the operator can fix ANTHROPIC_MODEL (or run ``--reprobe``).
``env_reliable`` is INFORMATIONAL — it does not block cache reuse. The cached
value is the live-probed context (correct regardless of the env label), and
reuse is keyed on env name + a 7-day TTL that bounds staleness. (Blocking reuse
on mismatch would force a probe handoff before every stage, since each
conversation-mode handoff is a fresh process invocation re-running the probe.)
A ``.ctxprobe-pending`` marker makes a genuine probe (cache miss / TTL / model
change / ``--reprobe``) re-ask the live model — clearing the stale conversation
answer on a fresh start — without looping on the handoff re-invocation.

Reliability: a confident numeric answer is sanity-gated to [8K, 10M]; an
out-of-range confident answer PAUSES the ingest (no-silent-fallback policy).
The prompt tells the model not to guess — if it isn't certain it must write
UNKNOWN instead of a number. A model self-report is never taken as ground
truth on its own: a recognized model (``_KNOWN_MODEL_CONTEXT``) is always
pinned to its authoritative spec, confident-sounding self-report or not; an
unrecognized model's confident self-report is used but loudly flagged as
unverified; UNKNOWN (or nothing parseable) falls back to the known spec if
recognized, else the codebase's own conservative default — never to a guess.
See references/context-probe.md for the hand-recovery path.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from _paths import atomic_write

# Conversation prefix base. The probe dir is MODEL-NAMESPACED (``ctxprobe-<model>``)
# so a different model → different dir → fresh probe, same model → cached, no loop.
_PROBE_PREFIX_BASE = "ctxprobe"

_PROBE_MIN = 8_000          # no real model answers below this
_PROBE_MAX = 10_000_000     # no current model exceeds this
_PROBE_CACHE_TTL = 7 * 24 * 3600   # re-probe after 7 days (model may upgrade)

# Known-model validation. Even with the prompt telling it not to guess, a model
# can still confidently self-report a wrong number (observed: Claude Opus 4.8 —
# genuinely 1M — self-reported a cautious 200000 in one probe; Claude Sonnet 5 —
# also genuinely 1M — self-reported 200000 from a generic impression in another,
# 2026-07-09). When the self-report names a model we have an authoritative spec
# for, we pin the context to that spec instead of trusting the model's answer,
# confident-sounding or not. Unknown models keep their self-report (still
# sanity-gated to [MIN, MAX], and loudly flagged as unverified — see
# probe_context). Sources: claude-api skill model table for Claude; glm-5.2 from
# the user's own runtime note. Update whenever a new model shows up in a probe —
# a missing entry is exactly what let the Sonnet 5 case slip through.
_KNOWN_MODEL_CONTEXT = {
    "claude-fable-5": 1_000_000,
    "claude-mythos-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-haiku-4-5": 200_000,
    "glm-5.2": 1_000_000,
}

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

Do not guess or estimate from a general impression of "typical" model sizes — a \
wrong guess silently mis-sizes every chunk budget downstream and is easy to get \
wrong (context windows change across model releases). Only report a number you \
are certain is your actual documented spec. If you are not certain, write UNKNOWN \
on line 2 instead of a number — do not write a fallback number either.
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


def _known_context(model_self: str | None) -> int | None:
    """Authoritative context for a recognized model, else None.

    Matches the model's self-report against ``_KNOWN_MODEL_CONTEXT`` with the
    same normalized, substring-tolerant comparison as ``_identities_match`` so
    ``Claude-Opus-4-8`` / ``anthropic/claude-opus-4-8`` all resolve. Matched
    against the self-report (the model actually answering), NOT the env name,
    which the probe deliberately distrusts.
    """
    a = _norm_model(model_self)
    if not a:
        return None
    # Exact match first — a substring hit must never shadow an exact entry
    # (e.g. a future "claude-sonnet-5-2" self-report vs the "claude-sonnet-5"
    # table row).
    for name, ctx in _KNOWN_MODEL_CONTEXT.items():
        if a == _norm_model(name):
            return ctx
    # Substring tolerance (proxy prefixes like "anthropic/claude-opus-4-8"):
    # still resolves, but say so — a fuzzy hit pinning the wrong spec should be
    # visible in the log rather than silent.
    for name, ctx in _KNOWN_MODEL_CONTEXT.items():
        b = _norm_model(name)
        if a in b or b in a:
            print(f"[context-probe] ⚠️  substring match: self-report {model_self!r} "
                  f"resolved to known model '{name}' ({ctx:,} tokens) — verify this "
                  f"is the intended model.")
            return ctx
    return None


def load_cached(config) -> int | None:
    """Return a cached context for the current model, or None if stale/missing.

    Reuse is keyed on the env name + 7-day TTL. ``env_reliable`` is INFORMATIONAL
    only (surfaced as a warning when probed) — it does NOT block reuse: the cached
    value is the live-probed context, correct regardless of whether the env name
    matches the model's self-report. (Blocking reuse on env_reliable=False would
    force a probe handoff before every stage, since each conversation-mode handoff
    is a fresh process invocation that re-runs resolve_context — it would stall the
    ingest.) Staleness is instead bounded by the TTL. Backward compatible with the
    old ``{model, context, probed_at}`` schema.
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
    atomic_write(_cache_path(config), json.dumps(d, indent=2, ensure_ascii=False))


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

    Context = first 4+ digit integer (tolerates prose/commas), or ``None`` if
    the model wrote UNKNOWN (or nothing parseable) rather than guessing —
    distinct from "parsed to 0", which would incorrectly read as a confident
    but out-of-range answer. Identity = the first non-empty line that isn't
    purely that number / UNKNOWN; None if unobtainable.
    """
    raw_lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]

    def _extract_int(s):
        cleaned = (s or "").replace(",", "").replace(" ", "")
        m = re.search(r"\d{4,}", cleaned)
        return int(m.group()) if m else None

    # Prefer the last line for the context number (the prompt's two-line
    # contract puts it there) — scanning the whole text first false-positives
    # on digits embedded in the model identifier itself (e.g. a release-date
    # suffix like "-20251001"), which the identifier line commonly has.
    context = _extract_int(raw_lines[-1]) if raw_lines else None
    if context is None:
        context = _extract_int(text)
    model_self = None
    for ln in raw_lines:
        if re.fullmatch(r"[\d,\s]+", ln) or ln.strip().upper() == "UNKNOWN":
            continue  # the number line, or an explicit non-guess
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
    known = _known_context(model_self)

    if raw is None:
        # The model declined to guess (wrote UNKNOWN) or gave nothing parseable.
        # Never substitute an unverified number here: use the authoritative spec
        # if this model is recognized, else the codebase's own conservative
        # built-in default — loudly, so it doesn't read as a confirmed value.
        from _core import _CONTEXT_SIZE_DEFAULT
        if known is not None:
            raw = known
            print(f"[context-probe] '{model_self}' reported no confident context "
                  f"value — using the known spec ({raw:,}) instead of guessing.")
        else:
            raw = _CONTEXT_SIZE_DEFAULT
            print(f"[context-probe] ⚠️  UNVERIFIED MODEL: '{model_self}' is not in "
                  f"_KNOWN_MODEL_CONTEXT and reported no confident context value. "
                  f"Falling back to the conservative default ({raw:,}) rather than "
                  f"guessing — chunks may end up smaller than the model actually "
                  f"supports. Verify the real context window and add a confirmed "
                  f"entry to _KNOWN_MODEL_CONTEXT in _context_probe.py, then run "
                  f"ingest.py --reprobe.")
    else:
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
        # Known-model validation: pin a recognized model to its authoritative
        # spec rather than trusting a possibly-cautious (lowballed) or confused
        # (overshot) self-reported number — regardless of how confident it read.
        if known is not None and known != raw:
            print(f"[context-probe] known-model validation: '{model_self}' self-reported "
                  f"{raw:,} tokens but the authoritative spec is {known:,} — using the spec.")
            raw = known
        elif known is None:
            print(f"[context-probe] ⚠️  UNVERIFIED: '{model_self}' is not in "
                  f"_KNOWN_MODEL_CONTEXT — using its self-reported value ({raw:,}) "
                  f"as-is. This has NOT been cross-checked against an authoritative "
                  f"spec. If it turns out wrong, add a confirmed entry to "
                  f"_KNOWN_MODEL_CONTEXT in _context_probe.py.")

    env_reliable = _identities_match(model_self, config.llm_model)
    save_cached(config, raw, model_self, env_reliable)
    try:
        pending.unlink()
    except FileNotFoundError:
        pass

    if model_self and env_reliable is False:
        print(f"[context-probe] WARNING: env model name '{config.llm_model}' is STALE — "
              f"disagrees with the live model's self-report '{model_self}'. Using the live "
              f"probe ({raw:,} tokens) and caching it (TTL-bounded). Fix ANTHROPIC_MODEL so "
              f"the cache key reflects the real model, or run 'ingest.py --reprobe'.")
    print(f"[context-probe] model_self={model_self!r} env={config.llm_model!r} "
          f"reported={raw:,} tokens → using as-is (reserves already provide headroom)")
    return raw


def resolve_context(config) -> int:
    """Return the live context for the current model: cache hit or one-shot probe."""
    cached = load_cached(config)
    if cached is not None:
        if not config.llm_model:
            # ANTHROPIC_MODEL is unset, so the cache key cannot distinguish
            # conversation models: a session-model switch (e.g. Opus → Fable)
            # is INVISIBLE here and the cached window may belong to the old
            # model (2026-07-02). Reuse anyway (blocking would stall every
            # handoff — see load_cached docstring), but say so loudly.
            print(f"[context-probe] cached: context={cached:,} (reuse) "
                  f"⚠️  ANTHROPIC_MODEL unset — a conversation-model switch is "
                  f"NOT auto-detected; clear_probe_cache() to force a re-probe")
        else:
            print(f"[context-probe] cached: model={config.llm_model} context={cached:,} (reuse)")
        return cached
    return probe_context(config)
