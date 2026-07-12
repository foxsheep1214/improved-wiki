"""_ingest_chunks.py — chunk analysis pipeline 2.2→2.4 (extracted from ingest.py)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from _core import (
    Config,
    ConversationPending,
    stage_begin as _stage_begin,
    file_sha256,
    is_stage_done,
    mark_stage_done,
    unmark_stage_done,
    save_progress,
    list_existing_slugs,
    slugify,
    PrepareStopAfter,
)
from _stage_2_base import file_block_slug
from _stage_2_analyze import (
    _stage_2_1_chunk_text,
    _stage_2_2_analyze_chunk,
    _stage_2_2_chunk_retries,
    _stage_2_2_resolve_chunk_heading_path,
)
from _stage_2_4_generation import (
    stage_2_4_generate_chunk,
    stage_2_4_generate_all,
    _stage_2_4_extract_names,
    _stage_2_4_per_concept_fallback,
)
from _stage_validators import _verify_stage_2_2_chunks, _verify_stage_2_1_digest

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

def _build_gen_inventory(chunk_meta: list, chunk_analyses: list) -> dict[str, int]:
    """Eager slug→owner-chunk inventory for the parallel-gen path.

    Every concept/entity slug is DETERMINISTIC: ``slug = slugify(name)`` where
    ``name`` is taken from the (cached, stable) chunk analyses. So the canonical
    slug of every page is computable BEFORE generation. Returns a flat
    ``slug_stem -> owner_chunk_index`` map where the owner is the FIRST chunk
    (in chunk order) that lists that name. Concepts and entities share one flat
    map, matching how the serial loop mixes both kinds of stems into
    ``generated_slugs``. Blank names are skipped.
    """
    inventory: dict[str, int] = {}
    for meta, analysis in zip(chunk_meta, chunk_analyses):
        i = meta[0]
        for key in ("concepts_found", "entities_found"):
            for item in analysis.get(key, []):
                name = item.get("name", "")
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
    """Eager-inventory + drain variant of the >1-chunk generation loop.

    The serial path forces order by feeding each chunk the slugs PRODUCED by
    prior chunks. Here every concept/entity slug is computed up front from the
    cached analyses (``_build_gen_inventory``), so each chunk can be told to
    skip+link the concepts owned by OTHER chunks independent of execution order
    (``_other_chunk_slugs``, sorted → stable cache key).

    Drain: in conversation mode an uncached prompt raises ``ConversationPending``
    AFTER writing its prompt .md. We catch it per chunk and CONTINUE so a single
    pipeline invocation emits ALL uncached chunk prompts for parallel answering,
    then raise ``ConversationPending`` once at the end. On the final all-cached
    replay no chunk raises, so we return ``(blocks, slug_union, None)`` exactly
    like the serial path's contract.
    """
    inventory = _build_gen_inventory(chunk_meta, chunk_analyses)
    all_file_blocks: list = []
    generated_slugs: list = []
    pending = 0
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
            continue
        all_file_blocks.extend(blocks)
        for path, _ in blocks:
            slug = file_block_slug(path)
            if slug not in generated_slugs:
                generated_slugs.append(slug)
        print(f"  [generate] {i + 1}/{chunk_total} [parallel-eager, grounded]")

    if pending > 0:
        print(f"  [generate] emitted {pending} chunk prompt(s) for "
              f"parallel answering")
        raise ConversationPending()

    print(f"  [generate] {chunk_total}/{chunk_total} parallel-eager grounded done "
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
    # Cached: chunk analysis already complete. Stage-completion is the single
    # source of truth in stages.json (stage_2_3_done); chunk_analyses presence
    # in the artifact store guards against a missing artifact.
    _h = file_sha256(raw_file)
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
            _verify_stage_2_2_chunks(chunk_analyses, extracted_text)
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
        _verify_stage_2_2_chunks(chunk_analyses, extracted_text)
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
    chunk_meta, chunk_total = _build_chunk_meta(extracted_text, config)
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

    save_progress(config, _h, {"chunk_analyses": chunk_analyses,
                               "global_digest": global_digest})
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
    for i in range(chunk_total):
        chunk = chunks[i]
        overlap_before = chunks[i - 1][-config.chunk_overlap:] if i > 0 else ""
        chunk_pos = extracted_text.find(chunk)
        if chunk_pos == -1:
            chunk_pos = i * config.target_chars
        heading_path = _stage_2_2_resolve_chunk_heading_path(
            extracted_text, chunk_pos, chunk_pos + len(chunk))
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
