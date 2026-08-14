"""_ingest_prepare.py — Stage 0-2 synthesis / source-page prep (extracted from ingest.py)."""
from __future__ import annotations

import time
from pathlib import Path

from _config import Config
from _core import (
    PrepareStopAfter,
    canonical_source_path,
    detect_template_type,
    load_template,
    set_current_file as _set_current_file,
    is_query_bridge_source,
)
from _progress import (
    file_sha256,
    load_progress,
    save_progress,
    delete_progress_keys,
    is_stage_done,
    mark_stage_done,
    unmark_stage_done,
)
from _parse import parse_file_blocks
from _stage_1_extract import (
    stage_1_1_extract_text,
    stage_1_2_extract_images,
    stage_1_3_caption_images,
)
from _stage_1_3_caption import _stage_1_3_inline_captions
from _stage_1_2_images import validate_stage_1_2_artifact
from _stage_1_3_caption import validate_stage_1_3_artifact
from _stage_2_4_generation import (
    _source_bibliographic_fields,
    source_page_rel_stem,
)
from _source_page import (
    _normalize_source_frontmatter,
    _validate_source_file_block,
    build_fallback_source_summary,
    source_analysis_text,
)
from _stage_validators import (
    verify_stage_0,
    StageValidationError,
    _verify_stage_2_4_file_blocks,
)
from _ingest_skip import _stage_0_2_should_skip, _stop_after_stage
from _ingest_chunks import (
    GENERATION_POLICY_VERSION,
    _build_chunk_meta,
    _run_chunk_pipeline,
)
from _stage_2_context import build_consolidated_stage_2_context
from normalize_raw_names import stage_0_1_check_file
from _task_manifest import ensure_task_manifest

def _stage_2_2_only_requested(config: Config, prefetch_only: bool) -> bool:
    """Whether prepare must stop before wiki-dependent Stage 2.3/2.4."""
    return prefetch_only or _stop_after_stage(config, "1.5")


def _count_comparison_blocks(file_blocks: list[tuple[str, str]]) -> int:
    """Count comparison pages emitted by the shared Stage 2.4 lifecycle."""
    count = 0
    for path, _content in file_blocks:
        normalized = str(path)
        if normalized.startswith("wiki/"):
            normalized = normalized[len("wiki/"):]
        if normalized.startswith("comparisons/"):
            count += 1
    return count


def _ensure_source_page(
    global_digest: dict, raw_file: Path, config: Config,
    file_blocks: list,
    chunk_analyses: list[dict] | None = None,
) -> tuple[list, bool]:
    """Guarantee the source page exists in ``file_blocks`` — no second LLM call.

    NashSU 0.6.6 parity: Stage 2.4's single generation call
    is asked for the source page along with the key/schema-typed pages
    (``_source_page_output_section``). This function only checks the result and,
    when the model omitted or malformed the block, writes the SAME deterministic
    fallback NashSU uses (ingest.ts:1287) from the complete Stage 2 analysis.

    Returns ``(file_blocks, source_page_missing)``. The flag is True when the
    generation did not supply a usable source block and the deterministic
    summary stood in, so the caller refuses to cache the run (NashSU
    ingest.ts:1326-1341).
    """
    source_rel_stem = source_page_rel_stem(raw_file, config)
    source_identity = canonical_source_path(raw_file, config)
    expected = f"sources/{source_rel_stem}.md"

    def _is_source_block(path: str) -> bool:
        norm = path[len("wiki/"):] if path.startswith("wiki/") else path
        return norm == expected

    generated = [(p, c) for p, c in file_blocks
                 if _is_source_block(p) and c.strip()]
    if generated:
        # Structural gate + frontmatter repair prevent malformed blocks and
        # fill blank bibliographic fields from the digest before write.
        path, content = generated[0]
        stem = source_rel_stem
        bib = _source_bibliographic_fields(
            global_digest if isinstance(global_digest, dict) else {}, stem)
        candidate = _normalize_source_frontmatter(
            f"---FILE:wiki/{expected}---\n{content}\n---END FILE---",
            bib["authors"], bib["year"], bib["url"], bib["venue"])
        try:
            _validate_source_file_block(candidate, stem)
        except RuntimeError as exc:
            print(f"  [stage 2.4] Generated source block rejected ({exc}) — "
                  "using the deterministic fallback")
        else:
            repaired = parse_file_blocks(candidate)
            if repaired:
                others = [(p, c) for p, c in file_blocks
                          if not _is_source_block(p)]
                return repaired[:1] + others, False

    print(
        f"  [stage 2.4] Source page absent from generation — writing the "
        f"NashSU deterministic fallback for {expected}"
    )
    fallback = build_fallback_source_summary(
        source_rel_stem,
        source_identity,
        source_analysis_text(
            global_digest,
            chunk_analyses=chunk_analyses,
        ),
        time.strftime("%Y-%m-%d"),
    )
    source_blocks = parse_file_blocks(fallback)
    if not source_blocks:
        raise RuntimeError(
            "Stage 2.4 deterministic source fallback could not be parsed; "
            "refusing to continue without a source page."
        )
    # Drop any malformed same-path block the model emitted before prepending.
    kept = [(p, c) for p, c in file_blocks if not _is_source_block(p)]
    return source_blocks + kept, True


def _do_prepare(
    raw_file: Path, config: Config,
    template_override: str | None = None,
    verbose: bool = False,
    prefetch_only: bool = False,
) -> dict | None:
    """Stage 0-2 for one book.

    Two segments with different cross-book safety:
      - **Snapshot-stable (0–2.2)** — Stage 2.2 freezes read-only wiki
        slug/index context once at entry, then reads only the book and that
        snapshot and writes no wiki/ state. Safe to run for several books in
        parallel ("prefetch"). ``prefetch_only=True`` runs exactly this segment
        then raises ``PrepareStopAfter("1.5")`` at the Stage 2.2/2.3 boundary.
      - **Wiki-dependent (2.3–2.4)** — Stage 2.3 reads ``config.wiki_dir`` to
        link/dedup against existing pages; Stage 2.4 builds on that. MUST run in the
        serial spine (one book at a time) so each book sees prior books' written
        pages. ``prefetch_only=False`` (default) runs the full segment, reusing
        cached 2.2.

    Returns the prepared dict for Stage 3+, or None on skip/failure.
    """
    _set_current_file(raw_file.name)
    print(f"\n=== [prepare] {raw_file.name} ===")
    try:
        # ── Stage 0.1: raw naming gate. Raises on violations or when the
        # project has no naming rules — rename/draft first, then re-run.
        _naming_errors = stage_0_1_check_file(raw_file, config.wiki_root)
        if _naming_errors:
            raise RuntimeError(
                f"[Stage 0.1] {raw_file.name} 违反 raw 命名规范: "
                + "；".join(_naming_errors)
                + " — 先重命名（normalize_raw_names.py --fix）再 ingest。")

        # Bind this run to one source identity + pipeline contract before any
        # cache or marker is allowed to influence control flow.
        h = file_sha256(raw_file)
        ensure_task_manifest(raw_file, config)

        # Dedup check — skip only if the ingest is truly complete (the
        # ``ingested`` completion marker is set); otherwise resume or re-ingest.
        if _stage_0_2_should_skip(raw_file, config):
            return None

        progress = load_progress(config, h)

        # ── Issue 1 fix (cross-pipeline cache reuse, 2026-06-25) ──
        # A prior llm-wiki-local run may have cached a *pymupdf* extraction under
        # the same file hash. The new pipeline requires minerU for PDFs (it
        # produces _manifest.json + image media + VLM captions); a pymupdf cache
        # hit silently skips minerU, leaving no manifest, no captions, and a
        # zero-file media directory (504 images → 0). Detect a legacy/non-minerU
        # cached method for a PDF and discard the stale extraction cache so minerU
        # re-runs. (plain-text/zipfile methods are for non-PDF inputs and stay.)
        _MINERU_METHODS = ("mineru-api",)
        if raw_file.suffix.lower() == ".pdf" and progress:
            _cm = progress.get("extract_method", "")
            if _cm and not _cm.startswith(_MINERU_METHODS):
                print(f"  [extract] ⚠️ Cached extraction method '{_cm}' is legacy "
                      f"(pre-minerU) — invalidating extraction/image/caption cache "
                      f"and re-running minerU")
                # save_progress is a MERGE-write: popping keys from the local
                # dict then saving it back can never delete a persisted key.
                # delete_progress_keys does a locked load→del→write (真删除).
                _stale = [k for k in ("extracted_text", "extract_method",
                                      "stage_1_2", "stage_1_3") if k in progress]
                for _k in _stale:
                    progress.pop(_k, None)
                if _stale:
                    delete_progress_keys(config, h, _stale)
                for _stage in ("stage_1_1_done", "stage_1_2_done"):
                    if is_stage_done(config, h, _stage):
                        unmark_stage_done(config, h, _stage)
        # If Stages 3.2–3.4 already completed, skip the entire Phase 2 pipeline.
        # Re-running Stage 2.4 generation would
        # cache-miss every resume because the generation prompt hash drifts
        # with wiki state (pages written/rewritten), looping forever before
        # _do_write can be reached. _do_write reconstructs the bound disk pages
        # and runs the remaining Stages 3.5–3.7. A legacy
        # checkpoint that predates review_prepared reconstructs the review
        # input from those bound pages once; new checkpoints restore the
        # already validated pre-write review items.
        if is_stage_done(config, h, "write_phase"):
            print("  [prepare] write_phase marker present — skipping 2.x prepare")
            extracted_text = (progress or {}).get("extracted_text", "")
            method = (progress or {}).get("extract_method", "cached")
            stage_1_2_result = (progress or {}).get("stage_1_2", {"count": 0})
            stage_1_3_result = (progress or {}).get("stage_1_3", {"captioned": 0})
            global_digest = (progress or {}).get("global_digest", {})
            template_name = detect_template_type(raw_file, config.raw_root, template_override)
            return {
                "raw_file": raw_file, "config": config, "h": h, "method": method,
                "extracted_text": extracted_text, "global_digest": global_digest,
                "chunk_analyses": [], "analysis": {},
                "file_blocks": [], "stage_1_2_result": stage_1_2_result,
                "stage_1_3_result": stage_1_3_result, "template_name": template_name,
                "comp_count": 0,
                "concept_merge_stats": (0, 0), "dedup_was_run": False,
                "incremental_associations": {},
                "enrich_enabled": False,
            }

        # Stage 0: Text extraction
        if progress and "extracted_text" in progress:
            extracted_text = progress["extracted_text"]
            method = progress.get("extract_method", "cached")
            print(f"  [extract] (cached) {method}: {len(extracted_text)} chars")
        else:
            extracted_text, method = stage_1_1_extract_text(raw_file, config)
            print(f"  [extract] {method}: {len(extracted_text)} chars")

            # Stage 0 Validation (Phase 2: per-stage verification)
            if not verify_stage_0(extracted_text):
                print("  [validate] ❌ Stage 0 failed: text extraction insufficient")
                raise StageValidationError("Stage 0: text extraction failed")

            save_progress(config, h, {
                "extracted_text": extracted_text,
                "extract_method": method,
            })
            mark_stage_done(config, h, "stage_1_1_done")

        # Template
        template_name = detect_template_type(raw_file, config.raw_root, template_override)
        template_content = load_template(template_name)
        print(f"  [template] {template_name}")

        # ── Stage 1.2 + 1.3 image pipeline (1.2 → 1.3 sequential) ──
        # Helper: run 1.2→1.3 together (1.3 depends on 1.2 output)
        def _run_image_pipeline():
            stage_1_2_result: dict = {"count": 0}
            stage_1_2_cached = False
            if progress and "stage_1_2" in progress:
                valid, reason, normalized = validate_stage_1_2_artifact(
                    progress["stage_1_2"], config, raw_file)
                if valid:
                    stage_1_2_result = normalized
                    stage_1_2_cached = True
                    print(
                        f"  [stage 1.2] (cached+verified) "
                        f"{stage_1_2_result.get('count', 0)} images")
                else:
                    print(
                        "  [stage 1.2] ⚠️ cached media artifact invalid "
                        f"({reason}) — rebuilding Stage 1.2/1.3 only")
                    progress.pop("stage_1_2", None)
                    progress.pop("stage_1_3", None)
                    delete_progress_keys(
                        config, h, ["stage_1_2", "stage_1_3"])
                    unmark_stage_done(config, h, "stage_1_2_done")
                    unmark_stage_done(config, h, "stage_1_3_done")

            if not stage_1_2_cached and method.startswith("mineru"):
                # method is "mineru-api" for all PDFs (extraction quality gate
                # removed 2026-07-08; all minerU runs produce images on disk).
                ocr_out = config.extract_tmp_dir / raw_file.stem
                if not ocr_out.exists():
                    # No silent count-0: a mineru extraction with no OCR output
                    # dir means extract_tmp_dir was cleaned — caching {count: 0}
                    # here would permanently record the whole book as image-free.
                    raise RuntimeError(
                        f"[Stage 1.2] minerU OCR output missing: {ocr_out} — "
                        f"extract_tmp_dir was likely cleaned after extraction. "
                        f"Re-run extraction (clear the cached extracted_text/"
                        f"extract_method for {raw_file.name}) instead of "
                        f"silently recording 0 images.")
                # Surviving per-chunk indexes are evidence that the source is
                # not image-free. If minerU's transient byte tree and canonical
                # media were cleaned, rebuild locally instead of accepting a
                # new empty manifest for an unfinished ingest.
                from _media_integrity import (
                    mineru_figure_names,
                    restore_or_reharvest_mineru_media,
                )
                # Limit the manifest to the current configured chunk layout.
                # The durable OCR directory may retain old 50-page chunks from
                # a prior run alongside today's 32-page chunks; merging them
                # turns stale history into a fictitious media-loss condition.
                from _stage_1_1_scanned import MINERU_CHUNK_SIZE
                try:
                    import fitz
                    with fitz.open(raw_file) as _doc:
                        _active_chunk_keys = {
                            f"{start}-{min(start + MINERU_CHUNK_SIZE, len(_doc))}"
                            for start in range(0, len(_doc), MINERU_CHUNK_SIZE)
                        }
                except Exception as exc:
                    raise RuntimeError(
                        f"[Stage 1.2] cannot determine current minerU chunk "
                        f"layout for {raw_file.name}: {exc}") from exc
                expected_names = mineru_figure_names(
                    ocr_out, active_chunk_keys=_active_chunk_keys)
                expected_media = len(expected_names)
                stage_1_2_result, authoritative_count = (
                    restore_or_reharvest_mineru_media(
                        raw_file,
                        config,
                        ocr_out,
                        expected_hint=expected_media,
                        expected_names=expected_names,
                    )
                )
                valid, reason, stage_1_2_result = (
                    validate_stage_1_2_artifact(
                        stage_1_2_result,
                        config,
                        raw_file,
                        expected_count=authoritative_count,
                    )
                )
                if not valid:
                    raise RuntimeError(
                        f"[Stage 1.2] rebuilt media artifact invalid: {reason}")
                # Save progress immediately after 1.2 completes
                save_progress(config, h, {"stage_1_2": stage_1_2_result})
                mark_stage_done(config, h, "stage_1_2_done")
            elif not stage_1_2_cached and raw_file.suffix.lower() in (".pptx", ".docx"):
                # Covers "zipfile-pptx", "zipfile-docx". PDFs no longer reach
                # here since 2026-06-23: all PDF extraction routes through
                # minerU (pipeline or VLM) and is handled by the branch above.
                # stage_1_2_extract_images() branches on file suffix internally.
                stage_1_2_result = stage_1_2_extract_images(raw_file, config)
                save_progress(config, h, {"stage_1_2": stage_1_2_result})
                mark_stage_done(config, h, "stage_1_2_done")
            elif not stage_1_2_cached and raw_file.suffix.lower() in (".md", ".markdown"):
                # .md sources (method="plain-text"): extract local images referenced
                # via ![[ref]] / ![alt](ref) — NashSU extractAndSaveMarkdownImages parity.
                stage_1_2_result = stage_1_2_extract_images(raw_file, config)
                save_progress(config, h, {"stage_1_2": stage_1_2_result})
                mark_stage_done(config, h, "stage_1_2_done")

            # Stage 1.3: Caption extracted images (runs if 1.2 found images)
            stage_1_3_result = {"captioned": 0}
            stage_1_3_cached = False
            if progress and "stage_1_3" in progress:
                valid, reason, actual = validate_stage_1_3_artifact(
                    stage_1_2_result, config)
                if valid:
                    stage_1_3_result = dict(progress["stage_1_3"])
                    stage_1_3_result.update(actual)
                    stage_1_3_cached = True
                    print(
                        f"  [stage 1.3] (cached+verified) "
                        f"{actual.get('complete', 0)}/{actual.get('total', 0)} "
                        "captions complete")
                else:
                    print(
                        "  [stage 1.3] ⚠️ cached caption artifact invalid "
                        f"({reason}) — resuming pending captions")
                    progress.pop("stage_1_3", None)
                    delete_progress_keys(config, h, ["stage_1_3"])
                    unmark_stage_done(config, h, "stage_1_3_done")

            needs_caption = (
                not stage_1_3_cached
                and stage_1_2_result.get("count", 0) > 0
            )
            if needs_caption:
                stage_1_3_result = stage_1_3_caption_images(config, stage_1_2_result)

            valid, reason, actual = validate_stage_1_3_artifact(
                stage_1_2_result, config)
            if not valid:
                raise RuntimeError(
                    f"[Stage 1.3] caption artifact validation failed: {reason}")
            stage_1_3_result.update(actual)

            return stage_1_2_result, stage_1_3_result

        # Stage 1.2→1.3 image pipeline. The document digest rolls up inside
        # Stage 2.2 from an empty seed via each chunk's updated_global_digest.
        stop_after_0 = _stop_after_stage(config, "0")

        stage_1_2_result, stage_1_3_result = _run_image_pipeline()

        # Persist the VERIFIED artifact view, even on a cache hit. The media
        # directory + manifest/caption files, not stale counters, are
        # authoritative.
        save_progress(config, h, {
            "stage_1_2": stage_1_2_result,
            "stage_1_3": stage_1_3_result,
        })
        mark_stage_done(config, h, "stage_1_2_done")
        mark_stage_done(config, h, "stage_1_3_done")

        if stop_after_0:
            print("\n[stop-after-stage] Stage 0 complete — "
                  "clean exit (--stop-after-stage=0)")
            raise PrepareStopAfter("0")

        # Stage 2.2 starts from an empty digest and returns the final roll-up
        # consumed by Stage 2.4.
        global_digest = {}

        # Stage 1.3 → 2 inline (NashSU ingest.ts Step 0.6 parity): rewrite
        # ![](images/...) refs to carry their VLM caption as alt text, so the
        # Stage 2.2/2.4 generation LLM sees figure semantics instead of
        # empty-alt refs it would silently paraphrase away. Runs AFTER Stage
        # 1.3 (captions exist) and BEFORE the chunk pipeline.
        _media_dir = stage_1_2_result.get("media_dir")
        if _media_dir and stage_1_2_result.get("count", 0) > 0:
            _inlined = _stage_1_3_inline_captions(extracted_text, config, Path(_media_dir))
            if _inlined != extracted_text:
                extracted_text = _inlined
                save_progress(config, h, {"extracted_text": extracted_text})
                print(f"  [caption] Inlined VLM captions as alt text into "
                      f"extracted_text ({len(extracted_text):,} chars)")

        # Stage 2.2 → 2.3 → 2.4 chunk pipeline. Both batch prefetch and an
        # explicit single-book ``--stop-after-stage=1.5`` stop at the
        # snapshot-stable 2.2/2.3 boundary. Previously only ``prefetch_only``
        # was forwarded, so the single-book flag was accepted by argparse but
        # silently ran into 2.3/2.4 and emitted generation handoffs.
        analyze_only = _stage_2_2_only_requested(config, prefetch_only)
        chunk_analyses, analysis, file_blocks, incremental_associations, global_digest = _run_chunk_pipeline(
            extracted_text, global_digest, raw_file, config, template_content,
            progress, verbose, analyze_only=analyze_only)

        # Persist 2.2/2.4 results + mark stage_2_3_done. Without this, a
        # mid-flight resume (e.g. an enrich conversation handoff) re-enters
        # _run_chunk_pipeline, misses the cached skip, and re-runs Stage 2.4
        # generation — whose prompt hash drifts with wiki state, so it cache-
        # misses every resume and loops before _do_write is reached. Merge-write
        # means only the new artifact keys are needed here.
        if not is_stage_done(config, h, "stage_2_3_done"):
            save_progress(config, h, {
                "chunk_analyses": chunk_analyses,
                "analysis": analysis,
                "incremental_associations": incremental_associations,
                "generation_policy_version": GENERATION_POLICY_VERSION,
                # Persist file_blocks so a stage_2_3_done cache-resume restores
                # them DIRECTLY (it is the authoritative artifact). The retired
                # raw_response could not be re-parsed into FILE blocks, so a
                # resume that relied on it lost every concept/entity block
                # (2026-06-25). Saved BEFORE the marker below so a crash in
                # between never leaves "done" without its artifact.
                "file_blocks": file_blocks,
            })
            mark_stage_done(config, h, "stage_2_3_done")

        # --stop-after-stage 2 = "key/schema-typed page generation only": halt
        # before the 2.4 closing/source-page tail. stage_2_3_done is set so a
        # re-run caches the chunk pipeline and resumes at the tail. ("2.0" is
        # the same boundary.)
        if _stop_after_stage(config, "2") or _stop_after_stage(config, "2.0"):
            print("\n[stop-after-stage] Stage 2 complete — "
                  "clean exit (--stop-after-stage=2)")
            raise PrepareStopAfter("2")

        # ── Generation tail: Stage 2.4 closing dedup + source-page gate ──
        # Cached as ONE segment under legacy marker name ``stage_2_9_done`` for
        # resume compatibility. NashSU 0.6.6 has no dedicated comparison stage:
        # comparison and synthesis pages are schema-typed Stage 2.4 FILE blocks.
        # On cache hit, restore the tail outputs from the artifact store.
        #
        # Same guard as the 2.3 cache path: this segment must have persisted a
        # ``file_blocks`` artifact (it always does — see save_progress below).
        # If the marker is set but the artifact is missing (old/partial cache),
        # honoring it would skip the closing/source-page tail with whatever
        # file_blocks happens to be in scope — dropping the source page.
        # Invalidate and re-run the tail instead.
        # True only when an unrecovered truncation cost us the real source page
        # and a deterministic summary stood in. A cached tail replays a run that
        # already made this call, so it stays False there.
        _source_page_truncated = False
        _tail_cached = (is_stage_done(config, h, "stage_2_9_done")
                        and (progress or {}).get("file_blocks") is not None)
        if is_stage_done(config, h, "stage_2_9_done") and not _tail_cached:
            print("  [generation tail] ⚠️  legacy stage_2_9_done marker set "
                  "but no persisted "
                  "file_blocks artifact — invalidating marker and re-running "
                  "the closing/source-page tail (prevents silent page loss).")
            unmark_stage_done(config, h, "stage_2_9_done")
        if _tail_cached:
            _pcache = progress or {}
            file_blocks = _pcache.get("file_blocks", file_blocks)
            comp_count = _count_comparison_blocks(file_blocks)
            concept_count_before, concept_count_after = _pcache.get(
                "concept_merge_stats", (0, 0))
            dedup_was_run = _pcache.get("dedup_was_run", False)
            print(f"  [generation tail] (cached) outputs restored — "
                  f"{len(file_blocks)} blocks")
        else:
            # Stage 2.4 closing sub-step: in-source concept dedup and merge for
            # multi-chunk books. Runs before the source page so the index lists
            # de-duplicated concepts.
            from _dedup_intra_source import dedup_intra_source
            dedup_result = dedup_intra_source(file_blocks, chunk_analyses, config)
            file_blocks = dedup_result["file_blocks"]
            dedup_was_run = dedup_result["dedup_was_run"]
            concept_count_before = dedup_result["concept_count_before"]
            concept_count_after = dedup_result["concept_count_after"]

            # Rebuild the exact deterministic whole-source context used by
            # Stage 2.4: final rolling digest + every chunk analysis + bounded
            # raw evidence from every chunk. It is intentionally not persisted
            # as a duplicate large cache artifact.
            _stage_2_chunk_meta, _ = _build_chunk_meta(
                extracted_text,
                config,
            )
            _stage_2_context = build_consolidated_stage_2_context(
                global_digest,
                chunk_analyses,
                _stage_2_chunk_meta,
                config.source_budget,
            )

            # Stage 2.4 source-page gate — skipped for an explicitly ingested
            # query page: the page itself is already the canonical artifact.
            # This is a manual/historical compatibility path; v0.6.7 Deep
            # Research does not auto-ingest its result.
            _is_query_bridge = is_query_bridge_source(raw_file, config)
            if _is_query_bridge:
                print("  [stage 2.4] Source page skipped (explicit query-page "
                      "ingest — the query page IS the canonical artifact)")
            else:
                file_blocks, _source_page_truncated = _ensure_source_page(
                    global_digest, raw_file, config, file_blocks,
                    chunk_analyses=chunk_analyses,
                )
            _verify_stage_2_4_file_blocks(file_blocks, raw_file,
                                          is_query_bridge=_is_query_bridge)

            # Backward-compatible statistic. Comparison pages now arrive in the
            # shared Stage 2.4 FILE-block set; there is no separate count cap or
            # forced source-page backlink section.
            comp_count = _count_comparison_blocks(file_blocks)

            # Persist tail outputs, then set the legacy segment marker.
            save_progress(config, h, {
                "file_blocks": file_blocks,
                "comp_count": comp_count,
                "concept_merge_stats": (concept_count_before, concept_count_after),
                "dedup_was_run": dedup_was_run,
            })
            mark_stage_done(config, h, "stage_2_9_done")

        analysis["__source_hash"] = h
        analysis["__extract_method"] = method

        print(f"  [prepare] ✅ done — {len(file_blocks)} blocks")
        return {
            "raw_file": raw_file, "config": config,
            "h": h, "method": method, "extracted_text": extracted_text,
            "global_digest": global_digest, "chunk_analyses": chunk_analyses,
            "analysis": analysis,
            "file_blocks": file_blocks,
            "stage_1_2_result": stage_1_2_result,
            "stage_1_3_result": stage_1_3_result,
            "template_name": template_name,
            "comp_count": comp_count,
            "concept_merge_stats": (concept_count_before, concept_count_after),
            "dedup_was_run": dedup_was_run,
            "incremental_associations": incremental_associations,
            "enrich_enabled": getattr(config, "enrich_enabled", True),
            "source_page_truncated": _source_page_truncated,
        }
    except Exception as e:
        print(f"  [prepare] ❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
        raise
