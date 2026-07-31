"""Provider resolution and runtime configuration for improved-wiki."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from _paths import detect_runtime_dir


def load_provider_config(name: str | None = None) -> dict:
    explicit = name or os.environ.get("LLM_PROVIDER")
    if not explicit:
        agent_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get(
            "ANTHROPIC_API_KEY"
        )
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

    config_path = Path.home() / ".agents" / "config.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            if name is None:
                name = os.environ.get("LLM_PROVIDER", config.get("default", ""))
            provider = config.get("providers", {}).get(name)
            if provider:
                models = provider.get("models", {})
                return {
                    "api_key": os.environ.get("LLM_API_KEY")
                    or provider.get("api_key", ""),
                    "base_url": os.environ.get("LLM_BASE_URL")
                    or provider.get("base_url", ""),
                    "model": os.environ.get("LLM_MODEL")
                    or models.get("text", provider.get("model", "")),
                    "protocol": provider.get("protocol", "anthropic"),
                    "provider": name,
                }
        except Exception as exc:
            raise RuntimeError(
                "~/.agents/config.json exists but failed to parse "
                f"({type(exc).__name__}: {exc}) — fix or remove it. "
                "No silent fallback."
            ) from exc
    return {
        "api_key": os.environ.get("LLM_API_KEY", ""),
        "base_url": os.environ.get("LLM_BASE_URL", ""),
        "model": os.environ.get("LLM_MODEL", ""),
        "protocol": os.environ.get("LLM_PROTOCOL", "anthropic"),
        "provider": "env",
    }


def _caption_provider_entry(
    provider: dict | None,
    name: str,
) -> dict | None:
    if not provider:
        return None
    models = provider.get("models", {})
    return {
        "api_key": provider.get("api_key", ""),
        "base_url": provider.get("base_url", ""),
        "model": models.get("caption")
        or models.get("vision")
        or provider.get("model", ""),
        "protocol": provider.get("protocol", "anthropic"),
        "provider": name,
        "timeout_seconds": provider.get("timeout_seconds", 180),
    }


def load_caption_provider() -> dict:
    config_path = Path.home() / ".agents" / "config.json"
    empty = {
        "api_key": "",
        "base_url": "",
        "model": "",
        "protocol": "",
        "provider": "",
        "timeout_seconds": 180,
        "fallback": None,
    }
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            name = config.get("caption_provider") or config.get("default", "")
            primary = _caption_provider_entry(
                config.get("providers", {}).get(name),
                name,
            )
            if primary:
                fallback_name = config.get("caption_fallback_provider", "")
                primary["fallback"] = (
                    _caption_provider_entry(
                        config.get("providers", {}).get(fallback_name),
                        fallback_name,
                    )
                    if fallback_name
                    else None
                )
                return primary
        except Exception as exc:
            raise RuntimeError(
                "~/.agents/config.json exists but failed to parse "
                f"({type(exc).__name__}: {exc}) — fix or remove it. "
                "No silent fallback."
            ) from exc
    return empty


_CONTEXT_SIZE_DEFAULT = 200_000
_RESPONSE_RESERVE_FRAC = 0.15
_STABLE_RESERVE_MIN = 12_000
_STABLE_RESERVE_FRAC = 0.25
_INSTRUCTION_RESERVE_MIN = 12_000
_INSTRUCTION_RESERVE_FRAC = 0.08
_SOURCE_BUDGET_MIN = 8_000
_SOURCE_BUDGET_MAX = 300_000
_SOURCE_BUDGET_FRAC = 0.6
_TARGET_TOKENS_MIN = 12_000
_TARGET_TOKENS_CEIL_FRAC = 0.33
_TARGET_TOKENS_HARD_CEIL = 64_000
_MAX_CHARS_PER_TOKEN = 4
_TARGET_CHARS_HARD_CEIL = 768_000


def _compute_chunk_targets(
    source_budget: int,
    context_size: int,
    hard_ceil: int = _TARGET_TOKENS_HARD_CEIL,
) -> tuple[int, int]:
    """Return the token budget and conservative character ceiling per chunk."""
    del source_budget  # retained in the API for compatibility
    ceiling_env = os.environ.get(
        "IMPROVED_WIKI_TARGET_TOKENS_CEIL",
        "",
    ).strip()
    resolved_hard_ceil = (
        int(ceiling_env) if ceiling_env.isdigit() else hard_ceil
    )
    target_tokens = min(
        resolved_hard_ceil,
        max(
            _TARGET_TOKENS_MIN,
            int(context_size * _TARGET_TOKENS_CEIL_FRAC),
        ),
    )
    target_chars = min(
        _TARGET_CHARS_HARD_CEIL,
        target_tokens * _MAX_CHARS_PER_TOKEN,
    )
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
    llm_model: str
    caption_api_key: str
    caption_base_url: str
    caption_model: str
    chunk_overlap: int
    source_budget: int
    target_chars: int
    target_tokens: int
    max_tokens: int
    context_size: int | None = None
    conversation_prefix: str = ""
    caption_protocol: str = "anthropic"
    caption_fallback_api_key: str = ""
    caption_fallback_base_url: str = ""
    caption_fallback_model: str = ""
    caption_fallback_protocol: str = ""
    caption_timeout_seconds: int = 180
    caption_fallback_timeout_seconds: int = 180
    media_policy: str = "required"

    @classmethod
    def from_env(cls) -> "Config":
        wiki_root = Path(
            os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd())
        ).expanduser()
        provider = load_provider_config()
        caption = load_caption_provider()
        runtime_dir = detect_runtime_dir(wiki_root)
        source_budget = _CONTEXT_SIZE_DEFAULT
        target_tokens, target_chars = _compute_chunk_targets(
            source_budget,
            _CONTEXT_SIZE_DEFAULT,
        )
        media_policy = os.environ.get(
            "IMPROVED_WIKI_MEDIA_POLICY",
            "required",
        ).strip().lower()
        if media_policy not in {"required", "best_effort", "off"}:
            raise RuntimeError(
                "IMPROVED_WIKI_MEDIA_POLICY must be one of: "
                "required, best_effort, off"
            )
        fallback = caption.get("fallback") or {}
        return cls(
            wiki_root=wiki_root,
            raw_root=wiki_root / "raw",
            wiki_dir=wiki_root / "wiki",
            runtime_dir=runtime_dir,
            cache_path=runtime_dir / "ingest-cache.json",
            progress_dir=runtime_dir / "ingest-progress",
            extract_tmp_dir=runtime_dir / "extract-tmp",
            llm_model=provider["model"],
            caption_api_key=caption["api_key"],
            caption_base_url=caption["base_url"],
            caption_model=caption["model"],
            caption_protocol=caption.get("protocol", "anthropic"),
            caption_timeout_seconds=caption.get("timeout_seconds", 180),
            caption_fallback_api_key=fallback.get("api_key", ""),
            caption_fallback_base_url=fallback.get("base_url", ""),
            caption_fallback_model=fallback.get("model", ""),
            caption_fallback_protocol=fallback.get("protocol", ""),
            caption_fallback_timeout_seconds=fallback.get(
                "timeout_seconds",
                180,
            ),
            chunk_overlap=3_000,
            source_budget=source_budget,
            target_chars=target_chars,
            target_tokens=target_tokens,
            max_tokens=16384,
            context_size=None,
            media_policy=media_policy,
        )

    def apply_context(self, context_size: int) -> None:
        self.context_size = context_size
        response_reserve = int(context_size * _RESPONSE_RESERVE_FRAC)
        stable_reserve = min(
            int(context_size * _STABLE_RESERVE_FRAC),
            max(_STABLE_RESERVE_MIN, 50_000),
        )
        instruction_reserve = max(
            _INSTRUCTION_RESERVE_MIN,
            int(context_size * _INSTRUCTION_RESERVE_FRAC),
        )
        available = (
            context_size
            - response_reserve
            - stable_reserve
            - instruction_reserve
        )
        upper = min(
            _SOURCE_BUDGET_MAX,
            max(
                _SOURCE_BUDGET_MIN,
                int(context_size * _SOURCE_BUDGET_FRAC),
            ),
        )
        self.source_budget = max(
            _SOURCE_BUDGET_MIN,
            min(available, upper),
        )
        self.target_tokens, self.target_chars = _compute_chunk_targets(
            self.source_budget,
            context_size,
        )
        print(
            f"[config] probed context={context_size:,} → "
            f"source_budget={self.source_budget:,} "
            f"target_tokens={self.target_tokens:,} "
            f"target_chars≤{self.target_chars:,}"
        )

    def compute_max_tokens(self, base_tokens: int = 16384) -> int:
        env_override = os.environ.get("LLM_MAX_TOKENS")
        if env_override:
            return int(env_override)
        context_size = self.context_size or 0
        if context_size >= 500_000:
            return min(base_tokens * 2, 32768)
        if context_size >= 250_000:
            return base_tokens
        if context_size >= 120_000:
            return max(base_tokens // 2, 8192)
        model = self.llm_model.lower()
        if any(
            pattern in model
            for pattern in (
                "512k",
                "1m",
                "deepseek-v4",
                "deepseek-chat",
            )
        ):
            return min(base_tokens * 2, 32768)
        if "256k" in model or "200k" in model:
            return base_tokens
        if "128k" in model or "100k" in model:
            return max(base_tokens // 2, 8192)
        return base_tokens


__all__ = [
    "Config",
    "_CONTEXT_SIZE_DEFAULT",
    "_compute_chunk_targets",
    "load_caption_provider",
    "load_provider_config",
]
