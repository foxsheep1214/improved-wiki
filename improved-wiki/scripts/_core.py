"""Shared core for improved-wiki ingest pipeline.

All stage modules and ingest.py import from here.  Refactored out of ingest.py
on 2026-06-18 — functions now live in a single canonical location.  No local
duplicates remain in ingest.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Runtime dir detection — delegated to _paths.py (shared with all scripts)
_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))
from _paths import detect_runtime_dir  # noqa: E402
# atomic_write moved to _paths.py (canonical, so light tools can import it
# without pulling in the full core); re-exported here for back compat.
from _paths import atomic_write  # noqa: E402,F401
from _paths import WIKI_ARTIFACT_DIRS as _WIKI_ARTIFACT_DIRS  # noqa: E402

# Maximum concurrency for parallel LLM phases (Stage 1/1.5/2 are
# read-only LLM calls — no shared wiki/ state mutation — so they can
# run in parallel). Shared by ingest.py (batch_ingest, main) and
# _watch.py (ingest_watch). 4 is safe for most provider rate limits.
BATCH_MAX_CONCURRENT = 4


# ── Progress / UI helpers ──

_current_file_local = threading.local()
_stage_start_times: dict[str, float] = {}


def set_current_file(name: str) -> None:
    _current_file_local.value = name


def get_current_file() -> str:
    return getattr(_current_file_local, "value", "")


def file_tag() -> str:
    f = get_current_file()
    if not f:
        return ""
    if len(f) > 50:
        return f"[{f[:40]}...{f[-6:]}] "
    return f"[{f}] "


def stage_begin(name: str) -> None:
    _stage_start_times[name] = time.time()
    tag = file_tag()
    print(f"\n{'─'*40}\n{tag}[{name}] Starting...\n{'─'*40}", flush=True)


def stage_end(name: str) -> None:
    t0 = _stage_start_times.pop(name, None)
    elapsed = time.time() - t0 if t0 else 0.0
    tag = file_tag()
    if elapsed >= 60:
        print(f"{tag}[{name}] Done ({elapsed/60:.1f}m)", flush=True)
    else:
        print(f"{tag}[{name}] Done ({elapsed:.0f}s)", flush=True)


def heartbeat(msg: str = "") -> None:
    ts = time.strftime("%H:%M:%S")
    tag = file_tag()
    suffix = f" — {msg}" if msg else ""
    print(f"  {ts}  {tag}… {suffix}", flush=True)


def llm_call_progress(label: str, attempt: int = 1, retries: int = 0) -> None:
    tag = file_tag()
    # Show "retry" only on actual retries (attempt > 1). Printing "(retry 1/4)"
    # on a first attempt is misleading — it's the initial try, not a retry.
    hint = f" (retry {attempt - 1}/{retries})" if retries and attempt > 1 else ""
    print(f"  {tag}→ {label}{hint}...", end=" ", flush=True)


def llm_call_done(elapsed: float, chars: int | None = None) -> None:
    size_hint = f", {chars:,} chars" if chars else ""
    print(f"OK ({elapsed:.0f}s{size_hint})", flush=True)


# Rate-limit tracking (shared across workers)
_RATE_LIMIT_HIT_AT = 0.0
_RLOCK = threading.Lock()


def record_rate_limit() -> None:
    global _RATE_LIMIT_HIT_AT
    with _RLOCK:
        _RATE_LIMIT_HIT_AT = time.time()


def rate_limit_cooldown_remaining() -> float:
    with _RLOCK:
        elapsed = time.time() - _RATE_LIMIT_HIT_AT
        return max(0.0, 60.0 - elapsed)


class ConversationPending(BaseException):
    """Raised when a prompt is written to disk and awaits the calling agent.

    Subclasses BaseException (not Exception) so the broad ``except Exception``
    retry/fallback blocks around LLM calls in the stage modules do NOT swallow
    it — ConversationPending is control flow (pause for the calling agent),
    not a transient HTTP error. It still propagates to the top-level
    ``except ConversationPending`` handler (ingest.py main) which exits 101.
    """


class PrepareStopAfter(BaseException):
    """Raised inside ``_do_prepare`` when ``--stop-after-stage`` matches a
    Stage-0..2 boundary that has just completed (0=extract, 1=global digest,
    2=generation). Subclasses BaseException so the broad ``except Exception``
    in ``_do_prepare`` (which prints FAILED + traceback and re-raises) does
    not noisy-up a clean, intentional stop. Caught in ``ingest_one`` and
    converted to ``{"status": "ok", "stopped_after": stage}``.

    Without this, ``--stop-after-stage 0`` could not actually halt after OCR:
    the stop check lived AFTER ``_do_prepare`` returned, but ``_do_prepare``
    runs all of Stage 0-2 (pausing at the 2.1/2.2/2.4 LLM handoffs) before
    that check — so the flag was effectively dead on a fresh run. Raising at
    the in-prepare boundary makes the documented "OCR-only then re-run" split
    work. Boundaries 1.5/2.3 (inside the chunk pipeline, no clean resume
    marker) remain best-effort and are not intercepted here.
    """

    def __init__(self, stage: str):
        super().__init__(stage)
        self.stage = stage


# ── Configuration ──

def load_provider_config(name: str | None = None) -> dict:
    # Priority 1: the current agent's own provider (ambient ANTHROPIC_* env).
    # Ingest text-generation follows whichever agent/model runs the skill —
    # GLM-5.2 this session, Deepseek V4 Pro next session, … — without any
    # hardcoded model in config.json. Skipped when a specific provider is
    # explicitly requested (name arg or LLM_PROVIDER env), so callers can
    # still force a particular provider when needed.
    explicit = name or os.environ.get("LLM_PROVIDER")
    if not explicit:
        agent_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
        agent_url = os.environ.get("ANTHROPIC_BASE_URL")
        agent_model = os.environ.get("ANTHROPIC_MODEL")
        if agent_key and agent_url and agent_model:
            return {
                "api_key": agent_key,
                "base_url": agent_url.rstrip("/"),
                "model": agent_model,
                "protocol": "anthropic",
                "provider": "agent",
            }
    # Priority 2: config.json provider (named, default, or LLM_PROVIDER).
    # Fallback for non-agent contexts (cron, standalone CLI) where no agent
    # env is present.
    config_path = Path.home() / ".agents" / "config.json"
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            if name is None:
                default = cfg.get("default", "")
                name = os.environ.get("LLM_PROVIDER", default)
            provider = cfg.get("providers", {}).get(name)
            if provider:
                models = provider.get("models", {})
                return {
                    "api_key": os.environ.get("LLM_API_KEY") or provider.get("api_key", ""),
                    "base_url": os.environ.get("LLM_BASE_URL") or provider.get("base_url", ""),
                    "model": os.environ.get("LLM_MODEL") or models.get("text", provider.get("model", "")),
                    "protocol": provider.get("protocol", "anthropic"),
                    "provider": name,
                }
        except Exception as e:
            # No silent fallback: a broken config.json must surface, not
            # silently degrade to env vars (policy 2026-06-24).
            raise RuntimeError(
                f"~/.agents/config.json exists but failed to parse "
                f"({type(e).__name__}: {e}) — fix or remove it. No silent fallback."
            ) from e
    return {
        "api_key": os.environ.get("LLM_API_KEY", ""),
        "base_url": os.environ.get("LLM_BASE_URL", ""),
        "model": os.environ.get("LLM_MODEL", ""),
        "protocol": os.environ.get("LLM_PROTOCOL", "anthropic"),
        "provider": "env",
    }


def load_caption_provider() -> dict:
    config_path = Path.home() / ".agents" / "config.json"
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            caption_name = cfg.get("caption_provider") or cfg.get("default", "")
            provider = cfg.get("providers", {}).get(caption_name)
            if provider:
                models = provider.get("models", {})
                return {
                    "api_key": provider.get("api_key", ""),
                    "base_url": provider.get("base_url", ""),
                    "model": models.get("caption") or models.get("vision") or provider.get("model", ""),
                    "protocol": provider.get("protocol", "anthropic"),
                    "provider": caption_name,
                }
        except Exception as e:
            # No silent fallback: a broken config.json must surface (policy 2026-06-24).
            raise RuntimeError(
                f"~/.agents/config.json exists but failed to parse "
                f"({type(e).__name__}: {e}) — fix or remove it. No silent fallback."
            ) from e
    # No caption provider configured — ingest will pause at Stage 1.3.
    # Configure ~/.agents/config.json with a caption_provider entry to enable.
    return {
        "api_key": os.environ.get("CAPTION_API_KEY") or os.environ.get("LLM_API_KEY", ""),
        "base_url": "",
        "model": "",
        "protocol": "",
        "provider": "",
    }


# ── NashSU-aligned context budget (ported from llm_wiki/src/lib/context-budget.ts + ingest.ts) ──
# Context window is probed from the live conversation model at ingest start
# (see _context_probe.resolve_context → Config.apply_context) and cached per-model.
# The LLM_CONTEXT_SIZE env convention has been removed; budgets adapt to whatever
# model the agent runs this session. from_env leaves a conservative placeholder
# that apply_context overwrites before any LLM stage runs.
_CONTEXT_SIZE_DEFAULT = 200_000
_RESPONSE_RESERVE_FRAC = 0.15
_STABLE_RESERVE_MIN = 12_000
_STABLE_RESERVE_FRAC = 0.25
_INSTRUCTION_RESERVE_MIN = 12_000
_INSTRUCTION_RESERVE_FRAC = 0.08
_SOURCE_BUDGET_MIN = 8_000
_SOURCE_BUDGET_MAX = 300_000
_SOURCE_BUDGET_FRAC = 0.6
_TARGET_CHARS_MIN = 12_000
_TARGET_CHARS_FRAC = 0.55
# ── Token-first chunk budgeting (replaces the fixed 60K char cap) ──
# The per-chunk budget is expressed in TOKENS; the char window is derived
# per-text from the measured chars/token ratio at split time (see
# _stage_2_analyze._stage_2_1_chunk_text). target_chars is now only a hard char
# *ceiling*, sized so even token-sparse Latin text can reach the token budget.
_TARGET_TOKENS_MIN = 12_000
_TARGET_TOKENS_CEIL_FRAC = 0.33     # (A) chunk size scales with the probed context window...
_TARGET_TOKENS_HARD_CEIL = 64_000   # (B) ...but never exceeds this. Set to 64K (2026-07-01) after an
                                    # A/B ingest (Barton, 448pp, 1M ctx): 64K (4 chunks) gave +27%
                                    # concept coverage, finer granularity, and CLEANER driving (10
                                    # native round-trips, no fan-out) vs a 192K whole-book single chunk
                                    # — which was too large to analyze/generate in one call and had to
                                    # fan out anyway, erasing its "fewer round-trips" rationale. Small
                                    # books stay 1 chunk (0.33×context + 12K floor govern below the cap;
                                    # the cap only binds for context > ~194K). Override per-run with
                                    # IMPROVED_WIKI_TARGET_TOKENS_CEIL.
_MAX_CHARS_PER_TOKEN = 4            # char ceiling = target_tokens × this (Latin ≈ 4 chars/token)
# A high char bound so token-sparse Latin text can spend its full token budget.
# It does NOT bind at the 64K default (64K×4 = 256K < 768K); kept at 768K so an
# env-override up to a ~192K-token ceiling stays effective for Latin text. CJK
# text (denser tokens) is governed by the token ceiling well before this binds.
_TARGET_CHARS_HARD_CEIL = 768_000


def _compute_chunk_targets(source_budget: int, context_size: int) -> tuple[int, int]:
    """Return ``(target_tokens, target_chars_ceiling)``.

    ``target_tokens`` — per-chunk budget in TOKENS. Scales with the probed
    model context window (×0.33) and is capped by ``_TARGET_TOKENS_HARD_CEIL``.
    Decoupled from ``source_budget`` (2026-06-27): each chunk is one analysis
    round-trip whose safe size is bounded by the context window, not the
    per-source digest budget. ``source_budget`` is retained in the signature for
    call-site compatibility and still governs the Stage 2.1 digest size.
    ``target_chars`` — hard per-chunk char ceiling, large enough that even
    token-sparse text can spend its full token budget.

    ``IMPROVED_WIKI_TARGET_TOKENS_CEIL`` env overrides the 64K default hard
    ceiling (e.g. 192000 to revert to whole-book single chunks, or 96000 for a
    middle ground). Unset → default 64K.
    """
    _ceil_env = os.environ.get("IMPROVED_WIKI_TARGET_TOKENS_CEIL", "").strip()
    hard_ceil = int(_ceil_env) if _ceil_env.isdigit() else _TARGET_TOKENS_HARD_CEIL
    tokens_ceil = min(hard_ceil,
                      max(_TARGET_TOKENS_MIN, int(context_size * _TARGET_TOKENS_CEIL_FRAC)))
    target_tokens = tokens_ceil
    target_chars = min(_TARGET_CHARS_HARD_CEIL, target_tokens * _MAX_CHARS_PER_TOKEN)
    return target_tokens, target_chars


@dataclass
class Config:
    wiki_root: Path
    raw_root: Path
    wiki_dir: Path
    runtime_dir: Path
    cache_path: Path
    progress_dir: Path
    extract_tmp_dir: Path
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_protocol: str
    caption_api_key: str
    caption_base_url: str
    caption_model: str
    chunk_size: int
    chunk_overlap: int
    source_budget: int
    target_chars: int
    target_tokens: int
    max_tokens: int
    context_size: int | None = None
    conversation_prefix: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        wiki_root = Path(os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd())).expanduser()
        provider = load_provider_config()
        caption = load_caption_provider()
        runtime_dir = detect_runtime_dir(wiki_root)

        # ── Context budget: pre-probe placeholder only ──
        # The real context window is probed from the live conversation model at
        # ingest start (see _context_probe.resolve_context → apply_context). This
        # placeholder is overwritten before any LLM stage runs; delete-only paths
        # never probe and simply use the conservative default.
        context_size = None
        source_budget = _CONTEXT_SIZE_DEFAULT
        target_tokens, target_chars = _compute_chunk_targets(source_budget, _CONTEXT_SIZE_DEFAULT)

        return cls(
            wiki_root=wiki_root,
            raw_root=wiki_root / "raw",
            wiki_dir=wiki_root / "wiki",
            runtime_dir=runtime_dir,
            cache_path=runtime_dir / "ingest-cache.json",
            progress_dir=runtime_dir / "ingest-progress",
            extract_tmp_dir=runtime_dir / "extract-tmp",
            llm_base_url=provider["base_url"],
            llm_model=provider["model"],
            llm_api_key=provider["api_key"],
            llm_protocol=provider.get("protocol", "anthropic"),
            caption_api_key=caption["api_key"],
            caption_base_url=caption["base_url"],
            caption_model=caption["model"],
            chunk_size=300_000,
            chunk_overlap=3_000,
            source_budget=source_budget,
            target_chars=target_chars,
            target_tokens=target_tokens,
            max_tokens=16384,
            context_size=context_size,
        )

    def apply_context(self, context_size: int) -> None:
        """Apply a live-probed context window and recompute derived budgets.

        Replaces the pre-probe placeholder set in ``from_env``. Called once at
        ingest start by ``_context_probe.resolve_context`` (after the model's
        context window is probed or read from cache). Idempotent.
        """
        self.context_size = context_size
        cs = context_size
        response_reserve = int(cs * _RESPONSE_RESERVE_FRAC)
        stable_reserve = min(int(cs * _STABLE_RESERVE_FRAC), max(_STABLE_RESERVE_MIN, 50_000))
        instruction_reserve = max(_INSTRUCTION_RESERVE_MIN, int(cs * _INSTRUCTION_RESERVE_FRAC))
        available = cs - response_reserve - stable_reserve - instruction_reserve
        upper = min(_SOURCE_BUDGET_MAX, max(_SOURCE_BUDGET_MIN, int(cs * _SOURCE_BUDGET_FRAC)))
        self.source_budget = max(_SOURCE_BUDGET_MIN, min(available, upper))
        self.target_tokens, self.target_chars = _compute_chunk_targets(self.source_budget, context_size)
        print(f"[config] probed context={context_size:,} → "
              f"source_budget={self.source_budget:,} target_tokens={self.target_tokens:,} "
              f"target_chars≤{self.target_chars:,}")

    def compute_source_budget(self, stable_length: int = 50_000) -> int:
        """NashSU-aligned: per-source budget from context window."""
        cs = self.context_size or _CONTEXT_SIZE_DEFAULT
        response_reserve = int(cs * _RESPONSE_RESERVE_FRAC)
        stable_reserve = min(int(cs * _STABLE_RESERVE_FRAC), max(_STABLE_RESERVE_MIN, stable_length))
        instruction_reserve = max(_INSTRUCTION_RESERVE_MIN, int(cs * _INSTRUCTION_RESERVE_FRAC))
        available = cs - response_reserve - stable_reserve - instruction_reserve
        upper = min(_SOURCE_BUDGET_MAX, max(_SOURCE_BUDGET_MIN, int(cs * _SOURCE_BUDGET_FRAC)))
        return max(_SOURCE_BUDGET_MIN, min(available, upper))

    def compute_target_tokens(self, stable_length: int = 50_000) -> int:
        """Per-source chunk token budget (token-first; scales with context)."""
        sb = self.compute_source_budget(stable_length)
        cs = self.context_size or _CONTEXT_SIZE_DEFAULT
        return _compute_chunk_targets(sb, cs)[0]

    def compute_target_chars(self, stable_length: int = 50_000) -> int:
        """Per-source hard char ceiling derived from the token budget."""
        sb = self.compute_source_budget(stable_length)
        cs = self.context_size or _CONTEXT_SIZE_DEFAULT
        return _compute_chunk_targets(sb, cs)[1]

    def compute_max_tokens(self, base_tokens: int = 16384) -> int:
        env_override = os.environ.get("LLM_MAX_TOKENS")
        if env_override:
            return int(env_override)

        # ── Context-size-aware (NashSU-aligned) ──
        cs = self.context_size or 0
        if cs >= 500_000:
            return min(base_tokens * 2, 32768)
        if cs >= 250_000:
            return base_tokens
        if cs >= 120_000:
            return max(base_tokens // 2, 8192)

        # ── Fallback: model-name pattern matching ──
        model = self.llm_model.lower()
        # DeepSeek V4 series has 1M context + 384K max output.
        # 「deepseek-chat」is a legacy alias mapping to v4-flash — same tier.
        if "512k" in model or "1m" in model or "deepseek-v4" in model or "deepseek-chat" in model:
            return min(base_tokens * 2, 32768)
        if "256k" in model or "200k" in model:
            return base_tokens
        if "128k" in model or "100k" in model:
            return max(base_tokens // 2, 8192)
        return base_tokens


# ── File-type detection ──

FOLDER_TO_TEMPLATE = {
    "Book": "digest-book",
    "Paper": "digest-paper",
    "Datasheet": "digest-datasheet",
    "Applicationnote": "digest-applicationnote",
    "Designexample": "digest-designexample",
    "Presentation": "digest-presentation",
    "Standard": "digest-standard",
    "News": "digest-news",
}


def str_distance(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    if len(b) == 0:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(
                prev[j + 1] + 1, curr[j] + 1,
                prev[j] + (0 if ca == cb else 1),
            ))
        prev = curr
    return prev[-1]


def detect_template_type(raw_file: Path, raw_root: Path, override: str | None) -> str:
    if override:
        return override
    try:
        rel = raw_file.relative_to(raw_root)
    except ValueError:
        return "digest-book"
    parts = rel.parts
    if len(parts) == 1:
        return "digest-book"
    folder = parts[0]
    # Case-insensitive lookup: "book" → "Book", "BOOK" → "Book"
    folder_lower = folder.lower()
    FOLDER_LOWER_MAP = {k.lower(): k for k in FOLDER_TO_TEMPLATE}
    if folder_lower in FOLDER_LOWER_MAP:
        return FOLDER_TO_TEMPLATE[FOLDER_LOWER_MAP[folder_lower]]
    if folder == "sources":
        if len(parts) >= 3:
            type_part = parts[1]
            type_lower = type_part.lower()
            if type_lower in FOLDER_LOWER_MAP:
                return FOLDER_TO_TEMPLATE[FOLDER_LOWER_MAP[type_lower]]
        return "digest-book"
    available = sorted(FOLDER_TO_TEMPLATE.keys())
    match = min(available, key=lambda a: str_distance(folder, a))
    print(f"[detect] Unknown raw folder '{folder}' — treating as '{match}' "
          f"(pass --type to override)", flush=True)
    return FOLDER_TO_TEMPLATE[match]


def load_template(template_name: str) -> str:
    skill_dir = Path(__file__).resolve().parent.parent
    tmpl_path = skill_dir / "templates" / f"{template_name}.md"
    if tmpl_path.exists():
        return tmpl_path.read_text(encoding="utf-8")
    return ""


# ── Hashing & cache ──

def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_cache(config: Config) -> dict:
    if config.cache_path.exists():
        try:
            return json.loads(config.cache_path.read_text(encoding="utf-8"))
        except Exception as e:
            # Corrupted cache is not a silent reset — warn loudly so the user
            # knows why a full re-ingest is happening (policy 2026-06-24).
            print(f"⚠️  [cache] {config.cache_path} corrupted ({type(e).__name__}: {e}) "
                  f"— discarding cache, will re-ingest from scratch.")
    return {"version": "2", "entries": {}}


def save_cache(config: Config, cache: dict) -> None:
    config.cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.cache_path.with_suffix(config.cache_path.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(config.cache_path)


# ── Checkpoint / Resume ──

def progress_path(config: Config, source_hash: str) -> Path:
    config.progress_dir.mkdir(parents=True, exist_ok=True)
    return config.progress_dir / f"{source_hash[:16]}.json"


def load_progress(config: Config, source_hash: str) -> dict | None:
    pp = progress_path(config, source_hash)
    if pp.exists():
        return json.loads(pp.read_text(encoding="utf-8"))
    return None


def save_progress(config: Config, source_hash: str, data: dict) -> None:
    """Merge-write artifact cache. Loads existing artifacts, updates with the
    new keys, writes back. Callers write ONLY their stage's new artifacts — no
    need to re-carry cumulative keys (the overwrite-write fragility that caused
    the 2026-06-25 stage-marker resume loop: a save that forgot a key silently
    erased it).

    Stage-completion state (control flow) lives in stages.json via
    mark_stage_done / is_stage_done — NOT in this file. This file is a pure
    artifact store keyed by artifact name (extracted_text, chunk_analyses, …).
    """
    pp = progress_path(config, source_hash)
    existing: dict = {}
    if pp.exists():
        try:
            existing = json.loads(pp.read_text(encoding="utf-8"))
        except Exception:
            # Corrupted artifact cache is not fatal — rebuild from empty.
            existing = {}
    existing.update(data)
    existing["_updated_at"] = int(time.time() * 1000)
    tmp = pp.with_suffix(".tmp")
    tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(pp)


def clear_progress(config: Config, source_hash: str) -> None:
    pp = progress_path(config, source_hash)
    if pp.exists():
        pp.unlink()


# ── Stage-completion markers (Option A: stage-aware resume) ──
# Separate from the extraction progress cache above: these markers persist
# across the whole ingest and record which pipeline stages have completed,
# so a conversation-mode resume can skip already-done non-idempotent stages
# (notably the Stage 3.1 write loop, whose page-merge would otherwise fire
# spuriously on every resume because post-write steps mutate page bodies).

def stages_path(config: Config, source_hash: str) -> Path:
    config.progress_dir.mkdir(parents=True, exist_ok=True)
    return config.progress_dir / f"{source_hash[:16]}.stages.json"


def load_stages(config: Config, source_hash: str) -> dict:
    sp = stages_path(config, source_hash)
    if sp.exists():
        try:
            return json.loads(sp.read_text(encoding="utf-8"))
        except Exception as e:
            # Corrupted stage-progress is not a silent reset — warn loudly so
            # the user knows why stages are re-running (policy 2026-06-24).
            print(f"⚠️  [stages] {sp} corrupted ({type(e).__name__}: {e}) "
                  f"— discarding stage progress, will re-run from start.")
            return {}
    return {}


def mark_stage_done(config: Config, source_hash: str, stage: str,
                    payload: dict | None = None) -> None:
    stages = load_stages(config, source_hash)
    stages[stage] = int(time.time() * 1000)
    if payload:
        stages[f"{stage}__payload"] = payload
    sp = stages_path(config, source_hash)
    tmp = sp.with_suffix(".tmp")
    tmp.write_text(json.dumps(stages, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(sp)


def get_stage_payload(config: Config, source_hash: str, stage: str) -> dict:
    return load_stages(config, source_hash).get(f"{stage}__payload", {}) or {}


def unmark_stage_done(config: Config, source_hash: str, stage: str) -> None:
    """Clear a stage-completion marker (and its payload), forcing a re-run.

    Used when a stage marker is set but its artifact cannot be recovered from
    the artifact store (e.g. an old/partial cache missing the ``file_blocks``
    key). Invalidating the marker and re-running is correct recovery — far
    better than honoring a "done" marker that would yield 0 pages and silently
    drop every concept/entity/query (the 2026-06-25 loss). Atomic tmp+rename.
    """
    stages = load_stages(config, source_hash)
    if stage not in stages and f"{stage}__payload" not in stages:
        return
    stages.pop(stage, None)
    stages.pop(f"{stage}__payload", None)
    sp = stages_path(config, source_hash)
    tmp = sp.with_suffix(".tmp")
    tmp.write_text(json.dumps(stages, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(sp)


def is_stage_done(config: Config, source_hash: str, stage: str) -> bool:
    return bool(load_stages(config, source_hash).get(stage))


# ── Project-level lock ──

class ProjectLock:
    """PID-file based mutual exclusion for a wiki project."""

    def __init__(self, config: Config, owner_id: str = ""):
        self._lock_path = config.runtime_dir / "ingest.lock"
        self._owner = owner_id or str(os.getpid())

    def _pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def __enter__(self):
        if self._lock_path.exists():
            try:
                content = self._lock_path.read_text().strip()
                parts = content.split()
                pid_str = ""
                for p in parts:
                    if p.startswith("pid="):
                        pid_str = p[4:]
                if pid_str:
                    pid = int(pid_str)
                    if self._pid_alive(pid):
                        raise RuntimeError(
                            f"Another ingest is running (PID {pid}). "
                            f"Wait for it to finish or remove {self._lock_path}"
                        )
            except RuntimeError:
                raise
            except Exception:
                pass
            self._lock_path.unlink(missing_ok=True)
        self._lock_path.write_text(f"owner={self._owner} pid={os.getpid()}")
        return self

    def __exit__(self, *args):
        if self._lock_path.exists():
            self._lock_path.unlink(missing_ok=True)

    # Backward-compatible acquire/release for non-context-manager usage
    def acquire(self, timeout: float = 0) -> bool:
        """Acquire the lock. Returns True on success, False if already held.
        timeout: seconds to wait for a stale lock (ignored — stale locks auto-release).
        """
        try:
            self.__enter__()
            return True
        except RuntimeError:
            return False

    def release(self) -> None:
        """Release the lock."""
        self.__exit__(None, None, None)


# A2 (audit 2026-07-02, M7): lint-generated placeholder pages must not enter
# the linkable/existing-pages lists — they occupy high-value slugs, soak up
# real links, and get narrated as knowledge pages. Two signals:
#   - queries/ stems with the legacy date-prefixed garbage pattern
#     (`2026-06-16-…-001`), a 2026-06 lint-stub残留;
#   - frontmatter `type: query` + a tags list containing `stub` or `lint`
#     (the exact shape _lint_fixes.ensure_broken_link_stub writes, wherever
#     the stub lands — queries/ or a nested concepts/ path).
_LINT_STUB_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")
_LINT_STUB_TYPE_RE = re.compile(r"^type:\s*['\"]?query['\"]?\s*$", re.MULTILINE)
_LINT_STUB_TAGS_RE = re.compile(r"^tags:\s*\[([^\]\n]*)\]", re.MULTILINE)


def _is_lint_stub_page(f: Path) -> bool:
    """Cheap head-only check for a lint stub (`type: query` + stub/lint tag).

    Reads at most 512 bytes; any read error → NOT a stub (conservative:
    never drop a real page on I/O trouble).
    """
    try:
        with f.open("r", encoding="utf-8", errors="ignore") as fh:
            head = fh.read(512)
    except OSError:
        return False
    if not head.startswith("---"):
        return False
    if not _LINT_STUB_TYPE_RE.search(head):
        return False
    m = _LINT_STUB_TAGS_RE.search(head)
    if not m:
        return False
    tags = {t.strip().strip("'\"").lower() for t in m.group(1).split(",")}
    return bool({"stub", "lint"} & tags)


def list_existing_slugs(config: Config) -> list[str]:
    """Stems of existing knowledge pages under wiki/, for the digest's
    existing-pages context and incremental-association detection.

    Excludes non-knowledge artifacts so they don't pollute the list fed to
    the Stage 2.1/2.2 LLM:
      - wiki/REVIEW/**  — review/audit item pages (date-prefixed suggestion,
        missing-page, contradiction, duplicate, confirm files + _audit_*)
      - wiki/clusters/** — graph.py-generated per-community hub pages
        (type: index). These are DERIVED graph artifacts, not knowledge content
        (graph.py itself excludes them via GRAPH_SKIP_DIRS to avoid ingesting
        its own output); the ingest must not feed them to the dedup/association
        LLM as if they were existing concept/entity pages.
      - wiki/lint/** and wiki/media/** — lint and image artifacts.
      - stems starting with '_' (system/audit files)
      - aggregate anchor files (index, log, overview, schema)
      - lint stub placeholder pages (type: query + stub/lint tags) and
        date-prefixed garbage query slugs (audit M7, 2026-07-02)
    """
    if not config.wiki_dir.exists():
        return []
    # Derived/artifact dirs — shared constant (_paths.WIKI_ARTIFACT_DIRS) so
    # graph output, review items, lint, and media are never treated as
    # knowledge pages. Note: checks ALL path parts (not just parts[0]).
    artifact_dirs = _WIKI_ARTIFACT_DIRS
    anchors = {"index", "log", "overview", "schema"}
    slugs: list[str] = []
    for f in config.wiki_dir.rglob("*.md"):
        if artifact_dirs.intersection(f.parts):
            continue
        stem = f.stem
        if stem.startswith("_") or stem in anchors:
            continue
        # A2/M7: lint stubs pollute the linkable list — filter them out.
        # Date-garbage pattern applies to queries/ only (news-clip sources may
        # legitimately carry date-prefixed stems).
        if f.parent.name == "queries" and _LINT_STUB_DATE_RE.match(stem):
            continue
        if _is_lint_stub_page(f):
            continue
        slugs.append(stem)
    # Sort so downstream `[:N]` truncation is deterministic. Without sorting,
    # rglob filesystem order varies across runs (especially after lint merges
    # rewrite pages), which changes the Stage 2.4 linkable-pages set and
    # thrashes the 2.4 conversation-handoff slug forever (cache never hits).
    slugs.sort()
    return slugs


# Base page-type folders always valid regardless of schema (the fixed core types).
BASE_PAGE_DIRS = {
    "sources", "concepts", "entities", "queries", "comparisons",
    "synthesis", "findings", "thesis", "methodology",
}


def load_schema_md(config: Config) -> str:
    """Raw schema.md text, or '' if absent.

    schema.md lives at the project root (NashSU); the wiki/ location
    is read as a back-compat fallback.
    """
    for p in (config.wiki_root / "schema.md", config.wiki_dir / "schema.md"):
        try:
            if p.exists():
                return p.read_text(encoding="utf-8")
        except OSError:
            pass
    return ""


def schema_folders(schema_text: str) -> set[str]:
    """Folder names declared in schema.md (the part after ``wiki/`` in each table
    row), e.g. ``{'sources', 'methodology', 'people'}``.

    NashSU schema-driven routing: the schema defines which typed folders exist for
    this project. Used to (a) tell the generation LLM which folders it may route
    pages into and (b) let the writer accept those pages instead of dropping them.
    """
    return set(re.findall(r"wiki/([a-z0-9][a-z0-9_-]*)", schema_text or ""))


# Canonical page-type → folder map for the fixed base types (NashSU
# WIKI_TYPE_DIRS parity). Schema-declared custom types extend/override this via
# parse_wiki_schema_routing(); the project schema wins on conflict.
BASE_TYPE_TO_DIR = {
    "source": "sources", "concept": "concepts", "entity": "entities",
    "query": "queries", "comparison": "comparisons", "synthesis": "synthesis",
    "finding": "findings", "thesis": "thesis", "methodology": "methodology",
}

_SCHEMA_TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]*$", re.IGNORECASE)


def parse_wiki_schema_routing(schema_text: str) -> dict[str, str]:
    """Parse the ``## Page Types`` table of schema.md into a ``{type: dir}`` map
    (NashSU ``wiki-schema.ts`` ``parseWikiSchemaRouting`` parity).

    Scoped to the first heading whose text is "Page Types" (case-insensitive),
    consuming rows until the next heading at the same-or-shallower level. Within
    that section only pipe-delimited rows are read; the leading/trailing ``|``
    cells are dropped, giving cells[0]=type, cells[1]=dir. A row is kept only
    when type matches ``/^[a-z][a-z0-9_-]*$/i`` and dir is exactly ``wiki`` or
    starts with ``wiki/``. Dirs are returned BARE (no ``wiki/`` prefix, no
    trailing slash; ``wiki`` → ``""`` = wiki root) to match the writer's
    wiki-relative path space. Returns ``{}`` when nothing parses, so routing is
    a no-op (NashSU-aligned: absent/empty schema → no validation).

    Unlike the loose ``schema_folders()`` (whole-text folder-name scan, used for
    the writer accept-list), this builds the precise type↔dir pairing the
    validator needs.
    """
    lines = (schema_text or "").split("\n")
    start, heading_level = -1, 6
    for i, raw in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.+?)\s*#*$", raw.strip())
        if m and re.match(r"^page\s+types$", m.group(2).strip(), re.IGNORECASE):
            start, heading_level = i, len(m.group(1))
            break
    if start < 0:
        return {}
    type_dirs: dict[str, str] = {}
    for raw in lines[start + 1:]:
        h = re.match(r"^(#{1,6})\s+", raw.strip())
        if h and len(h.group(1)) <= heading_level:
            break
        if not raw.strip().startswith("|"):
            continue
        cells = [c.strip() for c in raw.split("|")[1:-1]]
        if len(cells) < 2:
            continue
        ptype, pdir = cells[0], cells[1]
        if not _SCHEMA_TYPE_RE.match(ptype):
            continue
        if pdir != "wiki" and not pdir.startswith("wiki/"):
            continue
        bare = ("" if pdir == "wiki" else pdir[len("wiki/"):]).rstrip("/")
        # Defense-in-depth: a typo'd/malicious dir cell must not escape wiki/
        # (e.g. `wiki/../etc`). schema.md is project-authored, but a bad row
        # should be dropped, not turned into a path-traversal write target.
        if bare.startswith("/") or any(seg == ".." for seg in bare.split("/")):
            continue
        type_dirs[ptype] = bare
    return type_dirs


def schema_route_dir(fm_type: str, routing: dict[str, str]) -> str | None:
    """Authoritative bare folder for a page ``type``: schema-declared typeDirs
    first, then the fixed base map. ``None`` if the type is unknown (unroutable —
    the caller should leave the page where it is rather than guess)."""
    if not fm_type:
        return None
    if fm_type in routing:
        return routing[fm_type]
    return BASE_TYPE_TO_DIR.get(fm_type)


def validate_wiki_page_routing(rel_path: str, fm_type: str,
                               routing: dict[str, str]) -> str | None:
    """NashSU ``validateWikiPageRouting`` parity — return an issue string when a
    page's frontmatter ``type`` disagrees with its directory under the project
    schema, else ``None``.

    Bidirectional: (a) a schema-declared type sitting outside its declared dir;
    (b) a page inside a schema-declared dir carrying a different type. ``rel_path``
    is wiki-relative (a leading ``wiki/`` is tolerated). An empty ``fm_type`` is
    never an issue (untyped pages are allowed). With an empty ``routing`` (no
    schema.md) this is always ``None`` — NashSU-aligned.
    """
    fm_type = (fm_type or "").strip().strip('"').strip("'")
    if not fm_type:
        return None
    norm = rel_path.replace("\\", "/").lstrip("/")
    if norm.startswith("wiki/"):
        norm = norm[len("wiki/"):]
    actual_dir = norm.rsplit("/", 1)[0] if "/" in norm else ""
    expected = routing.get(fm_type)
    if expected is not None and actual_dir != expected:
        return (f'type "{fm_type}" must be under "{expected or "(wiki root)"}/", '
                f'not "{actual_dir or "(wiki root)"}/"')
    for t, d in routing.items():
        if d == actual_dir and t != fm_type:
            return (f'pages under "{actual_dir or "(wiki root)"}/" must use '
                    f'type "{t}", but found "{fm_type}"')
    return None


# ── Path safety (NashSU parity: isSafeIngestPath) ──

_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"}
for _i in range(1, 10):
    _WINDOWS_RESERVED.add(f"com{_i}")
    _WINDOWS_RESERVED.add(f"lpt{_i}")

_ILLEGAL_CHARS_RE = re.compile(r'[<>:"|?*\x00-\x1f]')


def is_safe_ingest_path(rel_path: str) -> bool:
    """Reject paths that are unsafe to write to the wiki/ directory.

    NashSU checks (ingest.ts L290-306):
      - No control/NUL bytes
      - Not an absolute path (POSIX /, Windows drive, UNC)
      - No .. segments
      - No segments ending with space or .
      - No Windows reserved device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9)
      - No Windows illegal characters (<>:"|?*)
      - No garbage slugs from LLM empty/malformed titles
    """
    if not rel_path or _ILLEGAL_CHARS_RE.search(rel_path):
        return False
    if rel_path.startswith("/") or rel_path.startswith("\\"):
        return False
    if len(rel_path) >= 2 and rel_path[1] == ":":
        return False
    if rel_path.startswith("\\\\"):
        return False
    if ".." in rel_path.split("/") or ".." in rel_path.split("\\"):
        return False
    for segment in rel_path.replace("\\", "/").split("/"):
        if not segment:
            continue
        if segment.endswith(" ") or segment.endswith("."):
            return False
        # Windows reserves device names with OR without an extension
        # (CON, CON.md both refer to the device), so test the pre-dot stem too.
        seg_stem = segment.split(".", 1)[0].lower()
        if segment.lower() in _WINDOWS_RESERVED or seg_stem in _WINDOWS_RESERVED:
            return False
    # Reject empty/garbage filenames. Path(".md").stem == ".md" (Python treats a
    # leading-dot name as extension-less), so derive the real base name directly.
    name = Path(rel_path).name
    base = name[:-3] if name.endswith(".md") else Path(rel_path).stem
    base = base.strip().strip(".").lower()
    if base in ("", "-", "--", "none", "null", "undefined", "n-a", "n/a"):
        return False
    if re.match(r'^\(.*\)$', base):
        return False
    return True


def source_slug_from_raw_path(raw_path: str | Path, wiki_root: str | Path) -> Path | None:
    """Derive the expected source page path from a raw file path.

    Used for dedup before ingest (SKILL.md gate 0.1): check whether
    ``wiki/sources/<slug>.md`` already exists.

    Algorithm (NashSU source-identity.ts parity, improved-wiki layout):
      1. Resolve ``raw_path`` relative to ``<wiki_root>/raw/``
      2. Replace extension with ``.md``
      3. Return full path under ``<wiki_root>/wiki/sources/``

    Returns ``None`` if the raw path is outside the project's raw/ tree.

    Example:
        >>> source_slug_from_raw_path(
        ...     "raw/Book/RF Circuit Design - 2008 - Bowick.pdf",
        ...     "/home/user/HardwareWiki",
        ... )
        Path("/home/user/HardwareWiki/wiki/sources/Book/RF Circuit Design - 2008 - Bowick.md")

    For dedup:
        expected = source_slug_from_raw_path(raw_file, config.wiki_root)
        if expected and expected.exists():
            print(f"Already ingested: {expected}")
    """
    wiki_root = Path(wiki_root).expanduser()
    raw_path_obj = Path(raw_path).expanduser()
    raw_root = wiki_root / "raw"

    # Resolve relative paths against the project's raw/ root, matching how
    # ``ingest.py "raw/Book/file.pdf"`` would be called from project dir.
    if not raw_path_obj.is_absolute():
        raw_path_obj = raw_root / raw_path_obj

    try:
        rel = raw_path_obj.relative_to(raw_root).with_suffix(".md")
        # Python Path.relative_to does pure string prefix removal — it doesn't
        # detect ".." traversal. Reject any result whose parts contain "..".
        if ".." in rel.parts:
            return None
    except ValueError:
        return None

    return wiki_root / "wiki" / "sources" / rel


# ── Parse helpers (moved from ingest.py) ──

def parse_yaml_block(response: str) -> dict:
    """Extract the first YAML block from the LLM response."""
    m = re.search(r"```yaml\s*\n(.*?)\n```", response, re.DOTALL)
    yaml_text = m.group(1) if m else response
    # Deferred import: _stage_1_1_scanned imports Config from this module,
    # so a top-level import here would be circular.
    from _stage_1_1_scanned import _decode_html_entities
    yaml_text = _decode_html_entities(yaml_text)
    try:
        import yaml
        return yaml.safe_load(yaml_text) or {}
    except ImportError:
        return parse_simple_yaml(yaml_text)
    except Exception:
        print(f"[parse] yaml.safe_load failed — falling back to simple parser")
        return parse_simple_yaml(yaml_text)


def _yaml_is_blank_or_comment(line: str) -> bool:
    s = line.strip()
    return (not s) or s.startswith("#")


def _yaml_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _yaml_scalar(s: str) -> Any:
    """Parse a YAML scalar: inline flow list/map, quoted string, or bare text.
    Surrounding matching quotes are stripped (the prior flat parser kept them,
    but downstream consumers slugify/strip names, so stripping is safer)."""
    s = s.strip()
    if not s:
        return ""
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_yaml_scalar(p) for p in _yaml_split_flow(inner)]
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        d: dict[str, Any] = {}
        for part in _yaml_split_flow(inner):
            if ":" in part:
                k, v = part.split(":", 1)
                d[k.strip()] = _yaml_scalar(v)
        return d
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


def _yaml_split_flow(inner: str) -> list[str]:
    """Split a flow-collection body on top-level commas (ignores commas inside
    nested [], {}, or quotes)."""
    parts, buf, depth, quote = [], [], 0, ""
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
        elif ch in ('"', "'"):
            quote = ch
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    return [p for p in parts if p]


def _yaml_dedent_block(block_lines: list[str]) -> str:
    while block_lines and not block_lines[-1].strip():
        block_lines.pop()
    indents = [_yaml_indent(l) for l in block_lines if l.strip()]
    base = min(indents) if indents else 0
    return "\n".join(l[base:] if len(l) >= base else l for l in block_lines)


def _yaml_parse_block(lines: list[str], i: int, min_indent: int) -> tuple[Any, int]:
    n = len(lines)
    while i < n and _yaml_is_blank_or_comment(lines[i]):
        i += 1
    if i >= n or _yaml_indent(lines[i]) < min_indent:
        return {}, i
    if lines[i].strip().startswith("- "):
        return _yaml_parse_list(lines, i, _yaml_indent(lines[i]))
    return _yaml_parse_map(lines, i, _yaml_indent(lines[i]))


_YAML_KEY_RE = re.compile(r"^([\w][\w_\-./]*):\s?(.*)$")
_YAML_BLOCK_SCALAR = {"|", ">", "|-", ">-", "|+", ">+"}


def _yaml_parse_map(lines: list[str], i: int, indent: int) -> tuple[dict, int]:
    result: dict[str, Any] = {}
    n = len(lines)
    while i < n:
        if _yaml_is_blank_or_comment(lines[i]):
            i += 1
            continue
        cur = _yaml_indent(lines[i])
        if cur < indent:
            break
        if cur > indent:
            i += 1
            continue
        s = lines[i].strip()
        if s.startswith("- "):
            break
        m = _YAML_KEY_RE.match(s)
        if not m:
            i += 1
            continue
        key, val_str = m.group(1), m.group(2).strip()
        i += 1
        if val_str in _YAML_BLOCK_SCALAR:
            block: list[str] = []
            while i < n and (_yaml_is_blank_or_comment(lines[i]) or _yaml_indent(lines[i]) > indent):
                block.append(lines[i])
                i += 1
            result[key] = _yaml_dedent_block(block)
        elif val_str == "":
            j = i
            while j < n and _yaml_is_blank_or_comment(lines[j]):
                j += 1
            if j < n and _yaml_indent(lines[j]) > indent:
                child, i = _yaml_parse_block(lines, i, indent + 1)
                result[key] = child
            else:
                result[key] = []
        else:
            result[key] = _yaml_scalar(val_str)
    return result, i


def _yaml_parse_list(lines: list[str], i: int, indent: int) -> tuple[list, int]:
    items: list[Any] = []
    n = len(lines)
    while i < n:
        if _yaml_is_blank_or_comment(lines[i]):
            i += 1
            continue
        cur = _yaml_indent(lines[i])
        if cur != indent or not lines[i].strip().startswith("- "):
            break
        rest = lines[i].strip()[2:]
        if _YAML_KEY_RE.match(rest):
            # Map item: synthesize the first key (at indent+2) plus every
            # deeper line belonging to this item.
            item_indent = indent + 2
            sub = [(" " * item_indent) + rest]
            i += 1
            while i < n and (_yaml_is_blank_or_comment(lines[i]) or _yaml_indent(lines[i]) > indent):
                sub.append(lines[i])
                i += 1
            val, _ = _yaml_parse_map(sub, 0, item_indent)
            items.append(val)
        else:
            items.append(_yaml_scalar(rest))
            i += 1
    return items, i


def parse_simple_yaml(text: str):
    """Indentation-aware parser for the YAML SUBSET the ingest prompts emit.
    Used ONLY when PyYAML is unavailable (or yaml.safe_load crashes on CJK
    curly quotes). Unlike the prior flat parser — which collapsed every list
    item to a bare string and so dropped the nested dicts in concepts_found /
    entities_found / review items, leaving Stage 2.4 generation and Stage 3.4
    review with empty inputs — this handles nested maps, lists of maps, block
    scalars (| / >), inline flow collections, and a top-level list (review
    YAML). Returns a dict or list mirroring yaml.safe_load's shape."""
    value, _ = _yaml_parse_block(text.split("\n"), 0, 0)
    return value


def parse_file_blocks(response: str) -> list[tuple[str, str]]:
    """Extract wiki page blocks from the LLM response.

    Supports two formats:
    1. NashSU native:  ---FILE:wiki/<path>--- ... ---END FILE---
    2. Legacy:         ### File N: <path>.md ...
    """
    # NashSU parity: normalize CRLF before parsing (ingest.ts L361)
    response = response.replace("\r\n", "\n")
    blocks: list[tuple[str, str]] = []

    # Format 1: NashSU-style ---FILE:wiki/<path>--- ... ---END FILE---
    # NashSU parity: fence-aware parsing (ingest.ts L377-400) — track CommonMark
    # code fences so ---END FILE--- inside a code block doesn't close the outer block.
    # Accept both ---FILE:wiki/concepts/X.md--- (correct) and
    # ---FILE:concepts/X.md--- (LLM forgot wiki/ prefix; auto-correct strips it either way)
    # NashSU parity (ingest.ts L263-264, L331): markers are case-insensitive and
    # tolerant of interior whitespace (`--- END FILE ---`, `---end file---`,
    # `--- FILE: path ---`). Fence delimiters follow CommonMark: 3+ backticks or
    # tildes, ≤3 leading spaces; a fence closes only on the SAME char repeated at
    # least as many times, so a 3-tick run can't close a 4-tick opener.
    FILE_HEADER_RE = re.compile(r'^---\s*FILE:\s*(wiki/)?(.+?)\s*---\s*$', re.IGNORECASE)
    END_FILE_RE = re.compile(r'^---\s*END\s+FILE\s*---\s*$', re.IGNORECASE)
    FENCE_RE = re.compile(r'^\s{0,3}(`{3,}|~{3,})')

    # Known wiki subdirectories (must match WIKI_TYPE_DIRS)
    _KNOWN_SUBDIRS = (
        "sources", "concepts", "entities", "queries", "comparisons",
        "synthesis", "findings", "thesis", "methodology",
    )

    lines = response.split("\n")
    fence_marker: str | None = None  # the fence CHAR currently open ('`' or '~')
    fence_len = 0                    # its run length (CommonMark close rule)
    current_path: str | None = None
    current_lines: list[str] = []

    for line in lines:
        # Track CommonMark code fences (still add the line to content).
        # A fence closes only on the SAME char repeated at least as many times,
        # so a 3-tick run inside a 4-tick block doesn't truncate the page.
        is_fence_line = False
        fm = FENCE_RE.match(line)
        if fm:
            run = fm.group(1)
            char = run[0]
            length = len(run)
            if fence_marker is None:
                fence_marker = char
                fence_len = length
            elif char == fence_marker and length >= fence_len:
                fence_marker = None
                fence_len = 0
            is_fence_line = True

        # Only match FILE/END FILE headers outside fences
        if fence_marker is None and not is_fence_line:
            end_match = END_FILE_RE.match(line)
            if end_match and current_path is not None:
                content = "\n".join(current_lines).rstrip() + "\n"
                blocks.append((current_path, content))
                current_path = None
                current_lines = []
                continue

            file_match = FILE_HEADER_RE.match(line)
            if file_match:
                if current_path is not None:
                    # Unclosed previous block — flush it with warning (H2)
                    content = "\n".join(current_lines).rstrip() + "\n"
                    print(f"  [parse] FILE block \"{current_path}\" was not closed "
                          f"before next block — likely missing END FILE marker. "
                          f"Block kept as-is.")
                    blocks.append((current_path, content))
                # group(1) = optional "wiki/" prefix, group(2) = actual path
                path = file_match.group(2).strip()
                # H6 fix: surface empty-path blocks instead of silently dropping.
                if not path:
                    print(f"  [parse] FILE block with empty path skipped "
                          f"(LLM omitted the path after ---FILE:).")
                    current_path = None
                    current_lines = []
                    continue
                if not path.endswith(".md"):
                    current_path = None
                    current_lines = []
                    continue
                # Normalize: if path has more than 2 segments (subdir/.../file.md),
                # merge extra segments into filename by replacing / with -.
                # Exception: sources/ keeps its category subdirectory (e.g. sources/book/x.md)
                parts = path.split("/")
                if len(parts) > 2:
                    subdir = parts[0]
                    if subdir == "sources":
                        # Preserve: sources/book/slug.md → keep as-is
                        pass
                    else:
                        merged_slug = "-".join(parts[1:])
                        corrected = f"{subdir}/{merged_slug}"
                        print(f"  [parse] merged / in slug: {path} → {corrected}")
                        path = corrected
                # Auto-correct LLM hyphen-for-slash error (subdir-slug → subdir/slug)
                for subdir in _KNOWN_SUBDIRS:
                    prefix = f"{subdir}-"
                    if path.startswith(prefix):
                        corrected = f"{subdir}/{path[len(prefix):]}"
                        print(f"  [parse] corrected path: {path} → {corrected}")
                        path = corrected
                        break
                # Validate path safety (NashSU parity)
                if not is_safe_ingest_path(path):
                    print(f"  [parse] unsafe path rejected: {path}")
                    current_path = None
                    current_lines = []
                    continue
                current_path = path
                current_lines = []
                continue

        # Collect content lines for current block
        if current_path is not None:
            current_lines.append(line)

    # Flush last unclosed block (H2: tolerant, but warn — NashSU parity)
    if current_path is not None and current_lines:
        print(f"  [parse] FILE block \"{current_path}\" was not closed before "
              f"end of stream — likely truncation (model hit max_tokens, "
              f"timeout, or connection dropped). Block kept as-is.")
        content = "\n".join(current_lines).rstrip() + "\n"
        blocks.append((current_path, content))

    if blocks:
        return blocks

    # Format 2: Legacy ### File N: <path>.md
    HEADER_RE = re.compile(r"^###\s+File\s+(\d+):\s*([^\n]+\.md)\s*$", re.MULTILINE)
    matches = list(HEADER_RE.finditer(response))
    for i, m in enumerate(matches):
        path = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(response)
        content = response[start:end].rstrip() + "\n"
        if path.startswith("wiki/"):
            path = path[len("wiki/"):]
        if not path.endswith(".md"):
            continue
        if not is_safe_ingest_path(path):
            print(f"  [parse] unsafe path rejected: {path}")
            continue
        blocks.append((path, content))
    return blocks

# ══════════════════════════════════════════════════════════════════════════════
# Shared utilities (used by multiple stage modules)
# ══════════════════════════════════════════════════════════════════════════════

def slugify(text: str) -> str:
    """Convert a concept/entity name to a kebab-case wiki slug.

    Standardized across all stage modules. Used in 15+ places. Strips the
    same Windows-illegal characters is_safe_ingest_path() rejects (e.g. a
    book-title entity like "...Volume III: Physics-Based Methods" would
    otherwise produce a colon-bearing slug that the FILE-block parser
    silently drops the page for).
    """
    # NFKC-normalize first so full-width CJK punctuation/digits fold to their
    # half-width equivalents before slugging (NashSU wiki-filename.ts parity).
    import unicodedata
    text = unicodedata.normalize("NFKC", text)
    slug = text.lower().replace(" ", "-").replace("/", "-")
    slug = _ILLEGAL_CHARS_RE.sub("", slug)
    # Strip interior punctuation that doesn't belong in slugs: commas, ampersands,
    # periods, semicolons, apostrophes (e.g. "Energy, Work, and Power" ->
    # "energy-work-and-power", "Taylor & Francis Ltd." -> "taylor-francis-ltd",
    # "The Fairmont Press, Inc." -> "the-fairmont-press-inc"). The illegal-char
    # strip above leaves these, producing comma/ampersand-bearing filenames.
    slug = re.sub(r"[,.&;!'`]+", "-", slug)
    # Collapse interior brackets/parentheses (ASCII + full-width) into hyphens.
    # The illegal-char strip above leaves them, and the trailing-edge strip
    # below only removes the LAST one, so "Total Module Power (TMP)" became the
    # malformed "total-module-power-(tmp" (interior "(" kept, trailing ")"
    # stripped). Converting them up front yields a clean "total-module-power-tmp".
    slug = re.sub(r"[()\[\]{}（）【】]+", "-", slug)
    # Keep Unicode letters/digits (CJK, Cyrillic, …) plus ASCII hyphen/underscore;
    # drop everything else (emoji, residual punctuation). NashSU wiki-filename.ts
    # parity: a non-Latin title must NOT collapse to an empty slug. The old
    # ASCII-only edge-strips (^[^a-z0-9]+ / [^a-z0-9]+$) deleted leading/trailing
    # CJK, turning "贴片电阻" into "" (colliding empty slugs) and "电感DCR" into "dcr".
    # The comma/bracket→hyphen substitutions above already ran, so dropping the
    # remaining non-slug chars here preserves intended word boundaries.
    slug = "".join(ch for ch in slug if ch in "-_" or ch.isalnum())
    # Collapse doubled hyphens (from bracket/space substitution) and trim edges.
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug


# (atomic_write lives in _paths.py; re-exported near the top of this module.)


def call_with_retry(fn, max_retries: int = 3, base_wait: float = 1.0, label: str = ""):
    """Call a function with exponential-backoff retry and jitter.

    Replaces 5 copy-pasted retry loops across Phase 2.
    """
    import time
    from _llm_api import _retry_jitter, _is_retryable_exception
    last_err = None
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if _is_retryable_exception(e) and attempt < max_retries - 1:
                # _retry_jitter(base_wait, attempt) RETURNS the jittered
                # exponential backoff (base_wait * 2**attempt * jitter); sleep on
                # it. (Was buggy: called as _retry_jitter(attempt, label) — wrong
                # arg order, a str where an int was expected (TypeError on the
                # first retry), and the return value discarded; the actual sleep
                # used base_wait ** (attempt+1), which with the default
                # base_wait=1.0 is always 1.0 — no backoff.)
                time.sleep(_retry_jitter(base_wait, attempt))
                continue
            raise
    raise last_err  # unreachable but satisfies type checker
