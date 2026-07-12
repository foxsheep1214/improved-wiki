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

import fcntl
import json
import os
import sys
from pathlib import Path

from _paths import atomic_write
from typing import List

WHITELIST_FILE = "dedup-whitelist.json"


def whitelist_path(runtime_dir: Path) -> Path:
    return runtime_dir / WHITELIST_FILE


def _warn_corrupt(path: Path, why: str) -> None:
    print(f"[dedup] WARNING: not-duplicates whitelist unreadable "
          f"({path}: {why}) — proceeding with an EMPTY whitelist. "
          f"Every previously-marked not-duplicate pair is unprotected: the "
          f"detector may re-suggest (and auto-apply may re-merge) pairs a "
          f"human already rejected. Fix or delete the file.", file=sys.stderr)


def load_not_duplicates(runtime_dir: Path) -> List[List[str]]:
    """Read whitelisted not-duplicate pairs. Mirrors NashSU loadNotDuplicates:
    best-effort — returns [] on any read/parse error, but LOUDLY (2026-07-12):
    a raise here would block the detector read path, so we warn instead, and
    the warning spells out the consequence (whitelist protection is off).
    Accepts both the ``{"not_duplicates": [...]}`` envelope and a bare array."""
    path = whitelist_path(runtime_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        _warn_corrupt(path, str(ex))
        return []
    raw = data.get("not_duplicates", []) if isinstance(data, dict) else data
    if not isinstance(raw, list):
        _warn_corrupt(path, f"top level must be a list, got {type(raw).__name__}")
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
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))


def canonical_key(slugs: List[str]) -> str:
    """Order-independent, case-insensitive group key. Mirrors NashSU
    canonicalKey / normalizeSlugGroupKey / normalizeGroupKey — one shared
    implementation (the three per-module copies had drifted on separator)."""
    return ",".join(sorted(s.lower() for s in slugs))


_canonical_key = canonical_key  # back-compat alias


def add_not_duplicate(runtime_dir: Path, slugs: List[str]) -> bool:
    """Add a group to the whitelist. Idempotent — if the same group (any order,
    any casing) is already present, this is a no-op. Mirrors NashSU
    addNotDuplicate. Returns True if a new entry was written, False if it was
    already present (or fewer than 2 slugs).

    The load→append→save sequence runs under an exclusive flock (2026-07-12)
    so two concurrent writers can't interleave and drop each other's entry
    (the atomic_write in save only protects against torn writes, not
    lost-update races)."""
    if len(slugs) < 2:
        return False
    runtime_dir.mkdir(parents=True, exist_ok=True)
    lock_path = whitelist_path(runtime_dir).with_name(WHITELIST_FILE + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        groups = load_not_duplicates(runtime_dir)
        new_key = _canonical_key(slugs)
        for existing in groups:
            if _canonical_key(existing) == new_key:
                return False  # already there
        groups.append(sorted(slugs))
        save_not_duplicates(runtime_dir, groups)
        return True
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
