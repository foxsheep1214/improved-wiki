#!/usr/bin/env python3
"""Query or one-time migrate improved-wiki ingest completion history.

Examples:
    python3 ingest_history.py --project /path/to/wiki list --sort last --limit 10
    python3 ingest_history.py --project /path/to/wiki migrate
    python3 ingest_history.py --project /path/to/wiki migrate --apply
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from _ingest_events import (
    EVENT_SCHEMA_VERSION,
    INGEST_COMPLETED,
    append_ingest_event,
    load_ingest_events,
    project_source_ingest_times,
    rfc3339_from_ms,
    summarize_ingest_history,
)
from _paths import atomic_write, detect_runtime_dir


# Before run IDs existed, Stage 3.3 wrote an INGEST block before the pipeline
# had actually completed. A quick retry therefore produced another block for
# the same source/hash. Keep distant evidence as separate historical runs, but
# coalesce adjacent records inside this deliberately narrow inference window.
LEGACY_RETRY_WINDOW_MS = 60 * 60 * 1000


@dataclass
class HistoryConfig:
    wiki_root: Path
    raw_root: Path
    wiki_dir: Path
    runtime_dir: Path
    progress_dir: Path
    cache_path: Path


def _config(project: Path) -> HistoryConfig:
    root = project.expanduser().resolve()
    runtime = detect_runtime_dir(root)
    return HistoryConfig(
        wiki_root=root,
        raw_root=root / "raw",
        wiki_dir=root / "wiki",
        runtime_dir=runtime,
        progress_dir=runtime / "ingest-progress",
        cache_path=runtime / "ingest-cache.json",
    )


def _stable_migration_run_id(kind: str, *parts: object) -> str:
    raw = "\0".join(str(part) for part in parts).encode("utf-8")
    return f"migration-{kind}-{hashlib.sha256(raw).hexdigest()[:24]}"


def _source_page_for_identity(source: str) -> str:
    if not source.startswith("raw/"):
        return ""
    return str(
        Path("wiki/sources") / Path(source).relative_to("raw").with_suffix(".md")
    ).replace("\\", "/")


def _local_ms(value: str) -> tuple[int, str]:
    fmt = "%Y-%m-%d %H:%M:%S" if " " in value else "%Y-%m-%d"
    parsed = datetime.strptime(value, fmt)
    local_tz = datetime.now().astimezone().tzinfo
    aware = parsed.replace(tzinfo=local_tz)
    return int(aware.timestamp() * 1000), (
        "second" if " " in value else "date"
    )


def _legacy_log_events(config: HistoryConfig) -> list[dict]:
    """Recover evidence from pre-ledger ``— INGEST`` blocks."""
    log = config.wiki_dir / "log.md"
    if not log.is_file():
        return []
    text = log.read_text(encoding="utf-8", errors="strict")
    headings = list(re.finditer(
        r"(?m)^## (\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}:\d{2})?) — INGEST\s*$",
        text,
    ))
    events: list[dict] = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        block = text[heading.end():end]
        source_match = re.search(r"(?m)^- Source: `([^`]+)`\s*$", block)
        hash_match = re.search(r"(?m)^- Hash: ([0-9a-fA-F]{16,64})\s*$", block)
        if not source_match or not hash_match:
            continue
        completed_at_ms, precision = _local_ms(heading.group(1))
        source = source_match.group(1)
        source_hash = hash_match.group(1).lower()
        events.append({
            "schema_version": EVENT_SCHEMA_VERSION,
            "event": INGEST_COMPLETED,
            "run_id": _stable_migration_run_id(
                "log", source, source_hash, completed_at_ms
            ),
            "source": source,
            "source_hash": source_hash,
            "source_page": _source_page_for_identity(source),
            "completed_at": rfc3339_from_ms(completed_at_ms),
            "completed_at_ms": completed_at_ms,
            "mode": "migration",
            "origin": "legacy_log",
            "time_precision": precision,
        })
    return _collapse_legacy_log_retries(events)


def _collapse_legacy_log_retries(events: list[dict]) -> list[dict]:
    """Coalesce nearby pre-ledger records that most plausibly represent retries.

    Old Stage 3.3 records have neither a run ID nor a completion commit. For
    one source/hash, adjacent records no more than one hour apart are treated
    as retry artifacts. The latest timestamp is retained because it is the
    closest available evidence to eventual completion; audit fields preserve
    how many records were represented and when the cluster began.
    """
    grouped: dict[tuple[str, str], list[dict]] = {}
    for event in events:
        key = (event["source"], event["source_hash"])
        grouped.setdefault(key, []).append(event)

    collapsed: list[dict] = []

    def _append_cluster(cluster: list[dict]) -> None:
        representative = dict(cluster[-1])
        if len(cluster) > 1:
            representative["legacy_log_record_count"] = len(cluster)
            representative["legacy_log_first_at"] = cluster[0]["completed_at"]
        collapsed.append(representative)

    for identity_events in grouped.values():
        ordered = sorted(
            identity_events,
            key=lambda event: (event["completed_at_ms"], event["run_id"]),
        )
        cluster: list[dict] = []
        for event in ordered:
            if (
                cluster
                and event["completed_at_ms"] - cluster[-1]["completed_at_ms"]
                > LEGACY_RETRY_WINDOW_MS
            ):
                _append_cluster(cluster)
                cluster = []
            cluster.append(event)
        if cluster:
            _append_cluster(cluster)

    return sorted(
        collapsed,
        key=lambda event: (
            event["completed_at_ms"], event["source"], event["run_id"]
        ),
    )


def _task_marker_events(config: HistoryConfig) -> list[dict]:
    """Recover precise latest completion evidence from current task markers."""
    if not config.progress_dir.is_dir():
        return []
    events: list[dict] = []
    for task_path in sorted(config.progress_dir.glob("*.task.json")):
        try:
            task = json.loads(task_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"unreadable task manifest {task_path}: {exc}") from exc
        source_info = task.get("source")
        if not isinstance(source_info, dict):
            continue
        source = str(source_info.get("identity") or "")
        source_hash = str(source_info.get("sha256") or "")
        if not source or not source_hash:
            continue
        stage_path = config.progress_dir / f"{source_hash[:16]}.stages.json"
        if not stage_path.is_file():
            continue
        try:
            stages = json.loads(stage_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"unreadable stage marker {stage_path}: {exc}") from exc
        completed_at_ms = stages.get("ingested")
        if not isinstance(completed_at_ms, int) or completed_at_ms <= 0:
            continue
        payload = stages.get("ingested__payload")
        payload_run_id = (
            str(payload.get("run_id") or "")
            if isinstance(payload, dict)
            else ""
        )
        events.append({
            "schema_version": EVENT_SCHEMA_VERSION,
            "event": INGEST_COMPLETED,
            "run_id": payload_run_id or _stable_migration_run_id(
                "marker", source, source_hash, completed_at_ms
            ),
            "source": source,
            "source_hash": source_hash,
            "source_page": _source_page_for_identity(source),
            "completed_at": rfc3339_from_ms(completed_at_ms),
            "completed_at_ms": completed_at_ms,
            "mode": "migration",
            "origin": "stages_marker",
            "time_precision": "millisecond",
        })
    return events


def migration_candidates(config: HistoryConfig) -> list[dict]:
    """Merge legacy evidence without double-counting inferred retries."""
    marker_events = _task_marker_events(config)
    legacy_events = []
    for legacy in _legacy_log_events(config):
        same_completion = any(
            marker["source"] == legacy["source"]
            and marker["source_hash"].startswith(legacy["source_hash"])
            and marker["completed_at"][:10] == legacy["completed_at"][:10]
            for marker in marker_events
        )
        if not same_completion:
            legacy_events.append(legacy)
    return sorted(
        legacy_events + marker_events,
        key=lambda event: (
            event["completed_at_ms"], event["source"], event["run_id"]
        ),
    )


def _project_current_marker_payload(
    config: HistoryConfig, event: dict
) -> bool:
    """Bind a migrated precise marker to the same run_id/timestamp."""
    if event.get("origin") != "stages_marker":
        return False
    stage_path = (
        config.progress_dir / f"{str(event['source_hash'])[:16]}.stages.json"
    )
    if not stage_path.is_file():
        return False
    stages = json.loads(stage_path.read_text(encoding="utf-8"))
    if stages.get("ingested") != event["completed_at_ms"]:
        raise RuntimeError(
            f"current marker changed during migration: {stage_path}"
        )
    payload = stages.get("ingested__payload")
    payload = dict(payload) if isinstance(payload, dict) else {}
    payload.update({
        "run_id": event["run_id"],
        "completed_at": event["completed_at"],
        "event": event["event"],
        "event_ledger": str(
            (config.runtime_dir / "ingest-events.jsonl").relative_to(
                config.wiki_root
            )
        ),
    })
    stages["ingested__payload"] = payload
    atomic_write(stage_path, json.dumps(stages, ensure_ascii=False, indent=2))
    return True


def _print_rows(rows: list[dict], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("No completed ingest events found.")
        return
    print("last_ingested_at\tfirst_ingested_at\tcount\tsource")
    for row in rows:
        print(
            f"{row['last_ingested_at']}\t{row['first_ingested_at']}\t"
            f"{row['ingest_count']}\t{row['source']}"
        )


def cmd_list(config: HistoryConfig, args) -> int:
    rows = summarize_ingest_history(load_ingest_events(config))
    key = f"{args.sort}_ingested_at_ms"
    rows.sort(key=lambda row: (row[key], row["source"]), reverse=args.order == "desc")
    if args.source:
        needle = args.source.casefold()
        rows = [row for row in rows if needle in row["source"].casefold()]
    if args.limit:
        rows = rows[:args.limit]
    _print_rows(rows, as_json=args.json)
    return 0


def cmd_migrate(config: HistoryConfig, args) -> int:
    existing = load_ingest_events(config)
    existing_ids = {(event["event"], event["run_id"]) for event in existing}
    evidence = migration_candidates(config)
    candidates = [
        event for event in evidence
        if (event["event"], event["run_id"]) not in existing_ids
    ]
    by_origin: dict[str, int] = {}
    for event in candidates:
        by_origin[event["origin"]] = by_origin.get(event["origin"], 0) + 1
    mode = "APPLY" if args.apply else "PREVIEW"
    print(
        f"[{mode}] {len(candidates)} new event(s): "
        + ", ".join(f"{key}={value}" for key, value in sorted(by_origin.items()))
    )
    collapsed_records = sum(
        max(0, int(event.get("legacy_log_record_count", 1)) - 1)
        for event in evidence
    )
    if collapsed_records:
        print(
            f"  Legacy retry inference: collapsed {collapsed_records} nearby "
            "pre-ledger log record(s) (same source/hash, <= 1 hour)."
        )
    for event in candidates[:20]:
        print(
            f"  {event['completed_at']}  {event['origin']:<14}  "
            f"{event['source']}"
        )
    if len(candidates) > 20:
        print(f"  ... +{len(candidates) - 20} more")
    if not args.apply:
        print("Preview only. Re-run with --apply to write the ledger/projections.")
        return 0

    for event in candidates:
        append_ingest_event(config, event)
    for event in evidence:
        _project_current_marker_payload(config, event)
    all_events = load_ingest_events(config)
    projected = 0
    sources = {
        (event["source"], str(event.get("source_page") or ""))
        for event in all_events
        if event["event"] == INGEST_COMPLETED and event.get("source_page")
    }
    for source, source_page in sorted(sources):
        page_path = config.wiki_root / source_page
        if not page_path.is_file():
            continue
        _page, changed = project_source_ingest_times(
            config, source, source_page, events=all_events
        )
        projected += int(changed)
    print(
        f"Applied {len(candidates)} event(s); updated {projected} source page(s)."
    )
    print(
        "Because operational time fields are excluded from new embeddings, run "
        "one explicit full embedding rebuild after this one-time migration to "
        "upgrade pre-existing vector rows to the same normalization contract."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Query or migrate improved-wiki ingest completion history"
    )
    parser.add_argument("--project", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="List first/last completed ingests")
    list_parser.add_argument("--sort", choices=("first", "last"), default="last")
    list_parser.add_argument("--order", choices=("asc", "desc"), default="desc")
    list_parser.add_argument("--limit", type=int, default=0)
    list_parser.add_argument("--source", default="")
    list_parser.add_argument("--json", action="store_true")

    migrate_parser = sub.add_parser(
        "migrate", help="Recover legacy log/stage evidence into the ledger"
    )
    migrate_parser.add_argument(
        "--apply",
        action="store_true",
        help="Write events and source-page projections (default is preview)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "limit", 0) < 0:
        parser.error("--limit must be >= 0")
    config = _config(args.project)
    try:
        if args.command == "list":
            return cmd_list(config, args)
        return cmd_migrate(config, args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
