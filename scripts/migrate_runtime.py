#!/usr/bin/env python3
"""Explicit legacy runtime migration. Preview by default; conflicts never overwrite."""
from __future__ import annotations

import argparse
import fcntl
import os
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

from _batch_coordination import batch_coordinator_slot, load_spine_reservation
from _paths import LEGACY_STATE_ALIASES, RUNTIME_STATE_NAMES
from _progress import ProjectLock, file_sha256


def migration_plan(project: Path, old_name: str) -> list[tuple[Path, Path, str]]:
    old, new = project / old_name, project / '.llm-wiki'
    if old_name not in {'.iwiki-runtime', 'wiki'}:
        raise ValueError('Choose .iwiki-runtime or wiki')
    if old.is_symlink() or new.is_symlink():
        raise ValueError('Runtime directories must not be symlinks')
    entries = sorted(old.iterdir()) if old.exists() else []
    plan: list[tuple[Path, Path, str]] = []
    for entry in entries:
        if old_name == 'wiki' and entry.name not in {*RUNTIME_STATE_NAMES, *LEGACY_STATE_ALIASES}:
            continue
        target = new / LEGACY_STATE_ALIASES.get(entry.name, entry.name)
        for src in ([entry] + sorted(entry.rglob('*')) if entry.is_dir() else [entry]):
            if src.is_symlink():
                raise ValueError(f'Symlink in runtime: {src}')
            if not src.is_file() or src.name.endswith(('.lock', '.lease')):
                continue  # Lock inodes stay in place, including after migration.
            dst = target / src.relative_to(entry) if entry.is_dir() else target
            if any(p.is_symlink() for p in [dst, *dst.parents]):
                raise ValueError(f'Symlink destination: {dst}')
            for parent in dst.parents:
                if parent == project:
                    break
                if parent.exists() and not parent.is_dir():
                    raise ValueError(f'Destination parent is not a directory: {parent}')
            digest = file_sha256(src)
            if dst.exists() and (not dst.is_file() or file_sha256(dst) != digest):
                raise ValueError(f'Conflicting runtime state: {src} -> {dst}; reconcile before migration')
            plan.append((src, dst, digest))
    # Two legacy aliases can nominate the same destination.
    digests: dict[Path, str] = {}
    for _, dst, digest in plan:
        if dst in digests and digests[dst] != digest:
            raise ValueError(f'Conflicting legacy aliases for {dst}')
        digests[dst] = digest
    return plan


def migrate(project: Path, old_name: str, *, apply: bool = False) -> list:
    project = project.expanduser().resolve()
    preview = migration_plan(project, old_name)
    if not apply or not preview:
        return preview
    with ExitStack() as stack:
        for runtime in (project / old_name, project / '.llm-wiki'):
            config = SimpleNamespace(runtime_dir=runtime)
            stack.enter_context(batch_coordinator_slot(config))
            stack.enter_context(ProjectLock(config, owner_id='runtime-migration'))
            if load_spine_reservation(config):
                raise RuntimeError(f'Reserved write spine in {runtime}; resume it first')
            # Existing watchers/queue writers and Phase-1 workers must be idle.
            for path in sorted(set(runtime.rglob('*.lease')) | {
                    runtime / 'watch.lock', runtime / 'ingest-queue.lock'}):
                handle = stack.enter_context(path.open('a+'))
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = migration_plan(project, old_name)
        if plan != preview:
            raise RuntimeError('Runtime changed since preview; retry')
        for src, dst, digest in plan:
            if file_sha256(src) != digest:
                raise RuntimeError(f'Source changed during migration: {src}')
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(src, dst)  # Atomic create, never replaces an existing file.
            except FileExistsError:
                if not dst.is_file() or file_sha256(dst) != digest:
                    raise RuntimeError(f'Destination changed during migration: {dst}')
            src.unlink()
        old = project / old_name
        # Empty data directories may be removed; old lock inodes are retained.
        folders = {parent for src, _, _ in plan for parent in src.parents
                   if parent != old and old in parent.parents}
        for folder in sorted(folders, key=lambda p: len(p.parts), reverse=True):
            try:
                folder.rmdir()
            except OSError:
                pass
        return plan


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path.cwd())
    parser.add_argument('--from', dest='old_name', choices=['.iwiki-runtime', 'wiki'], required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        plan = migrate(args.project, args.old_name, apply=args.apply)
        for src, dst, _ in plan:
            print(f'{src} -> {dst}')
        print(f'{len(plan)} file(s), {"migrated" if args.apply else "preview only"}')
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(f'ERROR: {exc}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
