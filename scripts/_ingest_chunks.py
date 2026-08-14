"""_ingest_chunks.py — chunk analysis pipeline 2.2→2.4 (extracted from ingest.py)."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from _config import Config
from _core import (
    stage_begin as _stage_begin,
    PrepareStopAfter,
)
from _progress import (
    file_sha256,
    is_stage_done,
    mark_stage_done,
    unmark_stage_done,
    save_progress,
    delete_progress_keys,
)
from _schema import (
    list_existing_slugs,
    load_purpose_md,
    load_schema_md,
    load_wiki_index_context,
)
from _stage_2_analyze import (
    ChunkAnalysisValidationError,
    _stage_2_2_chunk_text,
    _stage_2_2_analyze_chunk,
    _stage_2_2_chunk_retries,
    _stage_2_2_resolve_chunk_heading_path,
    normalize_and_validate_chunk_analysis,
)
from _stage_2_4_generation import (
    stage_2_4_generate_all,
    _stage_2_4_extract_names,
)
from _stage_2_context import (
    STAGE_2_CONTEXT_POLICY_VERSION,
    build_consolidated_stage_2_context,
)
from _stage_validators import _verify_stage_2_2_chunks, _verify_stage_2_2_digest
from _task_manifest import bind_chunk_plan

CHUNK_PLAN_SCHEMA_VERSION = 3
CHUNKER_VERSION = "token-bounded-heading-aware-v2"
ANALYSIS_POLICY_VERSION = "nashsu-0.6.6-schema-typed-v3-synthesis-thesis"
GENERATION_POLICY_VERSION = STAGE_2_CONTEXT_POLICY_VERSION

_STAGE_2_2_DOWNSTREAM_MARKERS = (
    "stage_2_2_done",
    "stage_2_3_done",
    "stage_2_9_done",  # legacy name: Stage 2.4 closing + source-page gate
    "review_prepared",
    "write_loop_done",
    "enrichment_done",
    "aggregate_done",
    "write_phase",
    "review_done",
    "ingested",
)

_STAGE_2_2_DOWNSTREAM_ARTIFACTS = (
    "slugs_snapshot_2_2",
    "wiki_index_snapshot_2_2",
    "chunk_plan_v2",
    "chunk_analyses",
    "global_digest",
    "analysis",
    "incremental_associations",
    "file_blocks",
    "source_page_response",
    "comp_count",
    "concept_merge_stats",
    "dedup_was_run",
    "generation_policy_version",
)

_STAGE_2_GENERATION_ARTIFACTS = (
    "analysis",
    "incremental_associations",
    "file_blocks",
    "source_page_response",
    "comp_count",
    "concept_merge_stats",
    "dedup_was_run",
    "generation_policy_version",
)

_STAGE_2_GENERATION_MARKERS = (
    "stage_2_3_done",
    "stage_2_9_done",
    "review_prepared",
)

_POST_WRITE_MARKERS = (
    "write_loop_done",
    "enrichment_done",
    "aggregate_done",
    "write_phase",
    "review_done",
)


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _resolve_chunk_spans(
    extracted_text: str,
    chunk_meta: list,
    overlap_cap: int,
) -> list[tuple[int, int]]:
    """Resolve chunk spans monotonically, never with an unscoped ``find``.

    Chunk text is produced from overlapping source slices and stripped at the
    edges. Searching every chunk from byte zero binds repeated passages to the
    first occurrence in the book. Instead, each lookup starts near the prior
    chunk's end, allowing the configured overlap plus a small whitespace
    margin. Exact chunk text is still required; failure is a checkpoint error,
    not a guessed position.
    """
    spans: list[tuple[int, int]] = []
    previous_end = 0
    for position, meta in enumerate(chunk_meta):
        chunk = meta[1]
        search_from = 0
        if position:
            search_from = max(0, previous_end - max(0, overlap_cap) - 4096)
        start = extracted_text.find(chunk, search_from)
        if start < 0:
            raise RuntimeError(
                f"[Stage 2.2] Cannot bind chunk {position + 1} back to the "
                "post-caption extracted text. Refusing to create an unstable "
                "checkpoint plan."
            )
        end = start + len(chunk)
        if spans and start <= spans[-1][0]:
            raise RuntimeError(
                f"[Stage 2.2] Non-monotonic chunk binding at chunk "
                f"{position + 1}: start={start}, prior_start={spans[-1][0]}."
            )
        spans.append((start, end))
        previous_end = end
    return spans


def _build_chunk_plan(
    extracted_text: str,
    config: Config,
    chunk_meta: list,
) -> dict:
    """Build the exact, versioned Stage 2.2 checkpoint compatibility envelope."""
    spans = _resolve_chunk_spans(extracted_text, chunk_meta, config.chunk_overlap)
    chunks: list[dict] = []
    for meta, (start, end) in zip(chunk_meta, spans):
        index, chunk, overlap_before, heading_path = meta
        text_hash = _text_sha256(chunk)
        chunks.append({
            "index": index + 1,
            "chunk_id": f"{index + 1:04d}-{text_hash[:16]}",
            "start": start,
            "end": end,
            "size": len(chunk),
            "text_sha256": text_hash,
            "overlap_sha256": _text_sha256(overlap_before),
            "heading_path": heading_path,
        })
    return {
        "schema_version": CHUNK_PLAN_SCHEMA_VERSION,
        "chunker_version": CHUNKER_VERSION,
        "analysis_policy_version": ANALYSIS_POLICY_VERSION,
        "schema_sha256": _text_sha256(load_schema_md(config)),
        "purpose_sha256": _text_sha256(load_purpose_md(config)),
        "source_text_sha256": _text_sha256(extracted_text),
        "source_text_length": len(extracted_text),
        "context_size": config.context_size,
        "source_budget": config.source_budget,
        "target_tokens": config.target_tokens,
        "target_chars": config.target_chars,
        "overlap_chars": config.chunk_overlap,
        "chunk_total": len(chunks),
        "chunks": chunks,
    }


def _chunk_checkpoint_mismatch(progress: dict, current_plan: dict) -> str | None:
    """Return an incompatibility reason, or ``None`` for an exact safe restore."""
    saved_plan = progress.get("chunk_plan_v2")
    if not isinstance(saved_plan, dict):
        return "legacy checkpoint has no ChunkPlanV2"
    if saved_plan != current_plan:
        for key in (
            "schema_version",
            "chunker_version",
            "analysis_policy_version",
            "schema_sha256",
            "purpose_sha256",
            "source_text_sha256",
            "source_text_length",
            "context_size",
            "source_budget",
            "target_tokens",
            "target_chars",
            "overlap_chars",
            "chunk_total",
            "chunks",
        ):
            if saved_plan.get(key) != current_plan.get(key):
                return f"ChunkPlanV2 field changed: {key}"
        return "ChunkPlanV2 differs"

    analyses = progress.get("chunk_analyses")
    if not isinstance(analyses, list):
        return "chunk_analyses is not a list"
    plan_chunks = current_plan["chunks"]
    if len(analyses) != len(plan_chunks):
        return (
            f"analysis count {len(analyses)} != planned chunk count "
            f"{len(plan_chunks)}"
        )
    seen_ids: set[str] = set()
    for position, (analysis, planned) in enumerate(zip(analyses, plan_chunks), 1):
        if not isinstance(analysis, dict):
            return f"analysis {position} is not a mapping"
        try:
            normalized = normalize_and_validate_chunk_analysis(
                analysis,
                expected_index=planned["index"],
                expected_total=current_plan["chunk_total"],
            )
        except ChunkAnalysisValidationError as exc:
            return f"analysis {position} failed schema validation: {exc}"
        analyses[position - 1] = normalized
        analysis = normalized
        chunk_id = analysis.get("_chunk_id")
        if chunk_id != planned["chunk_id"]:
            return f"analysis {position} chunk_id does not match its planned chunk"
        if analysis.get("_chunk_text_sha256") != planned["text_sha256"]:
            return f"analysis {position} text hash does not match its planned chunk"
        if analysis.get("_chunk_index") != planned["index"]:
            return f"analysis {position} index does not match its planned chunk"
        if chunk_id in seen_ids:
            return f"duplicate analysis chunk_id: {chunk_id}"
        seen_ids.add(chunk_id)
    return None


def _invalidate_stage_2_2_checkpoint(
    config: Config,
    source_hash: str,
    reason: str,
) -> None:
    """Invalidate Stage 2.2 and every artifact/marker derived from it."""
    print(
        "  [stage 2.2] ⚠️  cached checkpoint is incompatible "
        f"({reason}) — invalidating Stage 2.2 and downstream only."
    )
    delete_progress_keys(
        config, source_hash, list(_STAGE_2_2_DOWNSTREAM_ARTIFACTS))
    for marker in _STAGE_2_2_DOWNSTREAM_MARKERS:
        unmark_stage_done(config, source_hash, marker)


def _invalidate_stage_2_generation_checkpoint(
    config: Config,
    source_hash: str,
    progress: dict,
    reason: str,
) -> None:
    """Invalidate only the wiki-dependent Stage 2.3+ generation segment.

    The Stage 2.2 chunk plan and validated analyses remain reusable. This keeps
    prefetched long-book analysis intact while preventing a legacy per-chunk
    Stage 2.4 result or source page from being mistaken for the consolidated
    NashSU-style generation policy.
    """
    print(
        "  [stage 2.4] ⚠️  cached generation is incompatible "
        f"({reason}) — preserving Stage 2.2 and rebuilding Stage 2.3+."
    )
    delete_progress_keys(
        config,
        source_hash,
        list(_STAGE_2_GENERATION_ARTIFACTS),
    )
    for key in _STAGE_2_GENERATION_ARTIFACTS:
        progress.pop(key, None)
    for marker in _STAGE_2_GENERATION_MARKERS:
        unmark_stage_done(config, source_hash, marker)


def _assert_chunk_count_alignment(chunk_meta: list, chunk_analyses: list) -> None:
    """Prevent ``zip`` from silently truncating generation input."""
    if len(chunk_meta) != len(chunk_analyses):
        raise RuntimeError(
            "[Stage 2.4] Chunk plan/analysis cardinality mismatch: "
            f"{len(chunk_meta)} planned chunks vs "
            f"{len(chunk_analyses)} analyses. Stage 2.2 must be re-run."
        )


def _parse_accumulated_to_dict(accumulated) -> dict:
    """Parse the rolled-up accumulated_digest back to a dict for Stage 2.4.

    2.2's per-chunk updated_global_digest refines accumulated_digest across
    chunks (NashSU rolling-digest parity). Stage 2.4 consumes the
    structured fields (book_meta/outline/key_concepts/key_claims/key_entities),
    so the final accumulated value must be a dict. Returns {} for empty/corrupt.
    """
    if not accumulated:
        return {}
    if isinstance(accumulated, dict):
        return accumulated
    s = str(accumulated).strip()
    if not s or s in ("{}", '""'):
        return {}
    try:
        import json as _j
        parsed = _j.loads(s)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    try:
        import yaml as _y
        parsed = _y.safe_load(s)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _analyze_all_chunks(
    chunk_meta: list, global_digest: dict, accumulated_digest: str,
    raw_file: Path, config: Config, template_content: str,
    chunk_total: int, t_start: float, verbose: bool,
    existing_slugs: list | None = None,
    wiki_index_context: str = "",
) -> list:
    """Stage 2.2: analyze all chunks, serially.

    Serial preserves cross-chunk ``accumulated_digest`` refinement \u2014 each
    chunk's analysis is informed by the previous chunk's updated digest.
    Conversation mode is the only text-gen path, so there is no parallel
    branch: every call is a manual round-trip, which is inherently serial.
    Returns chunk_analyses indexed by chunk order.

    ``existing_slugs`` and ``wiki_index_context`` are this source's persisted
    Stage 2.2 snapshots (see _run_chunk_pipeline) so every chunk prompt is built
    from the SAME frozen wiki context — prompt-hash stable across resumes while
    matching NashSU's current-index analysis context.
    """
    chunk_analyses: list = []

    for i, chunk, overlap_before, heading_path in chunk_meta:
        ca = _stage_2_2_analyze_chunk(
            chunk, i, chunk_total, global_digest, accumulated_digest,
            overlap_before, heading_path, raw_file, config, template_content,
            max_retries=_stage_2_2_chunk_retries(), verbose=verbose,
            existing_slugs=existing_slugs,
            wiki_index_context=wiki_index_context)
        chunk_analyses.append(ca)
        updated = ca.get("updated_global_digest", "")
        if isinstance(updated, str) and len(updated.strip()) > 50:
            accumulated_digest = updated.strip()
        elif isinstance(updated, dict):
            accumulated_digest = json.dumps(updated, ensure_ascii=False, indent=2)
        done = i + 1
        pct = done * 100 // chunk_total
        eta = ((time.time() - t_start) / done) * (chunk_total - done) if done > 0 else 0
        print(f"  [analyze] {done}/{chunk_total} [{pct}% ETA {eta:.0f}s]")
    return chunk_analyses, accumulated_digest

def _generate_all_chunks(
    chunk_meta: list, chunk_analyses: list, existing_refs: dict,
    raw_file: Path, config: Config, template_content: str,
    chunk_total: int, t_start: float, verbose: bool,
    related_pages: list[dict] | None = None,
    global_digest: dict | None = None,
) -> tuple[list, list, str | None]:
    """Stage 2.4: one consolidated generation call for the whole source.

    NashSU 0.6.6 finishes every serial chunk analysis first, then performs one
    generation call with the final rolling digest and all chunk analyses.
    improved-wiki follows that order and additionally includes bounded raw
    evidence from every chunk. Stage 2.3 associations remain an explicit local
    extension and are threaded into this single prompt.
    """
    _assert_chunk_count_alignment(chunk_meta, chunk_analyses)
    consolidated_context = build_consolidated_stage_2_context(
        global_digest or {},
        chunk_analyses,
        chunk_meta,
        getattr(config, "source_budget", 100_000),
    )
    all_file_blocks, generated_slugs, stop_reason = stage_2_4_generate_all(
        chunk_analyses,
        raw_file,
        config,
        template_content,
        verbose=verbose,
        existing_refs=existing_refs,
        related_pages=related_pages,
        consolidated_context=consolidated_context,
    )
    print(
        f"  [generate] {chunk_total}/{chunk_total} "
        f"[single consolidated call, {time.time() - t_start:.0f}s]"
    )
    return all_file_blocks, generated_slugs, stop_reason


def _run_chunk_pipeline(
    extracted_text: str, global_digest: dict, raw_file: Path, config: Config,
    template_content: str, progress: dict | None, verbose: bool,
    analyze_only: bool = False,
) -> tuple[list, dict, list, dict, dict]:
    """Stage 2.2 \u2192 2.3 \u2192 2.4: analyze all chunks, detect existing-wiki
    associations, then generate pages with associations fed into each prompt.

    Split (2026-06-21): analysis and generation are separate phases so Stage 2.3
    (incremental association detection) can run between them and feed back into
    the generation prompt. Returns
    ``(chunk_analyses, analysis, file_blocks, incremental_associations,
    global_digest)``.

    ``analyze_only`` (prefetch boundary, 2026-06-28): Stage 2.2 freezes one
    read-only slug/index snapshot at entry, then every chunk reads only that
    snapshot plus the source text/digest. Stage 2.3 is the first stage that
    performs a fresh live wiki read. In batch mode the next source's 2.2 may be
    prefetched in parallel while the current source holds the serial wiki-write
    spine; ``analyze_only=True`` runs/caches 2.2 then raises
    ``PrepareStopAfter("1.5")`` BEFORE the wiki-dependent 2.3+ stages. The cached
    2.2 is restored later (under ``stage_2_2_done``) when the book reaches the
    spine and runs 2.3+ for real.
    """
    # Rebuild the CURRENT plan before accepting any Stage 2.2-derived cache.
    # The source/config/chunker in this run are authoritative. A legacy or
    # drifted checkpoint is locally invalidated rather than paired to new
    # chunks by list position.
    chunk_meta, chunk_total = _build_chunk_meta(extracted_text, config)
    chunk_plan = _build_chunk_plan(extracted_text, config, chunk_meta)

    _h = file_sha256(raw_file)
    checkpoint_invalidated = False
    _has_stage_2_cache = bool(
        progress
        and "chunk_analyses" in progress
        and (
            is_stage_done(config, _h, "stage_2_2_done")
            or is_stage_done(config, _h, "stage_2_3_done")
        )
    )
    if _has_stage_2_cache:
        mismatch = _chunk_checkpoint_mismatch(progress, chunk_plan)
        if mismatch:
            _invalidate_stage_2_2_checkpoint(config, _h, mismatch)
            checkpoint_invalidated = True
    # Once any incompatible cache has been invalidated, bind the current plan
    # before a cached restore or a new Stage 2.2 marker can be accepted.
    bind_chunk_plan(config, _h, chunk_plan)

    # Cached: chunk analysis already complete. Stage-completion is the single
    # source of truth in stages.json (stage_2_3_done); chunk_analyses presence
    # in the artifact store guards against a missing artifact.
    if (progress and "chunk_analyses" in progress
            and is_stage_done(config, _h, "stage_2_3_done")):
        cached_generation_policy = progress.get("generation_policy_version")
        generation_cache_compatible = (
            cached_generation_policy == GENERATION_POLICY_VERSION
        )
        if not generation_cache_compatible:
            post_write_started = any(
                is_stage_done(config, _h, marker)
                for marker in _POST_WRITE_MARKERS
            )
            if post_write_started:
                # Pages are already on disk. Replaying a different Stage 2.4
                # payload here would cross the non-idempotent write boundary.
                # Finish this already-started legacy source; new/pre-write
                # sources use the consolidated policy.
                generation_cache_compatible = True
                print(
                    "  [stage 2.4] ⚠️  legacy per-chunk generation cache is "
                    "already past the write boundary — preserving it for safe "
                    "resume. Re-ingest explicitly to regenerate this source "
                    "under the consolidated policy."
                )
            else:
                _invalidate_stage_2_generation_checkpoint(
                    config,
                    _h,
                    progress,
                    "generation policy changed from "
                    f"{cached_generation_policy or 'legacy-per-chunk'} to "
                    f"{GENERATION_POLICY_VERSION}",
                )

        # Restore file_blocks DIRECTLY from the artifact store. The retired
        # design re-parsed ``raw_response`` (= "\n".join(block BODIES), bodies
        # without the ---FILE:...--- wrappers), so parse_file_blocks() returned
        # [] and silently dropped every key concept/entity page on resume.
        #
        # The ``file_blocks`` key being PRESENT (even as []) is an authoritative
        # restore: [] is the legitimate "every key candidate already overlaps an
        # existing wiki page" outcome. The key being ABSENT means an old/partial
        # cache that predates file_blocks persistence \u2014 there is no safe way to
        # recover it, so rather than proceed with 0 blocks (re-introducing the
        # silent loss) we invalidate the stage marker and fall through to
        # re-run the chunk pipeline.
        persisted_blocks = (
            progress.get("file_blocks")
            if generation_cache_compatible
            else None
        )
        if generation_cache_compatible and persisted_blocks is None:
            print("  [stage 2.2] \u26a0\ufe0f  stage_2_3_done set but no persisted "
                  "file_blocks artifact \u2014 invalidating marker and re-running "
                  "chunk pipeline (prevents silent concept/entity loss).")
            _invalidate_stage_2_generation_checkpoint(
                config,
                _h,
                progress,
                "stage_2_3_done has no persisted file_blocks",
            )
            generation_cache_compatible = False
        if generation_cache_compatible:
            chunk_analyses = progress["chunk_analyses"]
            print(f"  [stage 2.2] (cached) Chunk Analysis \u2014 {len(chunk_analyses)} chunks")
            _verify_stage_2_2_chunks(
                chunk_analyses, extracted_text, chunk_plan=chunk_plan)
            # 2.3 is already done \u2014 prefetch (2.2) is a no-op, stop before any
            # wiki-dependent work re-runs.
            if analyze_only:
                raise PrepareStopAfter("1.5")
            analysis = progress.get("analysis", {})
            incremental_associations = progress.get("incremental_associations", {})
            global_digest = progress.get("global_digest", global_digest)
            _verify_stage_2_2_digest(global_digest, raw_file)
            return chunk_analyses, analysis, persisted_blocks, incremental_associations, global_digest

    # Prefetch resume: Stage 2.2 was cached on its own (analyze_only run) but 2.3+
    # has not run yet. Restore chunk_analyses and skip re-analysis. When the caller
    # is itself a prefetch (analyze_only), stop again at the 2.2 boundary; otherwise
    # fall through to run the wiki-dependent 2.3+ stages with the cached analyses.
    if (progress and "chunk_analyses" in progress
            and is_stage_done(config, _h, "stage_2_2_done")):
        chunk_analyses = progress["chunk_analyses"]
        print(f"  [stage 2.2] (cached) Chunk Analysis \u2014 {len(chunk_analyses)} chunks "
              f"(prefetched)")
        _verify_stage_2_2_chunks(
            chunk_analyses, extracted_text, chunk_plan=chunk_plan)
        # Restore the persisted roll-up digest. A pre-roll-up cache (no valid
        # persisted global_digest) would silently feed an empty digest to
        # Stage 2.4 — same pattern as the stage_2_3_done restore above:
        # warn, invalidate the marker, and fall through to re-run 2.2. This
        # validation runs even for another analyze-only prefetch resume.
        _digest_cached = progress.get("global_digest")
        _digest_keys = {"book_meta", "outline", "key_concepts", "key_claims", "key_entities"}
        if not isinstance(_digest_cached, dict) or not _digest_keys.issubset(_digest_cached):
            print("  [stage 2.2] ⚠️  stage_2_2_done set but no valid rolled-up "
                  "global_digest persisted (pre-roll-up cache?) — invalidating "
                  "marker and re-running chunk analysis (prevents an empty "
                  "digest reaching Stage 2.4).")
            unmark_stage_done(config, _h, "stage_2_2_done")
        else:
            try:
                _verify_stage_2_2_digest(_digest_cached, raw_file)
            except RuntimeError as exc:
                print(
                    "  [stage 2.2] ⚠️  cached rolled-up global_digest failed "
                    f"full shape/type validation ({exc}) — invalidating marker "
                    "and re-running chunk analysis."
                )
                unmark_stage_done(config, _h, "stage_2_2_done")
            else:
                global_digest = _digest_cached
                if analyze_only:
                    raise PrepareStopAfter("1.5")
                result = _generate_from_analyses(
                    chunk_analyses, extracted_text, global_digest, raw_file, config,
                    template_content, verbose)
                return (*result, global_digest)

    # \u2500\u2500 Stage 2.2: build chunk plan + analyze (frozen wiki context snapshot) \u2500\u2500
    est_sec = chunk_total * 75
    print(f"  [stage 2.2] Analyze \u2014 {chunk_total} chunk(s), "
          f"target {config.target_chars:,} chars/chunk (est. {est_sec/60:.0f} min)")
    _stage_begin("Stage 2.2: Chunk Analysis")
    t_start = time.time()
    # The global digest starts empty and rolls up across chunks via each
    # chunk's updated_global_digest.
    accumulated_digest = ""

    # Existing-wiki SNAPSHOT: freeze both the slug list and NashSU-style current
    # index ONCE per source before Stage 2.2. Live wiki reads per prompt would
    # drift while another batch source writes pages and break conversation-cache
    # hashes on resume. The index lets analysis recognize/update existing
    # synthesis/thesis pages instead of seeing only untyped bare stems.
    snapshot_update: dict = {}
    slugs_snapshot = (
        None if checkpoint_invalidated
        else (progress or {}).get("slugs_snapshot_2_2")
    )
    if not isinstance(slugs_snapshot, list):
        slugs_snapshot = sorted(list_existing_slugs(config))
        snapshot_update["slugs_snapshot_2_2"] = slugs_snapshot
    wiki_index_snapshot = (
        None if checkpoint_invalidated
        else (progress or {}).get("wiki_index_snapshot_2_2")
    )
    if not isinstance(wiki_index_snapshot, str):
        wiki_index_snapshot = load_wiki_index_context(config)
        snapshot_update["wiki_index_snapshot_2_2"] = wiki_index_snapshot
    if snapshot_update:
        save_progress(config, _h, snapshot_update)

    chunk_analyses, accumulated_digest = _analyze_all_chunks(
        chunk_meta, global_digest, accumulated_digest, raw_file, config,
        template_content, chunk_total, t_start, verbose,
        existing_slugs=slugs_snapshot,
        wiki_index_context=wiki_index_snapshot)

    for analysis, planned in zip(chunk_analyses, chunk_plan["chunks"]):
        analysis["_chunk_index"] = planned["index"]
        analysis["_chunk_id"] = planned["chunk_id"]
        analysis["_chunk_text_sha256"] = planned["text_sha256"]
    _verify_stage_2_2_chunks(
        chunk_analyses, extracted_text, chunk_plan=chunk_plan)

    # Persist 2.2 on its own + mark stage_2_2_done so a prefetch (analyze_only)
    # can stop here and the later spine run restores chunk_analyses without
    # re-analyzing. 2.2 is snapshot-stable after entry, so the frozen analysis
    # is safe to cache before 2.3+ performs fresh live wiki reads.
    # Roll the final accumulated_digest up into global_digest (dict) for
    # Stage 2.4. Persist so a cached resume restores it.
    global_digest = _parse_accumulated_to_dict(accumulated_digest)

    # Verify the Stage 2.2 roll-up has the five keys Stage 2.4 consumes.
    if chunk_analyses:
        _verify_stage_2_2_digest(global_digest, raw_file)

    save_progress(config, _h, {"chunk_plan_v2": chunk_plan,
                               "chunk_analyses": chunk_analyses,
                               "global_digest": global_digest})
    bind_chunk_plan(config, _h, chunk_plan)
    mark_stage_done(config, _h, "stage_2_2_done")
    if analyze_only:
        raise PrepareStopAfter("1.5")

    result = _generate_from_analyses(
        chunk_analyses, extracted_text, global_digest, raw_file, config,
        template_content, verbose, chunk_meta=chunk_meta)
    return (*result, global_digest)


def _build_chunk_meta(extracted_text: str, config: Config):
    """Deterministic chunk plan: ``(chunk_meta, chunk_total)``.

    Chunking is pure (same text + config \u2192 same chunks), so the prefetch-resume
    path rebuilds it cheaply instead of persisting every chunk's text.
    """
    chunks = _stage_2_2_chunk_text(extracted_text, config.target_chars, config.chunk_overlap,
                                   target_tokens=config.target_tokens)
    chunk_total = len(chunks)
    chunk_meta: list[tuple[int, str, str, str]] = []
    spans = _resolve_chunk_spans(
        extracted_text,
        [(i, chunk, "", "") for i, chunk in enumerate(chunks)],
        config.chunk_overlap,
    )
    for i in range(chunk_total):
        chunk = chunks[i]
        overlap_before = chunks[i - 1][-config.chunk_overlap:] if i > 0 else ""
        chunk_pos, chunk_end = spans[i]
        heading_path = _stage_2_2_resolve_chunk_heading_path(
            extracted_text, chunk_pos, chunk_end)
        chunk_meta.append((i, chunk, overlap_before, heading_path))
    return chunk_meta, chunk_total


def _generate_from_analyses(
    chunk_analyses: list, extracted_text: str, global_digest: dict, raw_file: Path,
    config: Config, template_content: str, verbose: bool,
    chunk_meta=None,
) -> tuple[list, dict, list, dict]:
    """Stage 2.3 \u2192 2.4: the wiki-DEPENDENT tail of the chunk pipeline.

    Runs only in the serial spine (one book at a time), so Stage 2.3's
    ``config.wiki_dir`` reads see pages written by previously-finalized books.
    ``chunk_meta`` is reused from the fresh path when available, else rebuilt
    deterministically (prefetch-resume).
    """
    if chunk_meta is None:
        chunk_meta, chunk_total = _build_chunk_meta(extracted_text, config)
    else:
        chunk_total = len(chunk_meta)
    t_start = time.time()

    # \u2500\u2500 Stage 2.3: incremental association detection (existing-wiki overlap) \u2500\u2500
    from _stage_2_3_incremental import (
        stage_2_3_detect_incremental_associations,
        stage_2_3_resolve_proposed_connections,
    )
    schema_text = load_schema_md(config)
    incremental_associations = stage_2_3_detect_incremental_associations(
        config.wiki_dir, chunk_analyses, schema_text=schema_text)
    if incremental_associations:
        print(f"  [stage 2.3] {len(incremental_associations)} candidate(s) "
              f"match existing wiki pages \u2192 fed into generation prompt")
    else:
        print("  [stage 2.3] No existing-wiki associations (first source or no overlap)")

    related_pages = stage_2_3_resolve_proposed_connections(
        config.wiki_dir, chunk_analyses, schema_text=schema_text)
    if related_pages:
        print(f"  [stage 2.3] {len(related_pages)} proposed connection(s) to "
              f"existing wiki resolved \u2192 fed into generation prompt")

    # \u2500\u2500 Stage 2.4: one whole-source generation (NashSU 0.6.6 order) \u2500\u2500
    _stage_begin("Stage 2.4: Consolidated Generation")
    # All serial analyses are complete before this call. The shared context
    # carries the final rolling digest, every chunk analysis, and bounded raw
    # evidence from every chunk. Stage 2.3 associations are an explicit
    # improved-wiki extension inserted before the single generation call.
    all_file_blocks, _generated_slugs, _gen_stop_reason = _generate_all_chunks(
        chunk_meta, chunk_analyses, incremental_associations, raw_file, config,
        template_content, chunk_total, t_start, verbose,
        related_pages=related_pages,
        global_digest=global_digest,
    )

    # Build combined analysis
    unique_concepts, _ = _stage_2_4_extract_names(chunk_analyses)
    concept_blocks = [b for b in all_file_blocks if "concepts/" in b[0]]
    entity_blocks = [b for b in all_file_blocks if "entities/" in b[0]]
    analysis = {
        "book_meta": global_digest.get("book_meta", {}),
        "outline": global_digest.get("outline", []),
        "concepts_identified": len(unique_concepts),
        "concepts_generated": len(concept_blocks),
        "entities_generated": len(entity_blocks),
        "coverage_pct": round(len(concept_blocks) / max(len(unique_concepts), 1), 2),
        "total_chunks": chunk_total,
        "method": "analyze-all\u2192associate-extension\u2192generate-once",
    }
    file_blocks = all_file_blocks

    _verify_stage_2_2_chunks(chunk_analyses, extracted_text)
    return chunk_analyses, analysis, file_blocks, incremental_associations
