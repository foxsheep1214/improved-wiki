"""Explicit context budget for conversation-mode workers; no model self-report.

Precedence: command line > IMPROVED_WIKI_CONTEXT_TOKENS > the project's saved
setting (``<runtime>/context-budget.json``) > the conservative default.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from _config import _CONTEXT_SIZE_DEFAULT
from _paths import atomic_write

CONTEXT_ENV = "IMPROVED_WIKI_CONTEXT_TOKENS"
PROJECT_BUDGET_FILE = "context-budget.json"


def _validated(raw, label: str) -> int:
    if not isinstance(raw, (str, int)) or isinstance(raw, bool):
        raise ValueError(f"{label} must be an integer")
    try:
        tokens = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if not 64_000 <= tokens <= 10_000_000:
        raise ValueError("context tokens must be between 64000 and 10000000; "
                         "the prompt reserves require at least 64000")
    return tokens


def project_budget_path(runtime_dir) -> Path:
    return Path(runtime_dir) / PROJECT_BUDGET_FILE


def project_context_tokens(runtime_dir) -> int | None:
    """The project's saved verified capacity, or None when unset."""
    path = project_budget_path(runtime_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unreadable project context budget {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"project context budget {path} must be a JSON object")
    return _validated(data.get("context_tokens"), str(path))


def save_project_context_tokens(runtime_dir, value) -> Path:
    tokens = _validated(value, "--set-context-tokens")
    path = project_budget_path(runtime_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps({"context_tokens": tokens}, indent=2) + "\n")
    return path


def resolve_context_tokens(value=None, runtime_dir=None) -> tuple[int, str]:
    """Return ``(tokens, source)`` using the documented precedence."""
    if value is not None:
        return _validated(value, "--context-tokens"), "command line"
    raw = os.environ.get(CONTEXT_ENV, "")
    if raw != "":
        return _validated(raw, f"{CONTEXT_ENV}/--context-tokens"), "explicit runtime setting"
    if runtime_dir is not None:
        saved = project_context_tokens(runtime_dir)
        if saved is not None:
            return saved, f"project setting {project_budget_path(runtime_dir)}"
    return _CONTEXT_SIZE_DEFAULT, "conservative default"


def context_tokens(value=None, runtime_dir=None) -> int:
    return resolve_context_tokens(value, runtime_dir)[0]


def apply_context_budget(config) -> None:
    tokens, source = resolve_context_tokens(runtime_dir=config.runtime_dir)
    print(f"[context] {tokens:,} tokens ({source}); no model self-report or cached probe")
    if source == "conservative default":
        print("[context] no verified capacity configured; set it once per project with "
              "`ingest.py --set-context-tokens <worker window>`")
    config.apply_context(tokens)
