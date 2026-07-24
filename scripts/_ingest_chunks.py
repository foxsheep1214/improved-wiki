"""_ingest_chunks.py — chunk analysis pipeline 2.2→2.4 (extracted from ingest.py)."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from _config import Config
from _core import (
    BATCH_MAX_CONCURRENT,
    ConversationPending,
    stage_begin as _stage_begin,
    slugify,
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
    load_schema_md,
    schema_candidate_routes,
)
from _stage_2_base import file_block_slug
from _stage_2_analyze import (
    ChunkAnalysisValidationError,
    _stage_2_1_chunk_text,
    _stage_2_2_analyze_chunk,
    _stage_2_2_chunk_retries,
    _stage_2_2_resolve_chunk_heading_path,
    normalize_and_validate_chunk_analysis,
)
from _stage_2_4_generation import (
    stage_2_4_generate_chunk,
    stage_2_4_generate_all,
    _stage_2_4_extract_names,
    _stage_2_4_per_concept_fallback,
)
from _stage_validators import _verify_stage_2_2_chunks, _verify_stage_2_1_digest
from _task_manifest import bind_chunk_plan

CHUNK_PLAN_SCHEMA_VERSION = 2
CHUNKER_VERSION = "token-bounded-heading-aware-v2"

_STAGE_2_2_DOWNSTREAM_MARKERS = (
    "stage_2_2_done",
    "stage_2_3_done",
    "stage_2_9_done",
    "write_loop_done",
    "write_phase",
    "ingested",
)

_STAGE_2_2_DOWNSTREAM_ARTIFACTS = (
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


def _assert_chunk_count_alignment(chunk_meta: list, chunk_analyses: list) -> None:
    """Prevent ``zip`` from silently truncating generation input."""
    if len(chunk_meta) != len(chunk_analyses):
        raise RuntimeError(
            "[Stage 2.4] Chunk plan/analysis cardinality mismatch: "
            f"{len(chunk_meta)} planned chunks vs "
            f"{len(chunk_analyses)} analyses. Stage 2.2 must be re-run."
        )


def _parse_accumulated_to_dict(accumulated) -> dict:
    """Parse the rolled-up accumulated_digest back to a dict for 2.4/2.6/2.9.

    2.2's per-chunk updated_global_digest refines accumulated_digest across
    chunks (NashSU rolling-digest parity). 2.4/2.6/2.9 consume the
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
) -> list:
    """Stage 2.2: analyze all chunks, serially.

    Serial preserves cross-chunk ``accumulated_digest`` refinement \u2014 each
    chunk's analysis is informed by the previous chunk's updated digest.
    Conversation mode is the only text-gen path, so there is no parallel
    branch: every call is a manual round-trip, which is inherently serial.
    Returns chunk_analyses indexed by chunk order.

    ``existing_slugs`` is this book's persisted 2.2 snapshot (see
    _run_chunk_pipeline) so every chunk prompt is built from the SAME frozen
    slug list \u2014 wiki-independent, prompt-hash stable across resumes.
    """
    chunk_analyses: list = []

    for i, chunk, overlap_before, heading_path in chunk_meta:
        ca = _stage_2_2_analyze_chunk(
            chunk, i, chunk_total, global_digest, accumulated_digest,
            overlap_before, heading_path, raw_file, config, template_content,
            max_retries=_stage_2_2_chunk_retries(), verbose=verbose,
            existing_slugs=existing_slugs)
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

def _build_gen_inventory(
    chunk_meta: list,
    chunk_analyses: list,
    schema_text: str = "",
) -> dict[str, int]:
    """Eager slug→owner-chunk inventory for the parallel-gen path.

    Every concept/entity/schema-candidate slug is DETERMINISTIC:
    ``slug = slugify(name)`` where ``name`` is taken from the (cached, stable)
    chunk analyses. So the canonical slug of every page is computable BEFORE
    generation. Returns a flat ``slug_stem -> owner_chunk_index`` map. An
    eligible schema-specific candidate takes precedence over a generic
    concept/entity with the same stem; within each tier the FIRST chunk wins.
    All page types share one flat map, matching how the serial loop mixes
    produced FILE stems into ``generated_slugs``. Schema candidates are
    included only when their type is an eligible route in the authoritative
    Page Types table. Blank names are skipped.
    """
    _assert_chunk_count_alignment(chunk_meta, chunk_analyses)
    candidate_routes = schema_candidate_routes(schema_text)
    inventory: dict[str, int] = {}

    # Schema-specific types win over the generic concept/entity buckets even
    # when the generic mention occurs in an earlier chunk. This mirrors the
    # generation contract ("prefer the more specific declared type") and keeps
    # one subject from becoming both concepts/foo and findings/foo.
    for meta, analysis in zip(chunk_meta, chunk_analyses):
        i = meta[0]
        if not isinstance(analysis, dict):
            continue
        candidates = analysis.get("schema_typed_candidates", [])
        if not isinstance(candidates, list):
            raise RuntimeError(
                "[Stage 2.4] Unvalidated Stage 2.2 field "
                "schema_typed_candidates: "
                f"{type(candidates).__name__}. Re-run Stage 2.2.")
        for item in candidates:
            if not isinstance(item, dict):
                raise RuntimeError(
                    "[Stage 2.4] Unvalidated Stage 2.2 schema candidate: "
                    f"{type(item).__name__}. Re-run Stage 2.2.")
            name = item.get("name", "")
            candidate_type = item.get("type", "")
            if (
                not isinstance(name, str)
                or not isinstance(candidate_type, str)
                or candidate_type not in candidate_routes
            ):
                continue
            stem = slugify(name)
            if stem and stem not in inventory:  # first candidate chunk owns
                inventory[stem] = i

    for meta, analysis in zip(chunk_meta, chunk_analyses):
        i = meta[0]
        if not isinstance(analysis, dict):
            continue
        for key in ("concepts_found", "entities_found"):
            items = analysis.get(key, [])
            if not isinstance(items, list):
                raise RuntimeError(
                    f"[Stage 2.4] Unvalidated Stage 2.2 field {key}: "
                    f"{type(items).__name__}. Re-run Stage 2.2.")
            for item in items:
                if not isinstance(item, dict):
                    raise RuntimeError(
                        "[Stage 2.4] Unvalidated Stage 2.2 inventory item: "
                        f"{type(item).__name__}. Re-run Stage 2.2.")
                name = item.get("name", "")
                if not isinstance(name, str):
                    continue
                if not name or not name.strip():
                    continue
                stem = slugify(name)
                if not stem:
                    continue
                if stem not in inventory:  # first chunk owns
                    inventory[stem] = i
    return inventory


def _other_chunk_slugs(inventory: dict[str, int], chunk_idx: int) -> list[str]:
    """Sorted list of inventory stems owned by chunks OTHER than ``chunk_idx``.

    Order-independent + sorted → deterministic prompt text → stable cache key
    across re-invokes, regardless of execution order.
    """
    return sorted(stem for stem, owner in inventory.items() if owner != chunk_idx)


def _generate_all_chunks(
    chunk_meta: list, chunk_analyses: list, existing_refs: dict,
    raw_file: Path, config: Config, template_content: str,
    chunk_total: int, t_start: float, verbose: bool,
    related_pages: list[dict] | None = None,
) -> tuple[list, list, str | None]:
    """Stage 2.4 generation, source-grounded for full-concept fidelity (P1, 2026-06-27).

    - 1 chunk  → one grounded single-shot call (the whole source fits one prompt).
    - >1 chunk → per-chunk generation, each prompt carrying THAT chunk's raw text,
      so EVERY concept is generated with its exact source passage present.

    A book small enough to fit one generation prompt is also a single chunk, so
    this is single-shot-equivalent for normal books and full-fidelity for large
    ones — solving the huge-book "concepts drift to training-memory" failure that
    a budget-trimmed prefix could not. improved-wiki ingest is not token-sensitive,
    so the extra per-chunk calls on big books are an accepted cost. ``existing_refs``
    + ``related_pages`` (Stage 2.3) are threaded so cross-chunk wikilinks resolve.
    """
    _assert_chunk_count_alignment(chunk_meta, chunk_analyses)
    if len(chunk_meta) <= 1:
        source_context = chunk_meta[0][1] if chunk_meta else ""
        all_file_blocks, generated_slugs, stop_reason = stage_2_4_generate_all(
            chunk_analyses, raw_file, config, template_content,
            verbose=verbose, existing_refs=existing_refs,
            related_pages=related_pages, source_context=source_context,
        )
        print(f"  [generate] 1/1 [single-shot, grounded, {time.time() - t_start:.0f}s]")
        return all_file_blocks, generated_slugs, stop_reason

    if _parallel_gen_enabled():
        return _generate_all_chunks_parallel(
            chunk_meta, chunk_analyses, existing_refs, raw_file, config,
            template_content, chunk_total, t_start, verbose,
            related_pages=related_pages)

    all_file_blocks: list = []
    generated_slugs: list = []
    for meta, analysis in zip(chunk_meta, chunk_analyses):
        i, chunk_text = meta[0], meta[1]
        blocks = stage_2_4_generate_chunk(
            analysis, i, generated_slugs, raw_file, config, template_content,
            verbose=verbose, chunk_text=chunk_text,
            existing_refs=existing_refs, related_pages=related_pages,
        )
        all_file_blocks.extend(blocks)
        for path, _ in blocks:
            slug = file_block_slug(path)
            if slug not in generated_slugs:
                generated_slugs.append(slug)
        print(f"  [generate] {i + 1}/{chunk_total} [per-chunk, grounded]")
    print(f"  [generate] {chunk_total}/{chunk_total} per-chunk grounded done "
          f"[{time.time() - t_start:.0f}s]")
    # No single stop_reason for the per-chunk path. Since 2026-07-12 a failed
    # chunk RAISES inside stage_2_4_generate_chunk (no []-sentinel), so there
    # is no silent partial-failure gap here; the caller's per-concept fallback
    # remains for the legitimate zero-block outcome and single-shot truncation.
    return all_file_blocks, generated_slugs, None


def _parallel_gen_enabled() -> bool:
    """DEFAULT ON (2026-07-09 user decision): eager-inventory + drain mode for
    Stage 2.4 multi-chunk generation.

    Cross-chunk dedup in 2.4 is a lightweight REFERENCE dependency (a
    deterministic slug list — ``slug = slugify(name)`` from already-cached 2.2
    analyses), not a CONTENT dependency like Stage 2.2's rolling digest (chunk
    N+1 genuinely needs chunk N's digest output to build its own prompt). NashSU
    itself never chunks generation at all (one call for the whole book) — it has
    no "must be serial" requirement to mirror here; improved-wiki's own per-chunk
    grounding (avoiding "concepts drift to training-memory" on large books) is
    what makes chunking necessary, and that grounding is per-chunk-source-text,
    unaffected by answer order. Serial was the original default purely out of
    launch-day conservatism ("byte-identical to before"), not a real constraint.

    Explicit opt-OUT via ``IMPROVED_WIKI_PARALLEL_GEN=0``/``false``/``no``/``off``
    (e.g. bisecting a regression) restores the old strictly-serial accumulation
    path. Unset or any other value = parallel-safe drain mode.
    """
    val = os.environ.get("IMPROVED_WIKI_PARALLEL_GEN", "").strip().lower()
    return val not in ("0", "false", "no", "off")


def _generate_all_chunks_parallel(
    chunk_meta: list, chunk_analyses: list, existing_refs: dict,
    raw_file: Path, config: Config, template_content: str,
    chunk_total: int, t_start: float, verbose: bool,
    related_pages: list[dict] | None = None,
) -> tuple[list, list, str | None]:
    """Eager-inventory + bounded-drain variant of >1-chunk generation.

    The serial path forces order by feeding each chunk the slugs PRODUCED by
    prior chunks. Here every concept/entity/schema-candidate slug is computed
    up front from the cached analyses (``_build_gen_inventory``), so each chunk
    can be told to skip+link the pages owned by OTHER chunks independent of
    execution order (``_other_chunk_slugs``, sorted → stable cache key).

    Bounded drain: in conversation mode an uncached prompt raises
    ``ConversationPending`` AFTER writing its prompt .md. We catch it per chunk
    and continue until the configured parallel handoff ceiling is reached.
    Thus ``--parallel 4`` advances a 10-chunk book in ``4 + 4 + 2`` prompt
    waves; it does not serialize Stage 2.4. Cached answers are drained for free,
    and a ceiling at least as large as the chunk count retains the original
    all-at-once behavior.
    """
    _assert_chunk_count_alignment(chunk_meta, chunk_analyses)
    # Some programmatic callers/tests intentionally provide a minimal config
    # carrying only the handoff limit. Treat that as a project with no schema;
    # a real Config always has both path attributes.
    schema_text = (
        load_schema_md(config)
        if hasattr(config, "wiki_root") and hasattr(config, "wiki_dir")
        else ""
    )
    inventory = _build_gen_inventory(
        chunk_meta,
        chunk_analyses,
        schema_text,
    )
    all_file_blocks: list = []
    generated_slugs: list = []
    pending = 0
    try:
        handoff_limit = max(
            1,
            int(getattr(
                config,
                "handoff_parallel_limit",
                BATCH_MAX_CONCURRENT,
            )),
        )
    except (TypeError, ValueError, OverflowError):
        handoff_limit = BATCH_MAX_CONCURRENT
    for meta, analysis in zip(chunk_meta, chunk_analyses):
        i, chunk_text = meta[0], meta[1]
        other_slugs = _other_chunk_slugs(inventory, i)
        try:
            blocks = stage_2_4_generate_chunk(
                analysis, i, other_slugs, raw_file, config, template_content,
                verbose=verbose, chunk_text=chunk_text,
                existing_refs=existing_refs, related_pages=related_pages,
            )
        except ConversationPending:
            # Prompt .md already written; defer this chunk's answer.
            pending += 1
            if pending >= handoff_limit:
                break
            continue
        all_file_blocks.extend(blocks)
        for path, _ in blocks:
            slug = file_block_slug(path)
            if slug not in generated_slugs:
                generated_slugs.append(slug)
        print(f"  [generate] {i + 1}/{chunk_total} [parallel-wave, grounded]")

    if pending > 0:
        print(f"  [generate] emitted {pending} chunk prompt(s) for a "
              f"parallel wave (limit {handoff_limit})")
        raise ConversationPending()

    print(f"  [generate] {chunk_total}/{chunk_total} parallel-wave grounded done "
          f"[{time.time() - t_start:.0f}s]")
    return all_file_blocks, generated_slugs, None

def _run_chunk_pipeline(
    extracted_text: str, global_digest: dict, raw_file: Path, config: Config,
    template_content: str, progress: dict | None, verbose: bool,
    analyze_only: bool = False,
) -> tuple[list, dict, list, dict]:
    """Stage 2.2 \u2192 2.3 \u2192 2.4: analyze all chunks, detect existing-wiki
    associations, then generate pages with associations fed into each prompt.

    Split (2026-06-21): analysis and generation are separate phases so Stage 2.3
    (incremental association detection) can run between them and feed back into
    the generation prompt. Returns
    ``(chunk_analyses, analysis, file_blocks, incremental_associations)``.

    ``analyze_only`` (prefetch boundary, 2026-06-28): Stage 2.2 (chunk analysis)
    is wiki-independent \u2014 it reads only the book's own text/digest. Stage 2.3 is
    the first stage that reads ``config.wiki_dir``. In batch mode the next book's
    2.2 may be prefetched in parallel while the current book holds the serial
    wiki-write spine; ``analyze_only=True`` runs/caches 2.2 then raises
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
    # Once any incompatible cache has been invalidated, bind the current plan
    # before a cached restore or a new Stage 2.2 marker can be accepted.
    bind_chunk_plan(config, _h, chunk_plan)

    # Cached: chunk analysis already complete. Stage-completion is the single
    # source of truth in stages.json (stage_2_3_done); chunk_analyses presence
    # in the artifact store guards against a missing artifact.
    if (progress and "chunk_analyses" in progress
            and is_stage_done(config, _h, "stage_2_3_done")):
        # Restore file_blocks DIRECTLY from the artifact store. The retired
        # design re-parsed ``raw_response`` (= "\n".join(block BODIES), bodies
        # without the ---FILE:...--- wrappers), so parse_file_blocks() returned
        # [] and silently dropped every concept/entity page on resume.
        #
        # The ``file_blocks`` key being PRESENT (even as []) is an authoritative
        # restore: [] is the legitimate "every concept already overlaps an
        # existing wiki page" outcome. The key being ABSENT means an old/partial
        # cache that predates file_blocks persistence \u2014 there is no safe way to
        # recover it, so rather than proceed with 0 blocks (re-introducing the
        # silent loss) we invalidate the stage marker and fall through to
        # re-run the chunk pipeline.
        persisted_blocks = progress.get("file_blocks")
        if persisted_blocks is None:
            print("  [stage 2.2] \u26a0\ufe0f  stage_2_3_done set but no persisted "
                  "file_blocks artifact \u2014 invalidating marker and re-running "
                  "chunk pipeline (prevents silent concept/entity loss).")
            unmark_stage_done(config, _h, "stage_2_3_done")
        else:
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
        if analyze_only:
            raise PrepareStopAfter("1.5")
        # Restore the persisted roll-up digest. A pre-roll-up cache (no valid
        # persisted global_digest) would silently feed an empty digest to
        # 2.4/2.6/2.9 — same pattern as the stage_2_3_done restore above:
        # warn, invalidate the marker, and fall through to re-run 2.2.
        _digest_cached = progress.get("global_digest")
        _digest_keys = {"book_meta", "outline", "key_concepts", "key_claims", "key_entities"}
        if not isinstance(_digest_cached, dict) or not _digest_keys.issubset(_digest_cached):
            print("  [stage 2.2] ⚠️  stage_2_2_done set but no valid rolled-up "
                  "global_digest persisted (pre-roll-up cache?) — invalidating "
                  "marker and re-running chunk analysis (prevents an empty "
                  "digest reaching 2.4/2.6/2.9).")
            unmark_stage_done(config, _h, "stage_2_2_done")
        else:
            global_digest = _digest_cached
            result = _generate_from_analyses(
                chunk_analyses, extracted_text, global_digest, raw_file, config,
                template_content, verbose)
            return (*result, global_digest)

    # \u2500\u2500 Stage 2.2: build chunk plan + analyze all chunks (wiki-independent) \u2500\u2500
    est_sec = chunk_total * 75
    print(f"  [stage 2.2] Analyze \u2014 {chunk_total} chunk(s), "
          f"target {config.target_chars:,} chars/chunk (est. {est_sec/60:.0f} min)")
    _stage_begin("Stage 2.2: Chunk Analysis")
    t_start = time.time()
    # 2.1 removed (NashSU parity, 2026-07-08): accumulated_digest starts
    # empty — the global digest rolls up across chunks via each chunk's
    # updated_global_digest. No whole-book prior.
    accumulated_digest = ""

    # Existing-slugs SNAPSHOT (2026-07-12): freeze the wiki slug list ONCE per
    # book on first entry into 2.2 and persist it, so every chunk prompt (and
    # every resume) is built from the same list. A live list_existing_slugs()
    # read per prompt violated 2.2's wiki-independent contract: in batch mode
    # a parallel book's wiki writes drifted the prompt hash → conversation
    # cache misses on every resume.
    slugs_snapshot = (progress or {}).get("slugs_snapshot_2_2")
    if slugs_snapshot is None:
        slugs_snapshot = sorted(list_existing_slugs(config))
        save_progress(config, _h, {"slugs_snapshot_2_2": slugs_snapshot})

    chunk_analyses, accumulated_digest = _analyze_all_chunks(
        chunk_meta, global_digest, accumulated_digest, raw_file, config,
        template_content, chunk_total, t_start, verbose,
        existing_slugs=slugs_snapshot)

    for analysis, planned in zip(chunk_analyses, chunk_plan["chunks"]):
        analysis["_chunk_index"] = planned["index"]
        analysis["_chunk_id"] = planned["chunk_id"]
        analysis["_chunk_text_sha256"] = planned["text_sha256"]
    _verify_stage_2_2_chunks(
        chunk_analyses, extracted_text, chunk_plan=chunk_plan)

    # Persist 2.2 on its own + mark stage_2_2_done so a prefetch (analyze_only)
    # can stop here and the later spine run restores chunk_analyses without
    # re-analyzing. 2.2 is wiki-independent \u2014 safe to cache before 2.3+ runs.
    # Roll the final accumulated_digest up into global_digest (dict) for
    # 2.4/2.6/2.9. Persist so a cached resume restores it.
    global_digest = _parse_accumulated_to_dict(accumulated_digest)

    # Verify the rolled-up digest has the 5 required keys (book_meta/outline/
    # key_concepts/key_claims/key_entities) that 2.4/2.6/2.9 consume.
    # Migrated from Stage 2.1 (removed 2026-07-08): the gate now runs on the
    # 2.2 roll-up instead of the former whole-book prior.
    if chunk_analyses and not analyze_only:
        _verify_stage_2_1_digest(global_digest, raw_file)

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
    chunks = _stage_2_1_chunk_text(extracted_text, config.target_chars, config.chunk_overlap,
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
    incremental_associations = stage_2_3_detect_incremental_associations(
        config.wiki_dir, chunk_analyses)
    if incremental_associations:
        print(f"  [stage 2.3] {len(incremental_associations)} new concept(s) "
              f"match existing wiki pages \u2192 fed into generation prompt")
    else:
        print(f"  [stage 2.3] No existing-wiki associations (first source or no overlap)")

    related_pages = stage_2_3_resolve_proposed_connections(config.wiki_dir, chunk_analyses)
    if related_pages:
        print(f"  [stage 2.3] {len(related_pages)} proposed connection(s) to "
              f"existing wiki resolved \u2192 fed into generation prompt")

    # \u2500\u2500 Stage 2.4: generate all chunks (associations fed into prompt) \u2500\u2500
    _stage_begin("Stage 2.4: Chunk Generation")
    # Generation is always source-grounded (P1): _generate_all_chunks feeds each
    # chunk's raw text into its prompt. Every concept is generated with its exact
    # source passage — full fidelity at any book size, no on/off switch (ingest is
    # not token-sensitive). See _generate_all_chunks / _stage_2_4_build_prompt.
    all_file_blocks, generated_slugs, gen_stop_reason = _generate_all_chunks(
        chunk_meta, chunk_analyses, incremental_associations, raw_file, config,
        template_content, chunk_total, t_start, verbose,
        related_pages=related_pages)

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
        "method": "analyze\u2192associate\u2192generate",
    }
    file_blocks = all_file_blocks

    # \u2500\u2500 Fallback: per-concept generation \u2500\u2500
    # 0 blocks generated is the CORRECT outcome (not a failure) when every
    # concept in this source already overlaps an existing wiki page \u2014
    # Stage 2.4 is instructed to skip and wikilink those (see existing_refs
    # in _stage_2_4_build_prompt). Only fall back for concepts genuinely
    # missing from the wiki; otherwise a conversation-mode replay that
    # resumes past Stage 3.1 (write) re-derives chunk_analyses, sees its own
    # already-written pages as "existing", correctly emits 0 blocks, and
    # then this fallback would burn 20+ wasted LLM calls re-generating pages
    # that are already on disk (confirmed live on the Plett BMS Vol.2 ingest).
    # Fallback triggers on EITHER (a) zero concept blocks, OR (b) single-shot
    # output truncation (stop_reason=length): a PARTIAL generation (e.g. 30 of
    # 50 pages emitted then cut off) would otherwise silently lose the missing
    # 20. pre_existing_slugs=generated_slugs makes the fallback backfill only the
    # gap, so its blocks MERGE with (not replace) the good single-shot blocks.
    # Resume replays end with end_turn (not length) and emit 0 blocks against
    # already-on-disk pages \u2014 caught only by branch (a)+truly_missing, never by
    # (b), so resume is never re-generated (see resume note above). (P0, 2026-06-27)
    _sr = str(gen_stop_reason or "").lower()
    truncated = ("length" in _sr) or ("max_tok" in _sr) or ("max_output" in _sr)
    truly_missing = [n for n in unique_concepts if n not in incremental_associations]
    if truly_missing and chunk_analyses and (not concept_blocks or truncated):
        n_missed = len(truly_missing)
        reason = "output truncated" if truncated else f"0/{n_missed} concepts generated"
        print(f"  [stage 2.4] \u26a0\ufe0f  {reason} "
              f"\u2014 per-concept fallback to backfill missing "
              f"(pre_existing_slugs={len(generated_slugs)})")
        _fa_analysis, _fa_raw, fa_blocks = _stage_2_4_per_concept_fallback(
            chunk_analyses, global_digest, raw_file, config,
            template_content, verbose=verbose,
            pre_existing_slugs=generated_slugs,
        )
        fa_concept_entity = [(p, c) for p, c in fa_blocks
                             if not p.startswith("sources/")]
        # Merge, don't replace: on partial truncation all_file_blocks already
        # holds the pages single-shot emitted; the fallback (pre_existing_slugs
        # dedup) only adds the gap pages. In the zero-block case all_file_blocks
        # is empty, so this is equivalent to the old assignment.
        all_file_blocks = all_file_blocks + fa_concept_entity
        file_blocks = all_file_blocks
        concept_blocks = [b for b in all_file_blocks if "concepts/" in b[0]]
        entity_blocks = [b for b in all_file_blocks if "entities/" in b[0]]
        analysis["concepts_generated"] = len(concept_blocks)
        analysis["entities_generated"] = len(entity_blocks)
        analysis["coverage_pct"] = round(
            len(concept_blocks) / max(len(unique_concepts), 1), 2)
        analysis["method"] = "analyze\u2192associate\u2192generate+fallback"
        for path, _ in fa_concept_entity:
            s = file_block_slug(path)
            if s not in generated_slugs:
                generated_slugs.append(s)

    _verify_stage_2_2_chunks(chunk_analyses, extracted_text)
    return chunk_analyses, analysis, file_blocks, incremental_associations
