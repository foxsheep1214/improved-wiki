"""Media artifact auditing and targeted repair for completed ingests."""
from __future__ import annotations

import json
import time
from pathlib import Path

from _config import Config
from _core import source_cache_key
from _progress import (
    file_sha256,
    get_stage_payload,
    load_cache,
    mark_stage_done,
    save_cache,
    unmark_stage_done,
)
from _paths import media_slug
from _page_ref import PageRef
from _stage_1_extract import (
    _stage_1_1_reharvest_media,
    _stage_1_2_extract_from_mineru,
    stage_1_2_extract_images,
    stage_1_3_caption_images,
)
from _stage_1_2_images import validate_stage_1_2_artifact
from _stage_1_3_caption import validate_stage_1_3_artifact
from _stage_3_4_inject_images import stage_3_4_inject_images
from _stage_3_7_embed import stage_3_7_embed_new_pages
from _stage_3_write import _stage_3_2_wiki_path_for_source


def _cache_entry(raw_file: Path, config: Config) -> tuple[dict, dict]:
    cache = load_cache(config)
    entry = cache.get("entries", {}).get(source_cache_key(raw_file, config))
    return cache, entry if isinstance(entry, dict) else {}


def mineru_figure_names(
    out_dir: Path,
    *,
    active_chunk_keys: set[str] | None = None,
) -> set[str]:
    """Return figure names from one OCR run's direct chunk manifests.

    ``extract-tmp/<stem>`` is durable across retries and, historically, across
    minerU chunk-size changes.  It can therefore contain complete but stale
    layouts such as both 50-page and 32-page chunks.  A recursive scan merges
    those runs and inflates the media-completeness expectation.  Callers that
    know the current chunk layout must pass ``active_chunk_keys``; repair
    caches are deliberately excluded in all cases.
    """
    names: set[str] = set()
    if not out_dir.is_dir():
        return names
    for manifest_path in out_dir.glob("_chunk_*/_mineru_figures.json"):
        chunk_key = manifest_path.parent.name.removeprefix("_chunk_")
        if active_chunk_keys is not None and chunk_key not in active_chunk_keys:
            continue
        try:
            entries = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                filename = entry.get("filename")
                if isinstance(filename, str) and filename:
                    names.add(filename)
    return names


def _recoverable_mineru_count(raw_file: Path, config: Config) -> int:
    """Count unique figure filenames retained in per-chunk OCR manifests."""
    return len(mineru_figure_names(
        config.extract_tmp_dir / raw_file.stem))


def canonical_mineru_figure_names(raw_file: Path, config: Config) -> set[str]:
    """Return the preserved, source-bound minerU image bytes on disk.

    For ``--keep-media`` re-ingests this directory is intentionally retained;
    unlike OCR scratch manifests it is scoped to exactly one source and is the
    strongest available evidence of media that must not be discarded.
    """
    media_dir = config.wiki_dir / "media" / media_slug(raw_file, config)
    return {
        path.name
        for path in media_dir.glob("p*-mineru_*.*")
        if path.is_file() and not path.name.endswith(".caption.txt")
    }


def restore_or_reharvest_mineru_media(
    raw_file: Path,
    config: Config,
    ocr_out: Path,
    *,
    expected_hint: int = 0,
    expected_names: set[str] | None = None,
) -> tuple[dict, int]:
    """Restore minerU bytes, or re-run an isolated full media harvest.

    ``expected_hint`` may come from the ingest cache or surviving
    ``_mineru_figures.json`` indexes. A non-zero hint can never collapse to an
    empty/partial manifest: missing transient bytes trigger the source-bound,
    resumable media-only re-harvest path.
    """
    # Preserved canonical bytes are source-bound and therefore stronger than
    # scratch manifests.  In particular, an analysis-only ``--keep-media``
    # re-ingest must never shrink its media set merely because a newer OCR
    # layout retained incomplete figure indexes.  Current-run manifest names
    # are unioned in so genuinely missing expected bytes still trigger repair.
    canonical_names = canonical_mineru_figure_names(raw_file, config)
    authoritative_names = canonical_names | (expected_names or set())
    allowed_filenames = authoritative_names if authoritative_names else None
    if authoritative_names:
        expected_hint = len(authoritative_names)
    elif expected_hint <= 0:
        expected_hint = len(mineru_figure_names(ocr_out))
    stage_1_2 = _stage_1_2_extract_from_mineru(
        ocr_out, config, raw_file, allowed_filenames=allowed_filenames)
    if expected_hint <= 0 or stage_1_2.get("count") == expected_hint:
        return stage_1_2, max(expected_hint, stage_1_2.get("count", 0))

    print(
        f"[media repair] Recoverable indexes expect {expected_hint} images, "
        f"but surviving bytes yielded {stage_1_2.get('count', 0)}; "
        "starting isolated minerU media re-harvest."
    )
    repair_out = _stage_1_1_reharvest_media(raw_file, config)
    rebuilt_names = mineru_figure_names(repair_out)
    if not rebuilt_names:
        raise RuntimeError(
            "Media re-harvest completed without any figure bytes even "
            f"though the prior OCR cache recorded {expected_hint} images.")
    stage_1_2 = _stage_1_2_extract_from_mineru(
        repair_out,
        config,
        raw_file,
        allowed_filenames=rebuilt_names,
    )
    rebuilt_count = len(rebuilt_names)
    if stage_1_2.get("count") != rebuilt_count:
        raise RuntimeError(
            f"Media re-harvest materialized "
            f"{stage_1_2.get('count', 0)}/{rebuilt_count} rebuilt "
            "figures; refusing a partial repair.")
    if expected_hint != rebuilt_count:
        print(
            f"[media repair] Full re-harvest changed the authoritative "
            f"figure count from {expected_hint} to {rebuilt_count}; the new "
            "source-bound manifest will replace the stale cache count."
        )
    return stage_1_2, rebuilt_count


def audit_cached_media(
    raw_file: Path,
    config: Config,
    *,
    require_injected: bool = True,
) -> tuple[bool, str, dict]:
    """Audit a completed ingest's media using cache counts plus disk artifacts."""
    _cache, entry = _cache_entry(raw_file, config)
    if not entry:
        return False, "ingest cache entry is missing", {}
    stages = entry.get("stages", {})
    if not isinstance(stages, dict):
        return False, "ingest cache stage statistics are missing", {}
    try:
        expected = int(stages.get("images_extracted", 0))
    except (TypeError, ValueError):
        return False, "cached images_extracted is invalid", {}
    method = str(entry.get("method", ""))

    # A zero cache count is not trustworthy when minerU's surviving per-chunk
    # manifests prove figures existed.
    recoverable = (
        _recoverable_mineru_count(raw_file, config)
        if method.startswith("mineru") else 0
    )
    if expected == 0 and recoverable > 0:
        return (
            False,
            f"cache records 0 images but OCR manifests retain "
            f"{recoverable} recoverable figures",
            {},
        )
    if expected == 0:
        return True, "", {
            "count": 0,
            "captioned": 0,
            "injected": 0,
            "method": method,
        }

    shell = {
        "count": expected,
        "media_dir": str(
            config.wiki_dir / "media" / media_slug(raw_file, config)),
        "mineru": method.startswith("mineru"),
    }
    valid, reason, stage_1_2 = validate_stage_1_2_artifact(
        shell, config, raw_file, expected_count=expected)
    if not valid:
        return False, reason, {}
    valid, reason, stage_1_3 = validate_stage_1_3_artifact(
        stage_1_2, config)
    if not valid:
        return False, reason, {
            "stage_1_2": stage_1_2,
            "stage_1_3": stage_1_3,
        }
    try:
        injected = int(stages.get("images_injected", 0))
    except (TypeError, ValueError):
        return False, "cached images_injected is invalid", {}
    policy = getattr(config, "media_policy", "required")
    if require_injected and policy == "required" and injected != expected:
        return (
            False,
            f"only {injected}/{expected} images were injected into the "
            "source page",
            {"stage_1_2": stage_1_2, "stage_1_3": stage_1_3},
        )
    return True, "", {
        "stage_1_2": stage_1_2,
        "stage_1_3": stage_1_3,
        "count": expected,
        "captioned": stage_1_3.get("complete", 0),
        "injected": injected,
        "method": method,
    }


def assert_cached_media_complete(raw_file: Path, config: Config) -> None:
    """Hard finalization gate before the authoritative ``ingested`` marker."""
    valid, reason, _details = audit_cached_media(
        raw_file, config, require_injected=True)
    if not valid:
        raise RuntimeError(
            f"[media required] Cannot mark {raw_file.name} ingested: {reason}")


def repair_completed_media(raw_file: Path, config: Config) -> dict:
    """Rebuild/caption/inject media without regenerating the book's wiki pages."""
    cache, entry = _cache_entry(raw_file, config)
    if not entry:
        raise RuntimeError(
            f"Cannot repair media for {raw_file.name}: ingest cache entry missing")
    stages = entry.setdefault("stages", {})
    method = str(entry.get("method", ""))
    expected = int(stages.get("images_extracted", 0) or 0)
    recoverable = _recoverable_mineru_count(raw_file, config)
    if expected <= 0:
        expected = recoverable

    source_path = _stage_3_2_wiki_path_for_source(raw_file, config)
    if not source_path.is_file():
        raise RuntimeError(
            f"Cannot repair media: source page missing: {source_path}")

    if method.startswith("mineru"):
        ocr_out = config.extract_tmp_dir / raw_file.stem
        if not ocr_out.is_dir():
            raise RuntimeError(
                f"Cannot repair media: minerU OCR output missing: {ocr_out}")
        stage_1_2, expected = restore_or_reharvest_mineru_media(
            raw_file,
            config,
            ocr_out,
            expected_hint=expected,
        )
    elif raw_file.suffix.lower() in {".pptx", ".docx", ".md", ".markdown"}:
        stage_1_2 = stage_1_2_extract_images(raw_file, config)
    else:
        raise RuntimeError(
            f"Cannot repair media automatically for method {method!r}")

    if expected > 0 and stage_1_2.get("count") != expected:
        raise RuntimeError(
            f"Media repair recovered {stage_1_2.get('count', 0)}/{expected} "
            "expected images; refusing a partial repair.")
    valid, reason, stage_1_2 = validate_stage_1_2_artifact(
        stage_1_2, config, raw_file, expected_count=expected)
    if not valid:
        raise RuntimeError(f"Stage 1.2 repair validation failed: {reason}")

    stage_1_3 = stage_1_3_caption_images(config, stage_1_2)
    valid, reason, actual_captions = validate_stage_1_3_artifact(
        stage_1_2, config)
    if not valid:
        raise RuntimeError(f"Stage 1.3 repair validation failed: {reason}")
    stage_1_3.update(actual_captions)

    injected = stage_3_4_inject_images(
        config, raw_file, source_path, method).get("injected", 0)
    if (getattr(config, "media_policy", "required") == "required"
            and injected != stage_1_2["count"]):
        raise RuntimeError(
            f"Stage 3.2 injected {injected}/{stage_1_2['count']} images")

    # Image injection changes the source page's searchable content. Refresh its
    # local semantic embedding before any completion marker can be restored;
    # otherwise a successfully repaired page would remain stale in retrieval.
    source_ref = PageRef.parse(
        source_path, config.wiki_root, config.wiki_dir)
    stage_3_7_embed_new_pages(config, [source_ref.project_relative])

    source_hash = file_sha256(raw_file)
    mark_stage_done(config, source_hash, "stage_1_2_done")
    mark_stage_done(config, source_hash, "stage_1_3_done")
    write_payload = get_stage_payload(config, source_hash, "write_phase")
    write_payload["images_injected"] = injected
    mark_stage_done(config, source_hash, "write_phase", payload=write_payload)

    stages["images_extracted"] = stage_1_2["count"]
    stages["images_captioned"] = stage_1_3.get("complete", 0)
    stages["images_injected"] = injected
    entry["timestamp"] = int(time.time() * 1000)
    cache["entries"][source_cache_key(raw_file, config)] = entry
    save_cache(config, cache)

    # The caller re-establishes this only after the repaired artifact passes
    # the same finalization audit.
    unmark_stage_done(config, source_hash, "ingested")
    return {
        "images_extracted": stage_1_2["count"],
        "images_captioned": stage_1_3.get("complete", 0),
        "images_injected": injected,
        "source_page": str(source_path),
    }
