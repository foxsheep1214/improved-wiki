#!/usr/bin/env python3
"""Durable stage checkpoints for one logical ``wiki-lint.sh`` run.

A conversation-mode lint can yield with exit 101 several times.  Those
process re-entries are continuations of one user-triggered lint run, not new
lint runs.  This helper records completed stages so a continuation does not
repeat a full semantic scan or already-completed mutation stages.

The state file is intentionally small and exists only while a logical run is
active.  ``finish`` removes it; ``--reset-lint-run`` in the shell wrapper is
the explicit escape hatch for an abandoned run.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _paths import atomic_write


STATE_VERSION = 1
_STAGE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_state() -> dict[str, Any]:
    now = _now()
    return {
        "version": STATE_VERSION,
        "run_id": uuid.uuid4().hex,
        "started_at": now,
        "updated_at": now,
        "completed_stages": [],
    }


def _load(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        return None
    run_id = raw.get("run_id")
    stages = raw.get("completed_stages")
    if not isinstance(run_id, str) or not run_id:
        return None
    if not isinstance(stages, list) or not all(isinstance(s, str) for s in stages):
        return None
    return raw


def begin(path: Path, *, reset: bool = False) -> dict[str, Any]:
    """Start or resume a logical lint run and return its state."""
    if reset:
        path.unlink(missing_ok=True)
    state = _load(path)
    if state is None:
        state = _new_state()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    return state


def is_done(path: Path, stage: str) -> bool:
    state = _load(path)
    return state is not None and stage in state["completed_stages"]


def mark_done(path: Path, stage: str) -> None:
    if not _STAGE_RE.fullmatch(stage):
        raise ValueError(f"invalid lint stage: {stage!r}")
    state = _load(path)
    if state is None:
        raise RuntimeError(f"no active lint run state: {path}")
    if stage not in state["completed_stages"]:
        state["completed_stages"].append(stage)
    state["updated_at"] = _now()
    atomic_write(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def finish(path: Path) -> None:
    path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    begin_p = sub.add_parser("begin", help="start or resume a logical lint run")
    begin_p.add_argument("state_file", type=Path)
    begin_p.add_argument("--reset", action="store_true")

    done_p = sub.add_parser("is-done", help="exit 0 when a stage is complete")
    done_p.add_argument("state_file", type=Path)
    done_p.add_argument("stage")

    mark_p = sub.add_parser("mark-done", help="checkpoint a completed stage")
    mark_p.add_argument("state_file", type=Path)
    mark_p.add_argument("stage")

    finish_p = sub.add_parser("finish", help="finish and remove active state")
    finish_p.add_argument("state_file", type=Path)

    reset_p = sub.add_parser("reset", help="discard an abandoned run")
    reset_p.add_argument("state_file", type=Path)
    reset_p.add_argument(
        "--related-state",
        action="append",
        default=[],
        type=Path,
        help="also remove a related per-run checkpoint",
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "begin":
            state = begin(args.state_file, reset=args.reset)
            print(state["run_id"])
        elif args.command == "is-done":
            return 0 if is_done(args.state_file, args.stage) else 1
        elif args.command == "mark-done":
            mark_done(args.state_file, args.stage)
        elif args.command == "finish":
            finish(args.state_file)
        elif args.command == "reset":
            finish(args.state_file)
            for related in args.related_state:
                finish(related)
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[lint-state] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
