"""_source_filter.py — Raw source safety filter (NashSU source-filter.ts).

Prevents agent/tool config files (API keys, MCP/CLI settings) from entering
the ingest pipeline. A file is "sensitive" when it BOTH lives under a known
agent/tool config directory AND has a config-file extension. The two-condition
gate is narrow on purpose: ``notes.json`` sitting next to a book is fine;
``.claude/settings.json`` is not.

NashSU applies this when listing ``raw/sources`` for image resolution and when
enqueuing folder imports. The skill ingests explicit paths, so the gate lives
in the ingest.py validation loop as defense-in-depth against an agent
accidentally passing a config file.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote  # noqa: F401  (reserved for future file:// paths)

HIDDEN_SOURCE_ENTRY_NAMES = frozenset({".cache", ".DS_Store"})
SENSITIVE_CONFIG_EXTENSIONS = frozenset({"env", "json", "toml", "yaml", "yml", "xml"})
SENSITIVE_CONFIG_DIR_NAMES = frozenset({".claude", ".codex", ".cursor", ".gemini", ".mcp"})


def is_hidden_source_entry(name: str) -> bool:
    """True for noise entries that should never be ingested (``.DS_Store``, ``.cache``)."""
    return name in HIDDEN_SOURCE_ENTRY_NAMES


def is_sensitive_config_source_file(path) -> bool:
    """True iff ``path`` is a config-file extension under a known agent/tool config dir.

    Mirrors NashSU ``isSensitiveConfigSourceFile``: the extension must be a
    config type AND some path component must be a config dir name
    (case-insensitive). Accepts ``str`` or ``Path``.
    """
    parts = [p for p in str(path).replace("\\", "/").split("/") if p]
    if not parts:
        return False
    name = parts[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in SENSITIVE_CONFIG_EXTENSIONS:
        return False
    return any(p.lower() in SENSITIVE_CONFIG_DIR_NAMES for p in parts)
