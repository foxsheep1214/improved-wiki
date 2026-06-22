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
    """Raised in --conversation mode when a prompt is written and awaits agent.

    Subclasses BaseException (not Exception) so the broad ``except Exception``
    retry/fallback blocks around LLM calls in the stage modules do NOT swallow
    it — ConversationPending is control flow (pause for the calling agent),
    not a transient HTTP error. It still propagates to the top-level
    ``except ConversationPending`` handler (ingest.py main) which exits 101.
    """


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
        except Exception:
            pass
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
            caption_name = cfg.get("caption_provider") or cfg.get("default", "minimax")
            provider = cfg.get("providers", {}).get(caption_name)
            if provider:
                models = provider.get("models", {})
                return {
                    "api_key": provider.get("api_key", ""),
                    "base_url": provider.get("base_url", "https://api.minimaxi.com"),
                    "model": models.get("caption") or models.get("vision") or provider.get("model", "MiniMax-M3"),
                    "protocol": provider.get("protocol", "anthropic"),
                    "provider": caption_name,
                }
        except Exception:
            pass
    return {
        "api_key": os.environ.get("CAPTION_API_KEY") or os.environ.get("LLM_API_KEY", ""),
        "base_url": "https://api.minimaxi.com",
        "model": "MiniMax-M3",
        "protocol": "anthropic",
        "provider": "minimax",
    }


# ── NashSU-aligned context budget (ported from llm_wiki/src/lib/context-budget.ts + ingest.ts) ──
# Set LLM_CONTEXT_SIZE to your model's context window (in chars). All budgets derive from this.
# DeepSeek V4 Pro: LLM_CONTEXT_SIZE=1000000 (1M tokens ≈ chars for budget math)
# If unset, falls back to model-name pattern matching + hardcoded defaults.
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
_TARGET_CHARS_MAX = 60_000
_TARGET_CHARS_FRAC = 0.55


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
    max_tokens: int
    context_size: int | None = None
    conversation_mode: bool = False
    conversation_prefix: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        wiki_root = Path(os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd())).expanduser()
        provider = load_provider_config()
        caption = load_caption_provider()
        runtime_dir = detect_runtime_dir(wiki_root)

        # ── NashSU-aligned context budget ──
        cs_env = os.environ.get("LLM_CONTEXT_SIZE")
        context_size = int(cs_env) if cs_env else None

        if context_size:
            # sourceBudget = maxCtx - responseReserve - stableReserve - instructionReserve
            # clamped to [SOURCE_BUDGET_MIN, min(SOURCE_BUDGET_MAX, maxCtx * 0.6)]
            cs = context_size
            response_reserve = int(cs * _RESPONSE_RESERVE_FRAC)
            stable_reserve = min(int(cs * _STABLE_RESERVE_FRAC), max(_STABLE_RESERVE_MIN, 50_000))
            instruction_reserve = max(_INSTRUCTION_RESERVE_MIN, int(cs * _INSTRUCTION_RESERVE_FRAC))
            available = cs - response_reserve - stable_reserve - instruction_reserve
            upper = min(_SOURCE_BUDGET_MAX, max(_SOURCE_BUDGET_MIN, int(cs * _SOURCE_BUDGET_FRAC)))
            source_budget = max(_SOURCE_BUDGET_MIN, min(available, upper))

            # targetChars = sourceBudget * 0.55, clamped [12K, 60K]
            target_chars = max(_TARGET_CHARS_MIN,
                              min(int(source_budget * _TARGET_CHARS_FRAC), _TARGET_CHARS_MAX))

            print(f"[config] LLM_CONTEXT_SIZE={context_size:,} → "
                  f"source_budget={source_budget:,} target_chars={target_chars:,} "
                  f"(NashSU-aligned)")
        else:
            # Backward-compatible hardcoded defaults (no LLM_CONTEXT_SIZE set)
            source_budget = 200_000
            target_chars = 100_000

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
            max_tokens=16384,
            context_size=context_size,
        )

    def compute_source_budget(self, stable_length: int = 50_000) -> int:
        """NashSU-aligned: per-source budget from context window."""
        cs = self.context_size or _CONTEXT_SIZE_DEFAULT
        response_reserve = int(cs * _RESPONSE_RESERVE_FRAC)
        stable_reserve = min(int(cs * _STABLE_RESERVE_FRAC), max(_STABLE_RESERVE_MIN, stable_length))
        instruction_reserve = max(_INSTRUCTION_RESERVE_MIN, int(cs * _INSTRUCTION_RESERVE_FRAC))
        available = cs - response_reserve - stable_reserve - instruction_reserve
        upper = min(_SOURCE_BUDGET_MAX, max(_SOURCE_BUDGET_MIN, int(cs * _SOURCE_BUDGET_FRAC)))
        return max(_SOURCE_BUDGET_MIN, min(available, upper))

    def compute_target_chars(self, stable_length: int = 50_000) -> int:
        """NashSU-aligned: per-source chunk target from source budget."""
        sb = self.compute_source_budget(stable_length)
        return max(_TARGET_CHARS_MIN, min(int(sb * _TARGET_CHARS_FRAC), _TARGET_CHARS_MAX))

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
        except Exception:
            pass
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
    pp = progress_path(config, source_hash)
    tmp = pp.with_suffix(".tmp")
    data["_updated_at"] = int(time.time() * 1000)
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(pp)


def clear_progress(config: Config, source_hash: str) -> None:
    pp = progress_path(config, source_hash)
    if pp.exists():
        pp.unlink()


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


# ── Domain detection ──

_DOMAIN_KEYWORDS: dict[str, str] = {
    "rf": "rf", "radio": "rf", "antenna": "rf", "microwave": "rf",
    "radar": "rf", "waveguide": "rf",
    "power": "power", "converter": "power", "inverter": "power",
    "rectifier": "power", "switching": "power", "buck": "power",
    "boost": "power", "ldo": "power",
    "analog": "analog", "op-amp": "analog", "operational amplifier": "analog",
    "adc": "analog", "dac": "analog", "pll": "analog",
    "digital": "digital", "fpga": "digital", "verilog": "digital",
    "vhdl": "digital", "cmos digital": "digital", "microcontroller": "digital",
    "signal-integrity": "signal-integrity", "signal integrity": "signal-integrity",
    "si ": "signal-integrity", "crosstalk": "signal-integrity",
    "eye diagram": "signal-integrity", "jitter": "signal-integrity",
    "emc": "emc", "emi": "emc", "electromagnetic compatibility": "emc",
    "shielding": "emc",
    "thermal": "thermal", "heat": "thermal", "cooling": "thermal",
    "heatsink": "thermal", "temperature": "thermal",
    "battery": "battery", "lithium": "battery", "soc": "battery",
    "state of charge": "battery", "bms": "battery",
    "semiconductor": "semiconductor", "mosfet": "semiconductor",
    "igbt": "semiconductor", "gan": "semiconductor", "sic": "semiconductor",
    "wafer": "semiconductor",
    "embedded": "embedded", "arm": "embedded", "cortex": "embedded",
    "rtos": "embedded", "firmware": "embedded",
}

_TEMPLATE_DOMAIN: dict[str, str] = {
    "digest-datasheet": "semiconductor",
    "digest-applicationnote": "semiconductor",
}


def detect_domain(file_path: Path, template: str,
                  global_digest: dict | None = None) -> str:
    title_lower = file_path.stem.lower()
    template_name = Path(template).name if template else ""
    if template_name in _TEMPLATE_DOMAIN:
        return _TEMPLATE_DOMAIN[template_name]
    for keyword, domain in _DOMAIN_KEYWORDS.items():
        if keyword in title_lower:
            return domain
    if global_digest:
        outline = global_digest.get("outline", [])
        outline_str = " ".join(
            (c.get("title", "") + " " + str(c.get("key_topics", ""))
             if isinstance(c, dict) else str(c))
            for c in outline
        ).lower()
        for keyword, domain in _DOMAIN_KEYWORDS.items():
            if keyword in outline_str:
                return domain
    return "general"


def list_existing_slugs(config: Config) -> list[str]:
    if not config.wiki_dir.exists():
        return []
    return [f.stem for f in config.wiki_dir.rglob("*.md")]


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
    try:
        import yaml
        return yaml.safe_load(yaml_text) or {}
    except ImportError:
        return parse_simple_yaml(yaml_text)
    except Exception:
        print(f"[parse] yaml.safe_load failed — falling back to simple parser")
        return parse_simple_yaml(yaml_text)


def parse_simple_yaml(text: str) -> dict:
    result: dict[str, Any] = {}
    current_list_key: str | None = None
    for line in text.split("\n"):
        if not line.strip() or line.strip().startswith("#"):
            continue
        if line.startswith("  - ") and current_list_key:
            # Use unwrapped value since we always set current_list_key
            result[current_list_key].append(line[4:].strip())
            continue
        m = re.match(r"^(\w[\w_]*):\s*(.*)", line)
        if m:
            key, value = m.group(1), m.group(2).strip()
            if value == "":
                result[key] = []
                current_list_key = key
            else:
                result[key] = value
                current_list_key = None
    return result


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
        "synthesis", "findings", "thesis",
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
    slug = text.lower().replace(" ", "-").replace("/", "-")
    return _ILLEGAL_CHARS_RE.sub("", slug)


def atomic_write(path, content: str, encoding: str = "utf-8") -> None:
    """Write file atomically via tmp + rename. Prevents partial writes."""
    import os
    p = str(path)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding=encoding) as f:
        f.write(content)
    os.replace(tmp, p)


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
                _retry_jitter(attempt, label)
                time.sleep(base_wait ** (attempt + 1))
                continue
            raise
    raise last_err  # unreachable but satisfies type checker
