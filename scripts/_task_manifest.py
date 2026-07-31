"""Source-bound ingest task manifests and resume invariants.

The artifact cache (``*.json``) stores data and ``*.stages.json`` controls
flow, but neither file alone says which source identity, pipeline contract,
chunk plan, and page set constitute one ingest task. This manifest is the
auditable envelope around both files and remains after progress cleanup.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlparse

from _core import (
    canonical_source_path,
    is_query_bridge_source,
    source_cache_key,
)
from _progress import (
    file_sha256,
    load_cache,
    load_progress,
    load_stages,
    progress_path,
    save_cache,
    stages_path,
)
from _page_ref import PageRef, canonical_page_refs
from _paths import atomic_write, media_slug

TASK_MANIFEST_SCHEMA_VERSION = 1
PIPELINE_CONTRACT_VERSION = "improved-wiki-ingest-v1"
POSTWRITE_COMPLETION_MARKERS = (
    "write_loop_done",
    "write_phase",
    "review_done",
    "aggregate_done",
)


class TaskManifestError(RuntimeError):
    """A task manifest does not match the resume state on disk."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _stable_hash(value) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def task_manifest_path(config, source_hash: str) -> Path:
    config.progress_dir.mkdir(parents=True, exist_ok=True)
    return config.progress_dir / f"{source_hash[:16]}.task.json"


def _endpoint_host(url: str) -> str:
    return urlparse(url or "").hostname or ""


def _artifact_ref(path: Path, wiki_root: Path) -> str:
    """Prefer project-relative diagnostics; retain absolute test/custom paths."""
    try:
        return str(path.relative_to(wiki_root))
    except ValueError:
        return str(path)


def _task_contract(config) -> dict:
    """Serializable, secret-free contract that can explain resume drift."""
    return {
        "pipeline_contract": PIPELINE_CONTRACT_VERSION,
        "artifact_schemas": {
            "chunk_plan": 2,
            "chunk_analysis": 1,
            "media_manifest": 3,
            "page_ref": 1,
            "task_manifest": TASK_MANIFEST_SCHEMA_VERSION,
        },
        "analysis": {
            "llm_model": config.llm_model,
            "context_size": config.context_size,
            "source_budget": config.source_budget,
            "target_tokens": config.target_tokens,
            "target_chars": config.target_chars,
            "chunk_overlap": config.chunk_overlap,
            "max_tokens": config.max_tokens,
        },
        "media": {
            "policy": getattr(config, "media_policy", "required"),
            "caption_model": config.caption_model,
            "caption_protocol": config.caption_protocol,
            "caption_host": _endpoint_host(config.caption_base_url),
            "fallback_model": config.caption_fallback_model,
            "fallback_protocol": config.caption_fallback_protocol,
            "fallback_host": _endpoint_host(
                config.caption_fallback_base_url),
        },
    }


def _new_manifest(raw_file: Path, config, source_hash: str) -> dict:
    identity = canonical_source_path(raw_file, config)
    cache_key = source_cache_key(raw_file, config)
    cache_entry = (
        load_cache(config).get("entries", {}).get(cache_key, {}) or {}
    )
    cached_page_refs = []
    if isinstance(cache_entry, dict):
        values = cache_entry.get("filesWritten", [])
        if isinstance(values, list):
            cached_page_refs = canonical_page_refs(
                values, config.wiki_root, config.wiki_dir)
    task_key = f"{identity}\0{source_hash}".encode("utf-8")
    contract = _task_contract(config)
    now = _now_ms()
    return {
        "schema_version": TASK_MANIFEST_SCHEMA_VERSION,
        "task_id": hashlib.sha256(task_key).hexdigest()[:24],
        "status": "running",
        "source": {
            "identity": identity,
            "cache_key": cache_key,
            "sha256": source_hash,
            "size_bytes": raw_file.stat().st_size,
        },
        "contract": contract,
        "contract_sha256": _stable_hash(contract),
        "contract_history": [],
        "artifacts": {
            "progress": _artifact_ref(
                progress_path(config, source_hash), config.wiki_root),
            "stages": _artifact_ref(
                stages_path(config, source_hash), config.wiki_root),
            "media": str(Path("wiki/media") / media_slug(raw_file, config)),
        },
        "resume": {
            "stage_markers": [],
            "stage_payload_sha256": {},
            "chunk_plan_sha256": "",
            "page_refs": cached_page_refs,
        },
        "created_at": now,
        "updated_at": now,
    }


def _read_manifest(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise TaskManifestError(
            f"task manifest is unreadable: {path}: "
            f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(value, dict):
        raise TaskManifestError(
            f"task manifest is not a JSON object: {path}")
    return value


def _stage_snapshot(stages: dict) -> tuple[list[str], dict[str, str]]:
    markers = sorted(
        key for key, value in stages.items()
        if not key.endswith("__payload") and bool(value)
    )
    payload_hashes = {
        key[: -len("__payload")]: _stable_hash(value)
        for key, value in stages.items()
        if key.endswith("__payload")
    }
    return markers, payload_hashes


def _sync_from_disk(manifest: dict, config, source_hash: str) -> dict:
    stages = load_stages(config, source_hash)
    markers, payload_hashes = _stage_snapshot(stages)
    resume = manifest.setdefault("resume", {})
    resume["stage_markers"] = markers
    resume["stage_payload_sha256"] = payload_hashes
    manifest["status"] = "complete" if "ingested" in markers else "running"
    manifest["updated_at"] = _now_ms()
    return manifest


def _reconcile_stale_legacy_page_refs(
    page_refs: list[str],
    config,
    source_hash: str,
    cache_key: str,
) -> list[str]:
    """Auto-heal a source's FIRST-EVER manifest bootstrap (Route A, 2026-07-30).

    ``page_refs`` reflects an ``ingest-cache.json`` "filesWritten" snapshot
    taken when the source was originally written — years before task
    manifests existed (2026-07-21), for some sources before this fix. The
    wiki's own lint (dedup merges, delete-orphans) runs continuously and
    legitimately removes/merges any source's pages afterward; that is normal
    lifecycle, not corruption. Treating the stale snapshot as a forever-
    binding invariant made an ordinary ``ingest.py <file>`` touch of an old,
    otherwise-healthy source crash outright. Observed live on HardwareWiki's
    "Op Amps for Everyone": 7 of 79 originally-written concept pages had
    already been merged away by dedup, each with its own long-unresolved
    ``wiki/REVIEW/missing-page/*`` item from a 2026-07-05 lint run — the gap
    was already known and tracked, not a silent loss.

    Only entries that are truly GONE are pruned (lint deletes files outright).
    An entry that still exists but is empty is left untouched here so
    ``_validate_bound_artifacts``'s "empty written pages" hard-fail still
    catches it — that is a more corruption-like signal this policy does not
    cover. Dropped entries are logged loudly (never silent) and the legacy
    ``ingest-cache.json`` entry is corrected to match, so the completion gate's
    cached/manifest/supplied three-way equality can still hold later.

    Only called from a source's first-ever bootstrap (no ``.task.json`` yet).
    An EXISTING, actively-tracked manifest keeps the strict behavior — a page
    vanishing there is a live corruption signal within the current resume's
    own bookkeeping, not accumulated lint history, and must still hard-fail.
    """
    kept, dropped = [], []
    for value in page_refs:
        ref = PageRef.parse(value, config.wiki_root, config.wiki_dir)
        if ref.absolute_path.is_file():
            kept.append(value)  # existing (even if empty) — let the caller validate size
        else:
            dropped.append(value)
    if not dropped:
        return page_refs

    preview = ", ".join(dropped[:5])
    more = f" (+{len(dropped) - 5} more)" if len(dropped) > 5 else ""
    print(
        f"  [task-manifest] ⚠️  auto-healed {len(dropped)} stale legacy page "
        f"ref(s) at first-time manifest bootstrap — no longer on disk, "
        f"treated as legitimate wiki-lint dedup/delete-orphans history, not "
        f"corruption: {preview}{more}"
    )

    cache = load_cache(config)
    entry = cache.get("entries", {}).get(cache_key)
    if isinstance(entry, dict) and isinstance(entry.get("filesWritten"), list):
        entry["filesWritten"] = kept
        save_cache(config, cache)

    # A write_phase/write_loop_done resume reads its files_written straight
    # from this stages.json payload (_ingest_write._do_write), independent of
    # the manifest/cache above. Leaving it stale would reintroduce the same
    # gone pages into the review/finalization set on the very next resume.
    stages = load_stages(config, source_hash)
    changed = False
    for marker in ("write_phase", "write_loop_done"):
        payload = stages.get(f"{marker}__payload")
        if not isinstance(payload, dict):
            continue
        values = payload.get("files_written")
        if not isinstance(values, list):
            continue
        refs = canonical_page_refs(values, config.wiki_root, config.wiki_dir)
        pruned = [
            r for r in refs
            if PageRef.parse(r, config.wiki_root, config.wiki_dir).absolute_path.is_file()
        ]
        if len(pruned) != len(refs):
            payload["files_written"] = pruned
            changed = True
    if changed:
        atomic_write(
            stages_path(config, source_hash),
            json.dumps(stages, ensure_ascii=False, indent=2),
        )

    return kept


def _validate_bound_artifacts(
    manifest: dict,
    config,
    source_hash: str,
) -> None:
    """Verify immutable bindings when the corresponding artifact exists."""
    resume = manifest.get("resume", {})
    expected_plan = resume.get("chunk_plan_sha256", "")
    progress = load_progress(config, source_hash)
    if expected_plan and progress and "chunk_plan_v2" in progress:
        actual_plan = _stable_hash(progress["chunk_plan_v2"])
        if actual_plan != expected_plan:
            raise TaskManifestError(
                "task manifest chunk-plan binding does not match the "
                "artifact cache; refusing an ambiguous resume")

    markers = set(resume.get("stage_markers", []))
    manifest_refs: list[str] = []
    if markers.intersection({
        "write_loop_done",
        "write_phase",
        "review_done",
        "aggregate_done",
        "ingested",
    }):
        missing: list[str] = []
        empty: list[str] = []
        try:
            manifest_refs = canonical_page_refs(
                resume.get("page_refs", []),
                config.wiki_root,
                config.wiki_dir,
            )
        except (TypeError, ValueError) as exc:
            raise TaskManifestError(
                f"task manifest contains an invalid page reference: {exc}"
            ) from exc
        for value in manifest_refs:
            ref = PageRef.parse(
                value, config.wiki_root, config.wiki_dir)
            if not ref.absolute_path.is_file():
                missing.append(ref.project_relative)
            elif ref.absolute_path.stat().st_size <= 0:
                empty.append(ref.project_relative)
        if missing:
            preview = ", ".join(missing[:5])
            more = f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""
            raise TaskManifestError(
                f"task manifest binds missing written pages: "
                f"{preview}{more}. Likely cause: this is a pre-2026-07-21 "
                "source (no .task.json existed yet) whose original write set "
                "was legitimately merged or removed by a later wiki-lint "
                "dedup/delete-orphans pass, not corruption — verify the pages "
                "are genuinely gone (not renamed/merged elsewhere) before "
                "resolving with `ingest.py --delete <file>` followed by a "
                "normal ingest, which establishes a fresh, consistent "
                "completion record.")
        if empty:
            preview = ", ".join(empty[:5])
            more = f" (+{len(empty) - 5} more)" if len(empty) > 5 else ""
            raise TaskManifestError(
                f"task manifest binds empty written pages: "
                f"{preview}{more}")

    stages = load_stages(config, source_hash)
    manifest_ref_set = set(manifest_refs)
    for stage in ("review_done", "aggregate_done"):
        if stage not in markers:
            continue
        payload = stages.get(f"{stage}__payload")
        if not isinstance(payload, dict):
            raise TaskManifestError(
                f"{stage} marker has no auditable payload")
        values = payload.get("page_refs")
        if not isinstance(values, list):
            raise TaskManifestError(
                f"{stage} payload has no page_refs list")
        try:
            payload_refs = canonical_page_refs(
                values, config.wiki_root, config.wiki_dir)
        except (TypeError, ValueError) as exc:
            raise TaskManifestError(
                f"{stage} payload contains an invalid page reference: {exc}"
            ) from exc
        unbound = sorted(set(payload_refs) - manifest_ref_set)
        if unbound:
            raise TaskManifestError(
                f"{stage} payload references pages not bound by the task "
                f"manifest: {', '.join(unbound[:5])}")
        if stage == "aggregate_done":
            required = {"wiki/log.md", "wiki/index.md"}
            missing_required = sorted(required - set(payload_refs))
            if missing_required:
                raise TaskManifestError(
                    "aggregate_done payload is missing required pages: "
                    + ", ".join(missing_required))


def ensure_task_manifest(raw_file: Path, config) -> dict:
    """Create or validate the source-bound manifest before resume/skip logic."""
    source_hash = file_sha256(raw_file)
    path = task_manifest_path(config, source_hash)
    current = _new_manifest(raw_file, config, source_hash)
    if not path.exists():
        # First-ever manifest for this source: its legacy-cache page_refs
        # snapshot may predate later, legitimate wiki-lint dedup/delete-
        # orphans activity. Auto-heal that drift (Route A) instead of hard-
        # failing — see _reconcile_stale_legacy_page_refs.
        current["resume"]["page_refs"] = _reconcile_stale_legacy_page_refs(
            current["resume"]["page_refs"],
            config,
            source_hash,
            current["source"]["cache_key"],
        )
        manifest = _sync_from_disk(current, config, source_hash)
        _validate_bound_artifacts(manifest, config, source_hash)
        atomic_write(
            path, json.dumps(manifest, ensure_ascii=False, indent=2))
        return manifest

    manifest = _read_manifest(path)
    if manifest.get("schema_version") != TASK_MANIFEST_SCHEMA_VERSION:
        raise TaskManifestError(
            f"unsupported task manifest schema "
            f"{manifest.get('schema_version')!r}: {path}")
    recorded_source = manifest.get("source", {})
    for key in ("identity", "cache_key", "sha256"):
        if recorded_source.get(key) != current["source"].get(key):
            raise TaskManifestError(
                "content-hash task collision: existing manifest belongs to "
                f"{recorded_source.get('identity')!r}, current source is "
                f"{current['source']['identity']!r}. Progress is keyed by "
                "source hash and cannot be reused across identities.")

    prior_contract_hash = manifest.get("contract_sha256", "")
    if prior_contract_hash != current["contract_sha256"]:
        history = list(manifest.get("contract_history", []))
        history.append({
            "changed_at": _now_ms(),
            "previous_sha256": prior_contract_hash,
            "current_sha256": current["contract_sha256"],
        })
        manifest["contract_history"] = history[-20:]
        manifest["contract"] = current["contract"]
        manifest["contract_sha256"] = current["contract_sha256"]

    manifest["source"]["size_bytes"] = current["source"]["size_bytes"]
    manifest["artifacts"] = current["artifacts"]
    manifest = _sync_from_disk(manifest, config, source_hash)
    _validate_bound_artifacts(manifest, config, source_hash)
    atomic_write(path, json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def sync_task_manifest(config, source_hash: str) -> None:
    """Refresh stage markers/status after marker mutation; no-op for legacy tasks."""
    path = task_manifest_path(config, source_hash)
    if not path.exists():
        return
    manifest = _sync_from_disk(
        _read_manifest(path), config, source_hash)
    _validate_bound_artifacts(manifest, config, source_hash)
    atomic_write(path, json.dumps(manifest, ensure_ascii=False, indent=2))


def bind_chunk_plan(config, source_hash: str, chunk_plan: dict) -> None:
    """Bind the exact ChunkPlanV2 artifact to this task."""
    path = task_manifest_path(config, source_hash)
    if not path.exists():
        return
    manifest = _read_manifest(path)
    manifest.setdefault("resume", {})["chunk_plan_sha256"] = _stable_hash(
        chunk_plan)
    manifest["updated_at"] = _now_ms()
    atomic_write(path, json.dumps(manifest, ensure_ascii=False, indent=2))


def bind_page_refs(config, source_hash: str, page_refs: list[str | Path]) -> None:
    """Bind the canonical written-page set to this task."""
    path = task_manifest_path(config, source_hash)
    if not path.exists():
        return
    manifest = _read_manifest(path)
    manifest.setdefault("resume", {})["page_refs"] = canonical_page_refs(
        page_refs, config.wiki_root, config.wiki_dir)
    manifest["updated_at"] = _now_ms()
    atomic_write(path, json.dumps(manifest, ensure_ascii=False, indent=2))


def assert_task_ready_for_completion(
    raw_file: Path,
    config,
    files_written: list[str | Path],
    source_hash: str | None = None,
) -> list[str]:
    """Prove all post-write artifacts agree before ``ingested`` is allowed.

    Returns the canonical page set after validation so the embedding stage and
    completion marker consume the exact same artifact binding.
    """
    actual_hash = file_sha256(raw_file)
    if source_hash is not None and source_hash != actual_hash:
        raise TaskManifestError(
            "finalization source hash does not match the source bytes")
    source_hash = actual_hash
    path = task_manifest_path(config, source_hash)
    if not path.is_file():
        raise TaskManifestError(
            f"task manifest is missing at finalization: {path}")

    manifest = ensure_task_manifest(raw_file, config)
    stages = load_stages(config, source_hash)
    missing_markers = [
        marker for marker in POSTWRITE_COMPLETION_MARKERS
        if not stages.get(marker)
    ]
    if missing_markers:
        raise TaskManifestError(
            "cannot finalize before required stage markers: "
            + ", ".join(missing_markers))

    cache = load_cache(config)
    cache_key = source_cache_key(raw_file, config)
    entry = cache.get("entries", {}).get(cache_key)
    if not isinstance(entry, dict):
        raise TaskManifestError(
            f"ingest cache entry is missing for {cache_key}")
    if entry.get("hash") != source_hash:
        raise TaskManifestError(
            "ingest cache hash does not match the source bytes")
    if entry.get("sourceHash", source_hash) != source_hash:
        raise TaskManifestError(
            "ingest cache sourceHash does not match the source bytes")

    cached_values = entry.get("filesWritten")
    if not isinstance(cached_values, list) or not cached_values:
        raise TaskManifestError(
            "ingest cache has no non-empty filesWritten list")
    try:
        cached_refs = canonical_page_refs(
            cached_values, config.wiki_root, config.wiki_dir)
        supplied_refs = canonical_page_refs(
            files_written, config.wiki_root, config.wiki_dir)
        manifest_refs = canonical_page_refs(
            manifest.get("resume", {}).get("page_refs", []),
            config.wiki_root,
            config.wiki_dir,
        )
    except (TypeError, ValueError) as exc:
        raise TaskManifestError(
            f"finalization contains an invalid page reference: {exc}"
        ) from exc

    if not supplied_refs:
        raise TaskManifestError(
            "finalization was given an empty written-page set")
    if set(supplied_refs) != set(cached_refs):
        raise TaskManifestError(
            "finalization page refs do not match ingest-cache filesWritten")
    if set(manifest_refs) != set(cached_refs):
        raise TaskManifestError(
            "task-manifest page refs do not match ingest-cache filesWritten")

    missing: list[str] = []
    empty: list[str] = []
    for value in cached_refs:
        ref = PageRef.parse(value, config.wiki_root, config.wiki_dir)
        if not ref.absolute_path.is_file():
            missing.append(ref.project_relative)
            continue
        if ref.absolute_path.stat().st_size <= 0:
            empty.append(ref.project_relative)
    if missing:
        raise TaskManifestError(
            "cannot finalize with missing written pages: "
            + ", ".join(missing[:5]))
    if empty:
        raise TaskManifestError(
            "cannot finalize with empty written pages: "
            + ", ".join(empty[:5]))

    for stage in ("review_done", "aggregate_done"):
        payload = stages.get(f"{stage}__payload")
        if not isinstance(payload, dict):
            raise TaskManifestError(
                f"{stage} marker has no auditable payload")
        values = payload.get("page_refs")
        if not isinstance(values, list):
            raise TaskManifestError(
                f"{stage} payload has no page_refs list")
        payload_refs = canonical_page_refs(
            values, config.wiki_root, config.wiki_dir)
        if not set(payload_refs).issubset(set(cached_refs)):
            raise TaskManifestError(
                f"{stage} pages are not included in filesWritten")

    aggregate_refs = canonical_page_refs(
        stages["aggregate_done__payload"]["page_refs"],
        config.wiki_root,
        config.wiki_dir,
    )
    required_aggregate_refs = {"wiki/log.md", "wiki/index.md"}
    if not required_aggregate_refs.issubset(set(aggregate_refs)):
        raise TaskManifestError(
            "aggregate_done payload does not bind wiki/log.md and "
            "wiki/index.md")

    if not is_query_bridge_source(raw_file, config):
        from _stage_3_write import _stage_3_1_wiki_path_for_source

        source_page = PageRef.parse(
            _stage_3_1_wiki_path_for_source(raw_file, config),
            config.wiki_root,
            config.wiki_dir,
        )
        if source_page.project_relative not in set(cached_refs):
            raise TaskManifestError(
                "normal-source completion set does not include its source page")
        if not source_page.absolute_path.is_file():
            raise TaskManifestError(
                f"normal-source page is missing: {source_page.project_relative}")

    return cached_refs
