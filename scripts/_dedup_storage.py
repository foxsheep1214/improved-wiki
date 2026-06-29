#!/usr/bin/env python3
"""_dedup_storage.py — read/write the dedup "not duplicates" whitelist.

Faithful port of NashSU ``src/lib/dedup-storage.ts`` (loadNotDuplicates /
saveNotDuplicates / addNotDuplicate). When the user reviews a candidate group
and decides "these are NOT the same thing", the pair is recorded so the next
detector run doesn't re-suggest it.

Divergence from NashSU storage filename: the desktop app stores at
``.llm-wiki/dedup-not-duplicates.json``; the port keeps the pre-existing
``dedup-whitelist.json`` filename that ``cross_source_dedup.load_whitelist``
already reads, so read and write stay on the same file. The on-disk shape is
the project's existing ``{"not_duplicates": [[slug, slug], ...]}`` envelope
(also accepts a bare array, like load_whitelist).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List

WHITELIST_FILE = "dedup-whitelist.json"


def whitelist_path(runtime_dir: Path) -> Path:
    return runtime_dir / WHITELIST_FILE


def load_not_duplicates(runtime_dir: Path) -> List[List[str]]:
    """Read whitelisted not-duplicate pairs. Mirrors NashSU loadNotDuplicates:
    best-effort — returns [] on any read/parse error. Accepts both the
    ``{"not_duplicates": [...]}`` envelope and a bare array."""
    path = whitelist_path(runtime_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = data.get("not_duplicates", []) if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    out: List[List[str]] = []
    for group in raw:
        if isinstance(group, list) and all(isinstance(s, str) for s in group):
            out.append([str(s) for s in group])
    return out


def save_not_duplicates(runtime_dir: Path, groups: List[List[str]]) -> None:
    """Persist the whitelist. Atomic write (tmp + rename) so a crash mid-write
    can't corrupt the file — matches cross_source_dedup._write_report."""
    path = whitelist_path(runtime_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"not_duplicates": groups}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _canonical_key(slugs: List[str]) -> str:
    """Order-independent, case-insensitive key. Mirrors NashSU canonicalKey."""
    return ",".join(sorted(s.lower() for s in slugs))


def add_not_duplicate(runtime_dir: Path, slugs: List[str]) -> bool:
    """Add a group to the whitelist. Idempotent — if the same group (any order,
    any casing) is already present, this is a no-op. Mirrors NashSU
    addNotDuplicate. Returns True if a new entry was written, False if it was
    already present (or fewer than 2 slugs)."""
    if len(slugs) < 2:
        return False
    groups = load_not_duplicates(runtime_dir)
    new_key = _canonical_key(slugs)
    for existing in groups:
        if _canonical_key(existing) == new_key:
            return False  # already there
    groups.append(sorted(slugs))
    save_not_duplicates(runtime_dir, groups)
    return True
