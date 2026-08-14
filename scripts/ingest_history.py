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
from _frontmatter import parse_frontmatter
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


@dataclass(frozen=True)
class _CacheRecord:
    cache_key: str
    source: str
    source_hash: str
    source_page: str
    source_exists: bool
    page_exists: bool


@dataclass
class _MigrationIndex:
    raw_paths: dict[str, list[str]]
    raw_basenames: dict[str, list[str]]
    source_pages: dict[str, list[str]]
    source_page_basenames: dict[str, list[str]]
    source_pages_by_source: dict[str, list[str]]
    cache_records: list[_CacheRecord]


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


def _derived_source_page(source: str) -> str:
    if not source.startswith("raw/"):
        return ""
    return str(
        Path("wiki/sources") / Path(source).relative_to("raw").with_suffix(".md")
    ).replace("\\", "/")


def _indexed_project_paths(root: Path, base: Path, pattern: str) -> dict[str, list[str]]:
    indexed: dict[str, list[str]] = {}
    if not base.is_dir():
        return indexed
    for path in base.rglob(pattern):
        if not path.is_file():
            continue
        project_path = path.relative_to(root).as_posix()
        indexed.setdefault(project_path.casefold(), []).append(project_path)
    return indexed


def _resolve_indexed_path(indexed: dict[str, list[str]], candidate: str) -> str:
    normalized = candidate.replace("\\", "/").lstrip("./")
    matches = indexed.get(normalized.casefold(), [])
    return matches[0] if len(matches) == 1 else normalized


def _basename_index(indexed: dict[str, list[str]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for values in indexed.values():
        for value in values:
            result.setdefault(Path(value).name.casefold(), []).append(value)
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_raw_path(
    config: HistoryConfig,
    index: _MigrationIndex,
    candidate: str,
    *,
    expected_hash: str = "",
    hash_cache: dict[str, str] | None = None,
) -> str:
    normalized = candidate.replace("\\", "/").lstrip("./")
    exact = _resolve_indexed_path(index.raw_paths, normalized)
    if exact != normalized or (config.wiki_root / exact).is_file():
        return exact
    basename_matches = index.raw_basenames.get(
        Path(normalized).name.casefold(), []
    )
    if len(basename_matches) != 1:
        return normalized
    match = basename_matches[0]
    if expected_hash:
        hashes = hash_cache if hash_cache is not None else {}
        actual_hash = hashes.get(match)
        if actual_hash is None:
            actual_hash = _sha256_file(config.wiki_root / match)
            hashes[match] = actual_hash
        if not (
            actual_hash.startswith(expected_hash.lower())
            or expected_hash.lower().startswith(actual_hash)
        ):
            return normalized
    return match


def _resolve_source_page(
    index: _MigrationIndex,
    candidate: str,
    *,
    source: str = "",
) -> str:
    if source:
        bound = index.source_pages_by_source.get(source.casefold(), [])
        selected = _select_bound_source_page(source, bound)
        if selected:
            return selected
    normalized = candidate.replace("\\", "/").lstrip("./")
    exact = _resolve_indexed_path(index.source_pages, normalized)
    if exact != normalized:
        return exact
    basename_matches = index.source_page_basenames.get(
        Path(normalized).name.casefold(), []
    )
    return basename_matches[0] if len(basename_matches) == 1 else normalized


def _select_bound_source_page(source: str, page_refs: list[str]) -> str:
    expected = _derived_source_page(source).casefold()
    exact = [value for value in page_refs if value.casefold() == expected]
    if len(exact) == 1:
        return exact[0]
    return page_refs[0] if len(page_refs) == 1 else ""


def _source_page_bindings(
    config: HistoryConfig,
    raw_paths: dict[str, list[str]],
) -> dict[str, list[str]]:
    bindings: dict[str, list[str]] = {}
    source_root = config.wiki_dir / "sources"
    if not source_root.is_dir():
        return bindings
    for page in source_root.rglob("*.md"):
        try:
            frontmatter, _body = parse_frontmatter(
                page.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue
        values = frontmatter.get("sources", []) if frontmatter else []
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            continue
        page_ref = page.relative_to(config.wiki_root).as_posix()
        for value in values:
            if not isinstance(value, str) or not value.startswith("raw/"):
                continue
            source = _resolve_indexed_path(raw_paths, value)
            if not (config.wiki_root / source).is_file():
                continue
            refs = bindings.setdefault(source.casefold(), [])
            if page_ref not in refs:
                refs.append(page_ref)
    return bindings


def _load_cache_value(config: HistoryConfig) -> dict:
    if not config.cache_path.is_file():
        return {"entries": {}}
    try:
        value = json.loads(config.cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"unreadable ingest cache {config.cache_path}: {exc}"
        ) from exc
    if not isinstance(value, dict) or not isinstance(value.get("entries"), dict):
        raise RuntimeError(
            f"ingest cache has no entries object: {config.cache_path}"
        )
    return value


def _build_migration_index(config: HistoryConfig) -> _MigrationIndex:
    """Index current paths plus legacy cache identities for exact migration."""
    raw_paths = _indexed_project_paths(
        config.wiki_root, config.raw_root, "*"
    )
    raw_basenames = _basename_index(raw_paths)
    source_pages = _indexed_project_paths(
        config.wiki_root, config.wiki_dir / "sources", "*.md"
    )
    source_page_basenames = _basename_index(source_pages)
    source_pages_by_source = _source_page_bindings(config, raw_paths)
    index = _MigrationIndex(
        raw_paths=raw_paths,
        raw_basenames=raw_basenames,
        source_pages=source_pages,
        source_page_basenames=source_page_basenames,
        source_pages_by_source=source_pages_by_source,
        cache_records=[],
    )
    hash_cache: dict[str, str] = {}
    records: list[_CacheRecord] = []
    entries = _load_cache_value(config)["entries"]
    for cache_key, raw_entry in entries.items():
        if not isinstance(cache_key, str) or not isinstance(raw_entry, dict):
            continue
        source_hash = str(
            raw_entry.get("sourceHash") or raw_entry.get("hash") or ""
        ).lower()
        if not re.fullmatch(r"[0-9a-f]{16,64}", source_hash):
            continue
        normalized_key = cache_key.replace("\\", "/").lstrip("/")
        source_candidate = f"raw/{normalized_key}"
        source = _resolve_raw_path(
            config,
            index,
            source_candidate,
            expected_hash=source_hash,
            hash_cache=hash_cache,
        )

        written_pages: list[str] = []
        values = raw_entry.get("filesWritten")
        if isinstance(values, list):
            for value in values:
                if not isinstance(value, str):
                    continue
                normalized = value.replace("\\", "/").lstrip("./")
                if normalized.startswith("wiki/sources/") and normalized.endswith(".md"):
                    written_pages.append(
                        _resolve_source_page(
                            index, normalized, source=source
                        )
                    )
        existing_written = [
            value for value in written_pages
            if (config.wiki_root / value).is_file()
        ]
        derived = _resolve_source_page(
            index, _derived_source_page(source), source=source
        )
        if existing_written:
            source_page = existing_written[0]
        elif derived and (config.wiki_root / derived).is_file():
            source_page = derived
        elif written_pages:
            source_page = written_pages[0]
        else:
            source_page = derived
        records.append(_CacheRecord(
            cache_key=normalized_key,
            source=source,
            source_hash=source_hash,
            source_page=source_page,
            source_exists=(config.wiki_root / source).is_file(),
            page_exists=bool(
                source_page and (config.wiki_root / source_page).is_file()
            ),
        ))

    # Folder and filename cleanups may happen after ingest. For current source
    # pages still not represented by any cache record, hash only those raw
    # files and use an exact full-content match to bind the historical marker.
    covered_pages = {
        record.source_page.casefold()
        for record in records if record.page_exists and record.source_page
    }
    current_by_hash: dict[str, list[tuple[str, str]]] = {}
    for source_key, page_refs in source_pages_by_source.items():
        current_sources = raw_paths.get(source_key, [])
        if len(current_sources) != 1:
            continue
        source = current_sources[0]
        source_page = _select_bound_source_page(source, page_refs)
        if not source_page or source_page.casefold() in covered_pages:
            continue
        source_hash = hash_cache.get(source)
        if source_hash is None:
            source_hash = _sha256_file(config.wiki_root / source)
            hash_cache[source] = source_hash
        current_by_hash.setdefault(source_hash, []).append(
            (source, source_page)
        )

    rebound: list[_CacheRecord] = []
    for record in records:
        matches = current_by_hash.get(record.source_hash, [])
        if len(matches) == 1:
            source, source_page = matches[0]
            rebound.append(_CacheRecord(
                cache_key=record.cache_key,
                source=source,
                source_hash=record.source_hash,
                source_page=source_page,
                source_exists=True,
                page_exists=True,
            ))
        else:
            rebound.append(record)
    index.cache_records = rebound
    return index


def _dedupe_cache_records(records: list[_CacheRecord]) -> list[_CacheRecord]:
    unique: dict[tuple[str, str, str], _CacheRecord] = {}
    for record in records:
        unique[(record.source, record.source_hash, record.source_page)] = record
    return list(unique.values())


def _cache_records_for_hash(
    index: _MigrationIndex, source_hash: str
) -> list[_CacheRecord]:
    value = source_hash.lower()
    return _dedupe_cache_records([
        record for record in index.cache_records
        if record.source_hash.startswith(value) or value.startswith(record.source_hash)
    ])


def _select_cache_record(
    index: _MigrationIndex,
    *,
    source_hash: str,
    source: str = "",
) -> _CacheRecord | None:
    records = _cache_records_for_hash(index, source_hash)
    if source:
        normalized_source = source.replace("\\", "/").lstrip("./")
        direct = _dedupe_cache_records([
            record for record in records
            if record.source.casefold() == normalized_source.casefold()
            or f"raw/{record.cache_key}".casefold() == normalized_source.casefold()
        ])
        if len(direct) == 1:
            return direct[0]
    for predicate in (
        lambda record: record.source_exists and record.page_exists,
        lambda record: record.source_exists,
        lambda record: record.page_exists,
        lambda _record: True,
    ):
        preferred = _dedupe_cache_records([
            record for record in records if predicate(record)
        ])
        if len(preferred) == 1:
            return preferred[0]
    return None


def _resolve_source_evidence(
    config: HistoryConfig,
    index: _MigrationIndex,
    source: str,
    source_hash: str,
    *,
    logged_source_page: str = "",
) -> tuple[str, str, str]:
    """Canonicalize legacy casing/renames without guessing ambiguous hashes."""
    normalized_source = source.replace("\\", "/").lstrip("./")
    record = _select_cache_record(
        index, source_hash=source_hash, source=normalized_source
    )
    if record is not None:
        return record.source, record.source_hash, record.source_page

    canonical_source = _resolve_raw_path(
        config, index, normalized_source, expected_hash=source_hash
    )
    if logged_source_page:
        source_page = _resolve_source_page(
            index, logged_source_page, source=canonical_source
        )
    else:
        source_page = _resolve_source_page(
            index,
            _derived_source_page(canonical_source),
            source=canonical_source,
        )
    return canonical_source, source_hash.lower(), source_page


def _local_ms(value: str) -> tuple[int, str]:
    fmt = "%Y-%m-%d %H:%M:%S" if " " in value else "%Y-%m-%d"
    parsed = datetime.strptime(value, fmt)
    local_tz = datetime.now().astimezone().tzinfo
    aware = parsed.replace(tzinfo=local_tz)
    return int(aware.timestamp() * 1000), (
        "second" if " " in value else "date"
    )


def _legacy_log_events(
    config: HistoryConfig, index: _MigrationIndex
) -> list[dict]:
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
    for heading_index, heading in enumerate(headings):
        end = (
            headings[heading_index + 1].start()
            if heading_index + 1 < len(headings)
            else len(text)
        )
        block = text[heading.end():end]
        source_match = re.search(r"(?m)^- Source: `([^`]+)`\s*$", block)
        hash_match = re.search(r"(?m)^- Hash: ([0-9a-fA-F]{16,64})\s*$", block)
        page_match = re.search(r"(?m)^- Source page: `([^`]+)`\s*$", block)
        if not source_match or not hash_match:
            continue
        completed_at_ms, precision = _local_ms(heading.group(1))
        source, source_hash, source_page = _resolve_source_evidence(
            config,
            index,
            source_match.group(1),
            hash_match.group(1).lower(),
            logged_source_page=page_match.group(1) if page_match else "",
        )
        events.append({
            "schema_version": EVENT_SCHEMA_VERSION,
            "event": INGEST_COMPLETED,
            "run_id": _stable_migration_run_id(
                "log", source, source_hash, completed_at_ms
            ),
            "source": source,
            "source_hash": source_hash,
            "source_page": source_page,
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


def _task_sources_by_prefix(
    config: HistoryConfig, index: _MigrationIndex
) -> dict[str, tuple[str, str, str]]:
    sources: dict[str, tuple[str, str, str]] = {}
    if not config.progress_dir.is_dir():
        return sources
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
        source, source_hash, source_page = _resolve_source_evidence(
            config, index, source, source_hash
        )
        prefix = source_hash[:16]
        value = (source, source_hash, source_page)
        if prefix in sources and sources[prefix] != value:
            raise RuntimeError(
                f"conflicting task manifests for marker prefix {prefix}"
            )
        sources[prefix] = value
    return sources


def _stage_marker_events(
    config: HistoryConfig, index: _MigrationIndex
) -> list[dict]:
    """Recover every completed marker, including pre-task-manifest history."""
    if not config.progress_dir.is_dir():
        return []
    task_sources = _task_sources_by_prefix(config, index)
    events: list[dict] = []
    unresolved: list[Path] = []
    for stage_path in sorted(config.progress_dir.glob("*.stages.json")):
        try:
            stages = json.loads(stage_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"unreadable stage marker {stage_path}: {exc}") from exc
        completed_at_ms = stages.get("ingested")
        if not isinstance(completed_at_ms, int) or completed_at_ms <= 0:
            continue
        prefix = stage_path.name.removesuffix(".stages.json")
        source_info = task_sources.get(prefix)
        if source_info is None:
            record = _select_cache_record(index, source_hash=prefix)
            if record is None:
                unresolved.append(stage_path)
                continue
            source_info = (
                record.source, record.source_hash, record.source_page
            )
        source, source_hash, source_page = source_info
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
            "source_page": source_page,
            "marker": stage_path.relative_to(config.wiki_root).as_posix(),
            "completed_at": rfc3339_from_ms(completed_at_ms),
            "completed_at_ms": completed_at_ms,
            "mode": "migration",
            "origin": "stages_marker",
            "time_precision": "millisecond",
        })
    if unresolved:
        names = ", ".join(path.name for path in unresolved[:10])
        suffix = f" (+{len(unresolved) - 10} more)" if len(unresolved) > 10 else ""
        raise RuntimeError(
            "completed stage marker(s) cannot be mapped through task manifests "
            f"or ingest-cache.json: {names}{suffix}"
        )
    return events


def migration_candidates(config: HistoryConfig) -> list[dict]:
    """Merge legacy evidence without double-counting inferred retries."""
    index = _build_migration_index(config)
    marker_events = _stage_marker_events(config, index)
    legacy_events = []
    for legacy in _legacy_log_events(config, index):
        same_completion = any(
            marker["source"] == legacy["source"]
            and (
                marker["source_hash"].startswith(legacy["source_hash"])
                or legacy["source_hash"].startswith(marker["source_hash"])
            )
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
    marker = str(event.get("marker") or "")
    stage_path = (
        config.wiki_root / marker
        if marker
        else config.progress_dir / f"{str(event['source_hash'])[:16]}.stages.json"
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
