#!/usr/bin/env python3
"""Atomic queue scanner and runner used by the legacy shell entry points."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from _exit_codes import ERROR, OK, USAGE
from _paths import detect_runtime_dir
from _queue_store import (
    load_queue as _load_queue,
    queue_lock as _queue_lock,
    save_queue as _save_queue,
)


SCRIPT_DIR = Path(__file__).resolve().parent
INGEST_SCRIPT = SCRIPT_DIR / "ingest.py"
IGNORED_RAW_NAMES = {
    "ingest-cache.json",
    "ingest-queue.json",
    "ingest-lock",
}


def _project_paths() -> tuple[Path, Path, Path, Path]:
    wiki_root = Path(
        os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd())
    ).expanduser().resolve()
    raw_root = wiki_root / "raw"
    runtime_dir = detect_runtime_dir(wiki_root)
    return (
        wiki_root,
        raw_root,
        runtime_dir,
        runtime_dir / "ingest-queue.json",
    )


def _validate_project(
    wiki_root: Path,
    raw_root: Path,
    *,
    require_wiki: bool,
) -> None:
    if not raw_root.is_dir():
        raise RuntimeError(f"raw/ does not exist under {wiki_root}")
    if require_wiki and not (wiki_root / "wiki").is_dir():
        raise RuntimeError(
            f"wiki/ does not exist under {wiki_root} — run setup first "
            "(see references/initial-setup.md)"
        )
    if not INGEST_SCRIPT.is_file():
        raise RuntimeError(f"ingest.py not found at {INGEST_SCRIPT}")


def _load_cache_entries(runtime_dir: Path) -> dict:
    cache_path = runtime_dir / "ingest-cache.json"
    if not cache_path.exists():
        return {}
    try:
        value = json.loads(cache_path.read_text(encoding="utf-8"))
        entries = value.get("entries", {})
        return entries if isinstance(entries, dict) else {}
    except Exception as exc:
        print(
            f"⚠️  [monitor] {cache_path} unreadable "
            f"({type(exc).__name__}: {exc}); treating all raw files as changed",
            file=sys.stderr,
        )
        return {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_raw(
    raw_root: Path,
    runtime_dir: Path,
) -> list[dict]:
    """Return raw files whose content hash is not confirmed by the cache."""
    cache = _load_cache_entries(runtime_dir)
    changed: list[dict] = []
    now_ms = int(time.time() * 1000)
    for path in sorted(raw_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(raw_root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if path.name in IGNORED_RAW_NAMES:
            continue
        try:
            source_hash = _sha256(path)
            size = path.stat().st_size
        except OSError as exc:
            print(
                f"ERROR: failed to inspect {path}: {exc}",
                file=sys.stderr,
            )
            continue
        rel_text = relative.as_posix()
        cached = cache.get(rel_text, {})
        if isinstance(cached, dict) and cached.get("hash") == source_hash:
            continue
        changed.append(
            {
                "rel": rel_text,
                "hash": source_hash,
                "size": size,
                "addedAt": now_ms,
            }
        )
    return changed


def _new_entry(item: dict, *, priority: bool = False) -> dict:
    relative = str(item["rel"])
    safe_id = relative.replace("/", "-").replace(" ", "_")[:32]
    prefix = "ingest-priority" if priority else "ingest"
    return {
        "id": f"{prefix}-{item['addedAt']}-{safe_id}",
        "sourcePath": f"raw/{relative}",
        "status": "pending",
        "addedAt": item["addedAt"],
        "hash": item.get("hash"),
        "size": item.get("size"),
        "retryCount": 0,
        "error": None,
        **({"priority": True} if priority else {}),
    }


def merge_changed(
    queue: list[dict],
    changed: list[dict],
) -> tuple[list[dict], int, int]:
    """Merge changed files, reviving completed entries when content changed."""
    by_path = {
        entry.get("sourcePath"): index
        for index, entry in enumerate(queue)
        if entry.get("sourcePath")
    }
    added = 0
    refreshed = 0
    for item in changed:
        source_path = f"raw/{item['rel']}"
        index = by_path.get(source_path)
        if index is None:
            queue.append(_new_entry(item))
            by_path[source_path] = len(queue) - 1
            added += 1
            continue
        existing = queue[index]
        if (
            existing.get("hash") == item["hash"]
            and existing.get("status") in {"pending", "processing", "failed"}
        ):
            continue
        priority = bool(existing.get("priority"))
        queue[index] = _new_entry(item, priority=priority)
        refreshed += 1
    return queue, added, refreshed


def _priority_item(path: Path, raw_root: Path) -> dict:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise RuntimeError(f"file not found: {resolved}")
    try:
        relative = resolved.relative_to(raw_root.resolve())
    except ValueError as exc:
        raise RuntimeError(
            f"priority file must be under {raw_root}: {resolved}"
        ) from exc
    return {
        "rel": relative.as_posix(),
        "hash": _sha256(resolved),
        "size": resolved.stat().st_size,
        "addedAt": int(time.time() * 1000),
    }


def prepend_priority(
    queue_path: Path,
    runtime_dir: Path,
    raw_root: Path,
    path: Path,
) -> int:
    item = _priority_item(path, raw_root)
    with _queue_lock(runtime_dir, wait=True) as lock:
        if lock is None:
            raise RuntimeError("could not acquire ingest-queue.lock")
        queue = _load_queue(queue_path)
        source_path = f"raw/{item['rel']}"
        queue = [
            entry
            for entry in queue
            if entry.get("sourcePath") != source_path
        ]
        queue.insert(0, _new_entry(item, priority=True))
        _save_queue(queue_path, queue)
    print(f"Prepended priority file: {source_path}. Queue size: {len(queue)}.")
    return len(queue)


def _exec_ingest(
    wiki_root: Path,
    args: list[str],
) -> None:
    environment = dict(os.environ)
    environment["IMPROVED_WIKI_ROOT"] = str(wiki_root)
    os.chdir(wiki_root)
    os.execvpe(
        sys.executable,
        [sys.executable, str(INGEST_SCRIPT), *args],
        environment,
    )


def scan_command(args: argparse.Namespace) -> int:
    wiki_root, raw_root, runtime_dir, queue_path = _project_paths()
    try:
        _validate_project(wiki_root, raw_root, require_wiki=True)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return ERROR

    with _queue_lock(runtime_dir) as lock:
        if lock is None:
            print("[monitor] Another queue scanner is running; exiting.")
            return OK
        changed = scan_raw(raw_root, runtime_dir)
        if not changed:
            if args.verbose or args.dry_run:
                print("[monitor] No new or changed files. Queue is up to date.")
            return OK
        if args.dry_run:
            print(f"DRY RUN — would merge into {queue_path}:")
            for item in changed:
                print(
                    f"  + {item['rel']}  ({item['size']:,} bytes, "
                    f"hash {item['hash'][:16]})"
                )
            return OK
        try:
            queue = _load_queue(queue_path)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return ERROR
        queue, added, refreshed = merge_changed(queue, changed)
        _save_queue(queue_path, queue)
    print(
        f"Added {added}, refreshed {refreshed}. Queue size: {len(queue)}."
    )
    if args.verbose:
        print("[monitor] Done. Run ./run-queue.sh to process the queue.")
    return OK


def run_command(args: argparse.Namespace) -> int:
    wiki_root, raw_root, runtime_dir, queue_path = _project_paths()
    try:
        _validate_project(wiki_root, raw_root, require_wiki=False)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return ERROR

    if args.file:
        file_path = Path(args.file).expanduser().resolve()
        if not file_path.is_file():
            print(f"ERROR: file not found: {file_path}", file=sys.stderr)
            return ERROR
        print(f"Processing single file: {file_path}", flush=True)
        ingest_args = [str(file_path)]
        if args.verbose:
            ingest_args.append("--verbose")
        _exec_ingest(wiki_root, ingest_args)

    if args.priority:
        try:
            prepend_priority(
                queue_path,
                runtime_dir,
                raw_root,
                Path(args.priority),
            )
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return ERROR

    common: list[str] = []
    if args.parallel is not None:
        if args.parallel < 0:
            print("ERROR: --parallel must be >= 0", file=sys.stderr)
            return USAGE
        common += ["--parallel", str(args.parallel)]
    if args.verbose:
        common.append("--verbose")

    if args.watch:
        print("Starting continuous queue watcher...", flush=True)
        _exec_ingest(
            wiki_root,
            ["--watch", "--poll-interval", "30", *common],
        )

    if not queue_path.exists():
        print(f"Queue not found: {queue_path}")
        print(
            "Run wiki-monitor.sh first to populate it, or use "
            "--file <path> for one-off."
        )
        return OK
    try:
        queue = _load_queue(queue_path)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return ERROR
    active = [
        entry
        for entry in queue
        if entry.get("status", "pending") in {
            "pending",
            "processing",
            "failed",
        }
        and int(entry.get("retryCount", 0) or 0) < 3
    ]
    if not active:
        print("Queue is empty — nothing to do.")
        return OK
    print(f"Draining queue: {len(active)} runnable entries", flush=True)
    _exec_ingest(
        wiki_root,
        ["--watch", "--drain", "--poll-interval", "10", *common],
    )
    return OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser(
        "scan",
        help="Scan raw/ and atomically merge new or changed files.",
    )
    scan.add_argument("--verbose", "-v", action="store_true")
    scan.add_argument("--dry-run", "-n", action="store_true")
    scan.set_defaults(handler=scan_command)

    run = subparsers.add_parser(
        "run",
        help="Run one file or consume the queue.",
    )
    run.add_argument("--file")
    run.add_argument("--priority")
    run.add_argument("--parallel", type=int)
    run.add_argument("--watch", action="store_true")
    run.add_argument("--verbose", "-v", action="store_true")
    run.set_defaults(handler=run_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
