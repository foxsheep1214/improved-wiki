"""Authoritative, append-only ingest and repair history.

``*.stages.json`` answers whether the *current* source artifact is complete.
This module answers a different question: when did each completed run happen?
The JSONL ledger is authoritative for history; source-page frontmatter and
``wiki/log.md`` are rebuildable human-facing projections.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from _core import (
    canonical_source_path,
    is_query_bridge_source,
    source_cache_key,
)
from _frontmatter import (
    parse_frontmatter,
    strip_operational_time_fields,
    write_frontmatter,
)
from _page_ref import PageRef
from _paths import atomic_write
from _progress import load_cache
from _stage_3_write import _stage_3_2_wiki_path_for_source


EVENT_SCHEMA_VERSION = 1
INGEST_COMPLETED = "ingest_completed"
REPAIR_COMPLETED = "repair_completed"
KNOWN_EVENTS = frozenset({INGEST_COMPLETED, REPAIR_COMPLETED})
LEDGER_NAME = "ingest-events.jsonl"
LOCK_NAME = "ingest-events.lock"
SOURCE_SNAPSHOT_DIR = "source-page-snapshots"
SOURCE_TIME_FIELDS = ("first_ingested_at", "last_ingested_at")


class IngestEventError(RuntimeError):
    """The authoritative event ledger is malformed or internally conflicting."""


_event_thread_lock = threading.Lock()


def ingest_event_path(config) -> Path:
    return config.runtime_dir / LEDGER_NAME


def _source_snapshot_path(config, source_identity: str) -> Path:
    key = hashlib.sha256(source_identity.encode("utf-8")).hexdigest()[:24]
    return config.runtime_dir / SOURCE_SNAPSHOT_DIR / f"{key}.json"


def _semantic_source_hash(content: str) -> str:
    normalized = strip_operational_time_fields(content).replace("\r\n", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def snapshot_source_page_times(
    raw_file: Path,
    config,
    source_page: Path,
    *,
    captured_at_ms: int,
) -> Path | None:
    """Save one identity-scoped pre-delete baseline for the next run."""
    if not source_page.is_file() or is_query_bridge_source(raw_file, config):
        return None
    content = source_page.read_text(encoding="utf-8")
    fm, _body = parse_frontmatter(content)
    if not fm:
        return None
    source_identity = canonical_source_path(raw_file, config)
    path = _source_snapshot_path(config, source_identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "schema_version": 1,
        "source": source_identity,
        "source_page": PageRef.parse(
            source_page, config.wiki_root, config.wiki_dir
        ).project_relative,
        "semantic_sha256": _semantic_source_hash(content),
        "created": str(fm.get("created") or ""),
        "updated": str(fm.get("updated") or ""),
        "captured_at_ms": captured_at_ms,
    }
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2))
    return path


def restore_source_page_times(
    raw_file: Path,
    config,
    run_id: str,
) -> tuple[bool, bool]:
    """Restore immutable creation and no-op update dates after ``--delete``.

    Returns ``(changed, content_identical)``. The snapshot is bound to the
    first run that consumes it so an abandoned old snapshot cannot affect a
    later unrelated run.
    """
    source_identity = canonical_source_path(raw_file, config)
    path = _source_snapshot_path(config, source_identity)
    if not path.is_file() or is_query_bridge_source(raw_file, config):
        return False, False
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IngestEventError(
            f"source-page time snapshot is unreadable: {path}: {exc}"
        ) from exc
    if (
        snapshot.get("schema_version") != 1
        or snapshot.get("source") != source_identity
        or not snapshot.get("semantic_sha256")
    ):
        raise IngestEventError(
            f"source-page time snapshot has an invalid contract: {path}"
        )
    bound_run = str(snapshot.get("consumed_by_run_id") or "")
    if bound_run and bound_run != run_id:
        return False, False
    if not bound_run:
        snapshot["consumed_by_run_id"] = run_id
        atomic_write(path, json.dumps(snapshot, ensure_ascii=False, indent=2))
    page = _stage_3_2_wiki_path_for_source(raw_file, config)
    if not page.is_file():
        raise IngestEventError(
            f"cannot restore source times onto missing page: {page}"
        )
    original = page.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(original)
    if not fm:
        raise IngestEventError(
            f"cannot restore source times onto a page without frontmatter: {page}"
        )
    content_identical = (
        _semantic_source_hash(original) == snapshot["semantic_sha256"]
    )
    prior_created = str(snapshot.get("created") or "")
    current_created = str(fm.get("created") or "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", prior_created):
        fm["created"] = (
            min(prior_created, current_created)
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", current_created)
            else prior_created
        )
    prior_updated = str(snapshot.get("updated") or "")
    if content_identical and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", prior_updated
    ):
        fm["updated"] = prior_updated
    rendered = write_frontmatter(fm, body)
    if rendered == original:
        return False, content_identical
    atomic_write(page, rendered)
    return True, content_identical


def clear_source_page_time_snapshot(
    raw_file: Path, config, run_id: str
) -> None:
    """Remove a successfully consumed pre-delete baseline."""
    path = _source_snapshot_path(
        config, canonical_source_path(raw_file, config)
    )
    if not path.is_file():
        return
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if snapshot.get("consumed_by_run_id") == run_id:
            path.unlink()
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"⚠️  [ingest-time] could not remove consumed snapshot {path}: "
            f"{type(exc).__name__}: {exc}"
        )


@contextmanager
def _event_lock(config):
    """Serialize ledger read-modify-write across threads and processes."""
    with _event_thread_lock:
        config.runtime_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(config.runtime_dir / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def rfc3339_from_ms(timestamp_ms: int) -> str:
    """Render epoch milliseconds with the machine's explicit local offset."""
    if not isinstance(timestamp_ms, int) or timestamp_ms <= 0:
        raise IngestEventError(
            f"completion timestamp must be positive epoch milliseconds, got "
            f"{timestamp_ms!r}"
        )
    return datetime.fromtimestamp(timestamp_ms / 1000).astimezone().isoformat(
        timespec="milliseconds"
    )


def _parse_rfc3339(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("completed_at must be a non-empty RFC3339 string")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.utcoffset() is None:
        raise ValueError("completed_at must include Z or an explicit UTC offset")
    return parsed


def _validate_event(event: object, *, line_number: int | None = None) -> dict:
    where = f" at line {line_number}" if line_number is not None else ""
    if not isinstance(event, dict):
        raise IngestEventError(f"ingest event{where} is not a JSON object")
    if event.get("schema_version") != EVENT_SCHEMA_VERSION:
        raise IngestEventError(
            f"ingest event{where} has unsupported schema_version "
            f"{event.get('schema_version')!r}"
        )
    if event.get("event") not in KNOWN_EVENTS:
        raise IngestEventError(
            f"ingest event{where} has unknown event {event.get('event')!r}"
        )
    for key in ("run_id", "source", "source_hash"):
        if not isinstance(event.get(key), str) or not event[key].strip():
            raise IngestEventError(
                f"ingest event{where} has no non-empty {key}"
            )
    completed_at_ms = event.get("completed_at_ms")
    if not isinstance(completed_at_ms, int) or completed_at_ms <= 0:
        raise IngestEventError(
            f"ingest event{where} has invalid completed_at_ms "
            f"{completed_at_ms!r}"
        )
    try:
        completed_at = _parse_rfc3339(event.get("completed_at"))
    except (TypeError, ValueError) as exc:
        raise IngestEventError(f"ingest event{where}: {exc}") from exc
    parsed_ms = int(completed_at.timestamp() * 1000)
    if abs(parsed_ms - completed_at_ms) > 1:
        raise IngestEventError(
            f"ingest event{where} completed_at and completed_at_ms disagree"
        )
    return event


def _read_events_unlocked(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events: list[dict] = []
    identities: dict[tuple[str, str], dict] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise IngestEventError(
                f"{path} is corrupt at line {line_number}: {exc.msg}"
            ) from exc
        event = _validate_event(value, line_number=line_number)
        identity = (event["event"], event["run_id"])
        if identity in identities:
            raise IngestEventError(
                f"{path} contains duplicate event/run_id {identity!r}"
            )
        identities[identity] = event
        events.append(event)
    return events


def load_ingest_events(config) -> list[dict]:
    """Read and strictly validate the complete event ledger."""
    return _read_events_unlocked(ingest_event_path(config))


def append_ingest_event(config, event: dict) -> tuple[dict, bool]:
    """Atomically append one event; replaying the same run is idempotent.

    Returns ``(event, appended)``. A reused ``event/run_id`` with different
    content is a hard conflict instead of a silent overwrite.
    """
    candidate = _validate_event(dict(event))
    path = ingest_event_path(config)
    with _event_lock(config):
        events = _read_events_unlocked(path)
        for existing in events:
            if (
                existing["event"] == candidate["event"]
                and existing["run_id"] == candidate["run_id"]
            ):
                if existing != candidate:
                    raise IngestEventError(
                        "event/run_id already exists with different content: "
                        f"{candidate['event']} {candidate['run_id']}"
                    )
                return existing, False
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(value, ensure_ascii=False, sort_keys=True)
            for value in events + [candidate]
        ]
        atomic_write(path, "\n".join(lines) + "\n")
    return candidate, True


def _source_page_ref(raw_file: Path, config) -> str:
    if is_query_bridge_source(raw_file, config):
        return ""
    page = _stage_3_2_wiki_path_for_source(raw_file, config)
    return PageRef.parse(
        page, config.wiki_root, config.wiki_dir
    ).project_relative


def _ingest_method(raw_file: Path, config) -> str:
    cache_key = source_cache_key(raw_file, config)
    entry = load_cache(config).get("entries", {}).get(cache_key, {})
    return str(entry.get("method") or "") if isinstance(entry, dict) else ""


def build_ingest_completed_event(
    raw_file: Path,
    config,
    source_hash: str,
    run_id: str,
    completed_at_ms: int,
) -> dict:
    event = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event": INGEST_COMPLETED,
        "run_id": run_id,
        "source": canonical_source_path(raw_file, config),
        "source_hash": source_hash,
        "source_page": _source_page_ref(raw_file, config),
        "completed_at": rfc3339_from_ms(completed_at_ms),
        "completed_at_ms": completed_at_ms,
        "mode": "ingest",
    }
    method = _ingest_method(raw_file, config)
    if method:
        event["method"] = method
    return _validate_event(event)


def build_repair_completed_event(
    raw_file: Path,
    config,
    source_hash: str,
    completed_at_ms: int,
    *,
    repair_kind: str,
    run_id: str | None = None,
) -> dict:
    return _validate_event({
        "schema_version": EVENT_SCHEMA_VERSION,
        "event": REPAIR_COMPLETED,
        "run_id": run_id or str(uuid.uuid4()),
        "source": canonical_source_path(raw_file, config),
        "source_hash": source_hash,
        "source_page": _source_page_ref(raw_file, config),
        "completed_at": rfc3339_from_ms(completed_at_ms),
        "completed_at_ms": completed_at_ms,
        "mode": "repair",
        "repair_kind": repair_kind,
    })


def ingest_events_for_source(
    events: list[dict], source_identity: str
) -> list[dict]:
    return sorted(
        (
            event for event in events
            if event.get("event") == INGEST_COMPLETED
            and event.get("source") == source_identity
        ),
        key=lambda event: (event["completed_at_ms"], event["run_id"]),
    )


def _ordered_source_frontmatter(fm: dict) -> dict:
    """Place the two ingest projections directly after ``updated``."""
    projected = {
        key: value for key, value in fm.items()
        if key not in SOURCE_TIME_FIELDS
    }
    result: dict = {}
    inserted = False
    for key, value in projected.items():
        result[key] = value
        if key == "updated":
            for time_key in SOURCE_TIME_FIELDS:
                result[time_key] = fm[time_key]
            inserted = True
    if not inserted:
        for time_key in SOURCE_TIME_FIELDS:
            result[time_key] = fm[time_key]
    return result


def project_source_ingest_times(
    config,
    source_identity: str,
    source_page: str,
    *,
    events: list[dict] | None = None,
) -> tuple[Path | None, bool]:
    """Project first/last completion times without changing ``updated``.

    ``created`` is corrected only backwards, to the earliest evidence-backed
    completion date. This preserves an older logical creation date and repairs
    the common ``--delete``/re-ingest reset without inventing an earlier date.
    """
    if not source_page:
        return None, False
    history = events if events is not None else load_ingest_events(config)
    source_events = ingest_events_for_source(history, source_identity)
    if not source_events:
        return None, False
    page = PageRef.parse(
        source_page, config.wiki_root, config.wiki_dir
    ).absolute_path
    if not page.is_file():
        raise IngestEventError(
            f"cannot project ingest times onto missing source page: {page}"
        )
    original = page.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(original)
    if not fm:
        raise IngestEventError(
            f"cannot project ingest times onto a page without frontmatter: {page}"
        )
    first = source_events[0]
    last = source_events[-1]
    old_updated = fm.get("updated")
    fm["first_ingested_at"] = first["completed_at"]
    fm["last_ingested_at"] = last["completed_at"]
    first_date = first["completed_at"][:10]
    created = str(fm.get("created") or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", created) or first_date < created:
        fm["created"] = first_date
    if old_updated is not None:
        fm["updated"] = old_updated
    rendered = write_frontmatter(_ordered_source_frontmatter(fm), body)
    if rendered == original:
        return page, False
    atomic_write(page, rendered)
    return page, True


def _log_contains_run(log_text: str, run_id: str) -> bool:
    return f"- Run: `{run_id}`" in log_text


def project_event_to_log(config, event: dict) -> tuple[Path, bool]:
    """Append the human-readable event projection, idempotent by run_id."""
    event = _validate_event(event)
    log_path = config.wiki_dir / "log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_text = (
        log_path.read_text(encoding="utf-8")
        if log_path.exists()
        else "# Log\n"
    )
    if _log_contains_run(log_text, event["run_id"]):
        return log_path, False
    label = (
        "INGEST COMPLETED"
        if event["event"] == INGEST_COMPLETED
        else "REPAIR COMPLETED"
    )
    lines = [
        "",
        f"## {event['completed_at']} — {label}",
        f"- Run: `{event['run_id']}`",
        f"- Source: `{event['source']}`",
        f"- Hash: {event['source_hash']}",
    ]
    if event.get("source_page"):
        lines.append(f"- Source page: `{event['source_page']}`")
    if event.get("method"):
        lines.append(f"- Method: {event['method']}")
    if event.get("repair_kind"):
        lines.append(f"- Repair: {event['repair_kind']}")
    rendered = log_text.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
    atomic_write(log_path, rendered)
    return log_path, True


def commit_ingest_completion(config, event: dict) -> tuple[dict, bool]:
    """Commit a completed run after all mandatory pipeline work succeeded.

    Rebuildable projections are written first; the authoritative event is the
    commit point. A crash after the event but before ``ingested`` is safe: the
    same run replays both projections idempotently and then restores the marker.
    """
    event = _validate_event(event)
    existing_events = load_ingest_events(config)
    same_run = next(
        (
            value for value in existing_events
            if value["event"] == event["event"]
            and value["run_id"] == event["run_id"]
        ),
        None,
    )
    if same_run is not None and same_run != event:
        raise IngestEventError(
            f"run_id {event['run_id']} already identifies a different event"
        )
    candidate_events = existing_events if same_run else existing_events + [event]
    project_source_ingest_times(
        config,
        event["source"],
        str(event.get("source_page") or ""),
        events=candidate_events,
    )
    project_event_to_log(config, event)
    return append_ingest_event(config, event)


def commit_repair_completion(config, event: dict) -> tuple[dict, bool]:
    event = _validate_event(event)
    if event["event"] != REPAIR_COMPLETED:
        raise IngestEventError("repair commit requires a repair_completed event")
    existing_events = load_ingest_events(config)
    same_run = next(
        (
            value for value in existing_events
            if value["event"] == event["event"]
            and value["run_id"] == event["run_id"]
        ),
        None,
    )
    if same_run is not None and same_run != event:
        raise IngestEventError(
            f"run_id {event['run_id']} already identifies a different event"
        )
    project_event_to_log(config, event)
    return append_ingest_event(config, event)


def summarize_ingest_history(events: list[dict]) -> list[dict]:
    """Return one first/last/count row per source."""
    grouped: dict[str, list[dict]] = {}
    for event in events:
        if event.get("event") == INGEST_COMPLETED:
            grouped.setdefault(event["source"], []).append(event)
    rows = []
    for source, source_events in grouped.items():
        ordered = sorted(
            source_events,
            key=lambda value: (value["completed_at_ms"], value["run_id"]),
        )
        rows.append({
            "source": source,
            "source_page": ordered[-1].get("source_page", ""),
            "first_ingested_at": ordered[0]["completed_at"],
            "first_ingested_at_ms": ordered[0]["completed_at_ms"],
            "last_ingested_at": ordered[-1]["completed_at"],
            "last_ingested_at_ms": ordered[-1]["completed_at_ms"],
            "ingest_count": len(ordered),
        })
    return rows


__all__ = [
    "EVENT_SCHEMA_VERSION",
    "INGEST_COMPLETED",
    "IngestEventError",
    "REPAIR_COMPLETED",
    "append_ingest_event",
    "build_ingest_completed_event",
    "build_repair_completed_event",
    "clear_source_page_time_snapshot",
    "commit_ingest_completion",
    "commit_repair_completion",
    "ingest_event_path",
    "ingest_events_for_source",
    "load_ingest_events",
    "project_event_to_log",
    "project_source_ingest_times",
    "rfc3339_from_ms",
    "restore_source_page_times",
    "snapshot_source_page_times",
    "summarize_ingest_history",
]
