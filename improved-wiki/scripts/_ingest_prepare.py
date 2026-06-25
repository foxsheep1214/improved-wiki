"""_ingest_prepare.py — Stage 0-2 synthesis / source-page prep (extracted from ingest.py)."""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from _core import (
    Config,
    PrepareStopAfter,
    detect_domain as _detect_domain,
    detect_template_type,
    load_template,
    file_sha256,
    load_progress,
    save_progress,
    parse_file_blocks,
    set_current_file as _set_current_file,
    is_stage_done,
    mark_stage_done,
    unmark_stage_done,
    list_existing_slugs,
)
from _stage_1_extract import (
    stage_1_1_extract_text,
    stage_1_2_extract_images,
    _stage_1_2_extract_from_mineru,
    stage_1_3_caption_images,
    _stage_1_1_check_text_quality,
)
from _stage_1_3_caption import _stage_1_3_inline_captions
from _stage_2_analyze import stage_2_1_global_digest
from _stage_2_6_source_page import stage_2_6_source_page
from _stage_2_7_query_generation import stage_2_7_query_generation
from _stage_2_9_comparison import stage_2_9_comparison_generation
from _stage_validators import (
    verify_stage_0,
    StageValidationError,
    _verify_stage_1_1_text,
    _verify_stage_2_1_digest,
    _verify_stage_2_4_file_blocks,
)
from _ingest_skip import _stage_0_2_should_skip, _stop_after_stage
from _ingest_chunks import _run_chunk_pipeline

def _prepare_source_page(
    global_digest: dict, raw_file: Path, config: Config,
    template_content: str, progress: dict | None, file_blocks: list,
    verbose: bool,
) -> list:
    """Stage 2.6: generate the source page (dedicated LLM call) and merge into file_blocks."""
    current_domain = _detect_domain(raw_file, template_content, global_digest)
    if progress and "source_page_response" in progress:
        source_page_response = progress["source_page_response"]
        print(f"  [stage 2.6] (cached) Source page already generated")
    else:
        # Issue 2 fix: build the linkable-slug set (concepts/entities generated
        # this ingest + existing wiki slugs) so the source page cannot wikilink
        # to an ALREADY-COVERED concept's never-written own slug.
        _linkable: list[str] = []
        for _path, _ in file_blocks:
            _stem = str(_path)
            # normalize "wiki/concepts/foo.md" → "concepts/foo"
            if _stem.startswith("wiki/"):
                _stem = _stem[len("wiki/"):]
            if _stem.endswith(".md"):
                _stem = _stem[:-3]
            _linkable.append(_stem)
        _linkable.extend(list_existing_slugs(config))
        source_page_response, _ = stage_2_6_source_page(
            global_digest, raw_file, config,
            template=template_content, current_domain=current_domain, verbose=verbose,
            linkable_slugs=_linkable,
        )

    if not source_page_response:
        return file_blocks

    source_blocks = parse_file_blocks(source_page_response)
    if source_blocks:
        file_blocks = source_blocks + list(file_blocks)
        print(f"  [stage 2.6] Source page block merged ({len(file_blocks)} total)")
        return file_blocks

    # LLM didn't use FILE block format — generate placeholder
    source_rel = f"sources/{raw_file.relative_to(config.raw_root).with_suffix('.md')}"
    book_meta = global_digest.get("book_meta", {})
    title = book_meta.get("title", raw_file.stem) if isinstance(book_meta, dict) else raw_file.stem
    stub = f"---\ntype: source\ntitle: \"{title}\"\ndomain: general\n"
    stub += f"created: {time.strftime('%Y-%m-%d')}\nupdated: {time.strftime('%Y-%m-%d')}\n"
    stub += f"tags: []\nrelated: []\nsources: [\"raw/{raw_file.relative_to(config.raw_root)}\"]\n---\n\n"
    stub += f"**Title:** {title}\n**Author:** {raw_file.stem}\n\n"
    stub += f"## Global Digest\n\n```yaml\n{json.dumps(global_digest, ensure_ascii=False, indent=2)[:4000]}\n```\n\n"
    stub += f"## Key Concepts\n\n"
    for path, _ in file_blocks:
        if "concepts/" in path:
            stub += f"- [[{Path(path).stem}]]\n"
    file_blocks.append((source_rel, stub))
    print(f"  [stage 2.6] Placeholder source page generated ({len(file_blocks)} total)")
    return file_blocks

def _do_prepare(
    raw_file: Path, config: Config,
    template_override: str | None = None,
    verbose: bool = False,
) -> dict | None:
    """Stage 0-2 for one book.  Read-only: no shared state writes, no lock needed.

    Returns a dict with all data needed for Stage 3+, or None on skip/failure.
    Suitable for parallel execution across multiple books.
    """
    _set_current_file(raw_file.name)
    print(f"\n=== [prepare] {raw_file.name} ===")
    try:
        # Dedup check — skip only if the ingest is truly complete (stage_4_1
        # marker set) or the source page exists and is reasonably complete.
        if _stage_0_2_should_skip(raw_file, config):
            return None

        h = file_sha256(raw_file)
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
                _invalidated = False
                for _k in ("extracted_text", "extract_method", "stage_1_2", "stage_1_3"):
                    if _k in progress:
                        progress.pop(_k, None)
                        _invalidated = True
                for _stage in ("stage_1_1_done", "stage_1_2_done"):
                    if is_stage_done(config, h, _stage):
                        unmark_stage_done(config, h, _stage)
                        _invalidated = True
                if _invalidated:
                    save_progress(config, h, progress)
        # ── write_phase short-circuit (Bug 2 fix, 2026-06-25) ──
        # If the Stage 3.1-3.3 write phase already completed in a prior run,
        # skip the entire 2.x pipeline. Re-running Stage 2.4 generation would
        # cache-miss every resume because the generation prompt hash drifts
        # with wiki state (pages written/rewritten), looping forever before
        # _do_write can be reached. _do_write handles write_phase_done by
        # setting _write_blocks=[] and skipping 3.1/3.2/3.3, then runs
        # 3.4-4.1 over the on-disk wiki. chunk_analyses/analysis are not
        # needed post-write (3.4+ scan the wiki dir, not file_blocks).
        if is_stage_done(config, h, "write_phase"):
            print("  [prepare] write_phase marker present — skipping 2.x prepare")
            extracted_text = (progress or {}).get("extracted_text", "")
            method = (progress or {}).get("extract_method", "cached")
            stage_1_2_result = (progress or {}).get("stage_1_2", {"count": 0})
            stage_1_3_result = (progress or {}).get("stage_1_3", {"captioned": 0})
            global_digest = (progress or {}).get("global_digest", {})
            template_name = detect_template_type(raw_file, config.raw_root, template_override)
            current_domain = _detect_domain(raw_file, load_template(template_name), global_digest)
            return {
                "raw_file": raw_file, "config": config, "h": h, "method": method,
                "extracted_text": extracted_text, "global_digest": global_digest,
                "chunk_analyses": [], "analysis": {},
                "file_blocks": [], "stage_1_2_result": stage_1_2_result,
                "stage_1_3_result": stage_1_3_result, "template_name": template_name,
                "query_count": 0, "comp_count": 0,
                "concept_merge_stats": (0, 0), "dedup_was_run": False,
                "current_domain": current_domain, "incremental_associations": {},
                "query_resolutions": {}, "enrich_enabled": False,
            }

        # Stage 0: Text extraction
        if progress and "extracted_text" in progress:
            extracted_text = progress["extracted_text"]
            method = progress.get("extract_method", "cached")
            print(f"  [extract] (cached) {method}: {len(extracted_text)} chars")
        else:
            extracted_text, method = stage_1_1_extract_text(raw_file, config)
            print(f"  [extract] {method}: {len(extracted_text)} chars")
            _verify_stage_1_1_text(raw_file, extracted_text, method)

            # Stage 0 Validation (Phase 2: per-stage verification)
            if not verify_stage_0(extracted_text):
                print(f"  [validate] ❌ Stage 0 failed: text extraction insufficient")
                raise StageValidationError("Stage 0: text extraction failed")

            qr = _stage_1_1_check_text_quality(extracted_text, raw_file.name)
            if qr["status"] == "severe":
                print(f"  [extract] ❌ Text quality SEVERE — aborting ingest. "
                      f"Re-run with a different PDF or re-download from source.")
                return None
            save_progress(config, h, {
                "extracted_text": extracted_text,
                "extract_method": method,
            })
            mark_stage_done(config, h, "stage_1_1_done")

        # Template
        template_name = detect_template_type(raw_file, config.raw_root, template_override)
        template_content = load_template(template_name)
        print(f"  [template] {template_name}")

        # ── Stage 1.2 + 1.3 pipeline (1.2 → 1.3 sequential) ∥ Stage 2.1 (parallel) ──
        # Helper: run 1.2→1.3 together (1.3 depends on 1.2 output)
        def _run_image_pipeline():
            stage_1_2_result: dict = {"count": 0}
            if progress and "stage_1_2" in progress:
                stage_1_2_result = progress["stage_1_2"]
                print(f"  [stage 1.2] (cached) {stage_1_2_result.get('count', 0)} images")
            elif method.startswith("mineru"):
                # Covers "mineru", "mineru-local-ocr", "mineru-local-ocr-low-quality"
                # (low-quality OCR still ran and produced images on disk —
                # excluding it here silently dropped every image from those
                # sources even though minerU already extracted them).
                ocr_out = config.extract_tmp_dir / raw_file.stem
                if ocr_out.exists():
                    stage_1_2_result = _stage_1_2_extract_from_mineru(ocr_out, config, raw_file)
                # Save progress immediately after 1.2 completes
                save_progress(config, h, {"stage_1_2": stage_1_2_result})
                mark_stage_done(config, h, "stage_1_2_done")
            elif raw_file.suffix.lower() in (".pptx", ".docx"):
                # Covers "zipfile-pptx", "zipfile-docx". PDFs no longer reach
                # here since 2026-06-23: all PDF extraction routes through
                # minerU (pipeline or VLM) and is handled by the branch above.
                # stage_1_2_extract_images() branches on file suffix internally.
                stage_1_2_result = stage_1_2_extract_images(raw_file, config)

            # Stage 1.3: Caption extracted images (runs if 1.2 found images)
            stage_1_3_result = {"captioned": 0}
            needs_caption = (not progress or "stage_1_3" not in progress) and stage_1_2_result.get("count", 0) > 0
            if needs_caption:
                stage_1_3_result = stage_1_3_caption_images(config, stage_1_2_result)
            elif progress and "stage_1_3" in progress:
                stage_1_3_result = progress["stage_1_3"]
                # `captioned` is NEW captions written in the prior run (0 when
                # all were already captioned). Report it as "new" so a cached
                # re-ingest doesn't misleadingly print "0 captions" when every
                # image actually has a .caption.txt on disk.
                print(f"  [stage 1.3] (cached) {stage_1_3_result.get('total', 0)} images, "
                      f"{stage_1_3_result.get('captioned', 0)} new captions")

            return stage_1_2_result, stage_1_3_result

        # Parallel execution: 1.2→1.3 pipeline vs 2.1 global digest
        needs_digest = not is_stage_done(config, h, "stage_2_1_done")

        # --stop-after-stage 0 = "text+image extract only": do NOT enter 2.1.
        # The digest future is gated so 2.1 never starts; the stop is raised
        # below once 1.2/1.3 are persisted. (On a re-run without the flag,
        # needs_digest stays True and 2.1 runs normally — stage_1_x_done
        # markers make 1.x cached.)
        stop_after_0 = _stop_after_stage(config, "0")

        with ThreadPoolExecutor(max_workers=2) as executor:
            fut_images = executor.submit(_run_image_pipeline)
            fut_digest = None if (stop_after_0 or not needs_digest) else executor.submit(
                stage_2_1_global_digest, extracted_text, raw_file, config,
                template_content, verbose=verbose)

            stage_1_2_result, stage_1_3_result = fut_images.result()

            # Persist Stage 1.2/1.3 immediately, before awaiting the digest future.
            # fut_digest.result() below can raise ConversationPending (conversation-
            # mode cache miss), which propagates out of this function before a
            # later save_progress call would ever be reached — every subsequent
            # conversation-mode round-trip would otherwise re-run
            # _run_image_pipeline() from scratch for this source, forever.
            if not progress or "stage_1_2" not in progress:
                save_progress(config, h, {
                    "stage_1_2": stage_1_2_result,
                    "stage_1_3": stage_1_3_result,
                })
                mark_stage_done(config, h, "stage_1_3_done")

            if stop_after_0:
                print(f"\n[stop-after-stage] Stage 0 complete — "
                      f"clean exit (--stop-after-stage=0)")
                raise PrepareStopAfter("0")

            global_digest = fut_digest.result() if fut_digest else progress.get("global_digest", {})

        if needs_digest:
            _verify_stage_2_1_digest(global_digest, raw_file)
            # Mark 2.1 done + persist global_digest so a resume in the 2.2/2.4
            # window (e.g. chunk-analysis handoff) skips 2.1 via needs_digest.
            # Conditional on needs_digest so a later resume (stage already
            # stage_2_3_done from Fix A) does not regress the marker.
            save_progress(config, h, {"global_digest": global_digest})
            mark_stage_done(config, h, "stage_2_1_done")
        else:
            print(f"  [stage 2.1] (cached) Global Digest — {len(global_digest)} keys")
            _verify_stage_2_1_digest(global_digest, raw_file)

        # --stop-after-stage 1 = "global digest only": halt before the chunk
        # pipeline. stage_2_1_done is set so a re-run without the flag caches
        # 2.1 and proceeds to 2.2.
        if _stop_after_stage(config, "1"):
            print(f"\n[stop-after-stage] Stage 1 complete — "
                  f"clean exit (--stop-after-stage=1)")
            raise PrepareStopAfter("1")

        # Stage 1.3 → 2 inline (NashSU ingest.ts Step 0.6 parity): rewrite
        # ![](images/...) refs to carry their VLM caption as alt text, so the
        # Stage 2.2/2.4 generation LLM sees figure semantics instead of
        # empty-alt refs it would silently paraphrase away. Runs AFTER Stage
        # 1.3 (captions exist) and BEFORE the chunk pipeline. Stage 2.1 ran in
        # parallel without this — acceptable, 2.1 is structural not figure-level.
        _media_dir = stage_1_2_result.get("media_dir")
        if _media_dir and stage_1_2_result.get("count", 0) > 0:
            _inlined = _stage_1_3_inline_captions(extracted_text, config, Path(_media_dir))
            if _inlined != extracted_text:
                extracted_text = _inlined
                save_progress(config, h, {"extracted_text": extracted_text})
                print(f"  [caption] Inlined VLM captions as alt text into "
                      f"extracted_text ({len(extracted_text):,} chars)")

        # Stage 2.2 + 2.4: Chunk Analysis → Generation (barrier-free pipeline)
        chunk_analyses, analysis, file_blocks, incremental_associations = _run_chunk_pipeline(
            extracted_text, global_digest, raw_file, config, template_content,
            progress, verbose)

        # Persist 2.2/2.4 results + mark stage_2_3_done. Without this, a
        # mid-flight resume (e.g. 3.3 enrich conversation handoff) re-enters
        # _run_chunk_pipeline, misses the cached skip, and re-runs Stage 2.4
        # generation — whose prompt hash drifts with wiki state, so it cache-
        # misses every resume and loops before _do_write is reached. Merge-write
        # means only the new artifact keys are needed here.
        if not is_stage_done(config, h, "stage_2_3_done"):
            save_progress(config, h, {
                "chunk_analyses": chunk_analyses,
                "analysis": analysis,
                "incremental_associations": incremental_associations,
                # Persist file_blocks so a stage_2_3_done cache-resume restores
                # them DIRECTLY (it is the authoritative artifact). The retired
                # raw_response could not be re-parsed into FILE blocks, so a
                # resume that relied on it lost every concept/entity block
                # (2026-06-25). Saved BEFORE the marker below so a crash in
                # between never leaves "done" without its artifact.
                "file_blocks": file_blocks,
            })
            mark_stage_done(config, h, "stage_2_3_done")

        # --stop-after-stage 2 = "concept/entity generation only": halt before
        # the 2.5-2.9 tail. stage_2_3_done is set so a re-run caches the chunk
        # pipeline and resumes at 2.5. ("2.0" is the same boundary.)
        if _stop_after_stage(config, "2") or _stop_after_stage(config, "2.0"):
            print(f"\n[stop-after-stage] Stage 2 complete — "
                  f"clean exit (--stop-after-stage=2)")
            raise PrepareStopAfter("2")

        # ── Stage 2.5–2.9A tail: dedup → source page → queries → resolve → comparisons ──
        # Cached as ONE segment under stage_2_9_done. 2.8 (LLM judge) and 2.9A
        # (LLM comparison generation) can fire ConversationPending; without this
        # cache a resume would re-run the whole tail from 2.5. On cache hit,
        # restore the tail outputs from the artifact store and skip the segment.
        #
        # Same guard as the 2.3 cache path: this segment must have persisted a
        # ``file_blocks`` artifact (it always does — see save_progress below).
        # If the marker is set but the artifact is missing (old/partial cache),
        # honoring it would skip the entire 2.5–2.9 tail with whatever
        # file_blocks happens to be in scope — dropping source page / queries /
        # comparisons. Invalidate and re-run the tail instead.
        _tail_cached = (is_stage_done(config, h, "stage_2_9_done")
                        and (progress or {}).get("file_blocks") is not None)
        if is_stage_done(config, h, "stage_2_9_done") and not _tail_cached:
            print("  [stage 2.5–2.9] ⚠️  stage_2_9_done set but no persisted "
                  "file_blocks artifact — invalidating marker and re-running "
                  "the 2.5–2.9 tail (prevents silent query/comparison loss).")
            from _core import unmark_stage_done
            unmark_stage_done(config, h, "stage_2_9_done")
        if _tail_cached:
            _pcache = progress or {}
            file_blocks = _pcache.get("file_blocks", file_blocks)
            query_resolutions = _pcache.get("query_resolutions", {})
            query_count = _pcache.get("query_count", 0)
            comp_count = _pcache.get("comp_count", 0)
            concept_count_before, concept_count_after = _pcache.get(
                "concept_merge_stats", (0, 0))
            dedup_was_run = _pcache.get("dedup_was_run", False)
            print(f"  [stage 2.5–2.9] (cached) tail outputs restored — "
                  f"{len(file_blocks)} blocks")
        else:
            # Stage 2.5: In-source concept dedup & merge (multi-chunk books only).
            # Runs before the source page so the index lists de-duplicated concepts.
            from _stage_2_5_dedup import stage_2_5_dedup
            _stage_2_5 = stage_2_5_dedup(file_blocks, chunk_analyses, config, verbose=verbose)
            file_blocks = _stage_2_5["file_blocks"]
            dedup_was_run = _stage_2_5["dedup_was_run"]
            concept_count_before = _stage_2_5["concept_count_before"]
            concept_count_after = _stage_2_5["concept_count_after"]

            # Stage 2.6: Source page generation + merge
            file_blocks = _prepare_source_page(
                global_digest, raw_file, config, template_content, progress,
                file_blocks, verbose)
            _verify_stage_2_4_file_blocks(file_blocks, raw_file, incremental_associations)

            # ── Stage 2.7: Query generation ──
            query_blocks, _ = stage_2_7_query_generation(
                global_digest, chunk_analyses, file_blocks, raw_file, config,
                template=template_content, verbose=verbose
            )
            if query_blocks:
                file_blocks = list(file_blocks) + query_blocks

            # ── Stage 2.8: Cross-source query resolution ──
            # LLM judge closes queries already answered elsewhere; defaults to "kept".
            from _stage_2_8_query_resolve import (stage_2_8_resolve_queries,
                                                   _stage_2_8_update_file_blocks_after_resolution)
            query_resolutions = stage_2_8_resolve_queries(file_blocks, config.wiki_dir, config)
            if any(r["status"] == "closed" for r in query_resolutions.values()):
                before_q = len(file_blocks)
                file_blocks = _stage_2_8_update_file_blocks_after_resolution(file_blocks, query_resolutions)
                print(f"  [stage 2.8] Removed {before_q - len(file_blocks)} closed query block(s)")

            # ── Stage 2.9: Comparison generation ──
            comp_blocks, _ = stage_2_9_comparison_generation(
                global_digest, chunk_analyses, file_blocks, raw_file, config,
                template=template_content, verbose=verbose
            )
            if comp_blocks:
                file_blocks = list(file_blocks) + comp_blocks

            query_count = len(query_blocks)
            comp_count = len(comp_blocks)

            # Persist tail outputs + mark the segment done so a 2.8/2.9A
            # ConversationPending resume restores instead of re-running.
            save_progress(config, h, {
                "file_blocks": file_blocks,
                "query_resolutions": query_resolutions,
                "query_count": query_count,
                "comp_count": comp_count,
                "concept_merge_stats": (concept_count_before, concept_count_after),
                "dedup_was_run": dedup_was_run,
            })
            mark_stage_done(config, h, "stage_2_9_done")

        analysis["__source_hash"] = h
        analysis["__extract_method"] = method

        print(f"  [prepare] ✅ done — {len(file_blocks)} blocks")
        current_domain = _detect_domain(raw_file, template_content, global_digest)
        return {
            "raw_file": raw_file, "config": config,
            "h": h, "method": method, "extracted_text": extracted_text,
            "global_digest": global_digest, "chunk_analyses": chunk_analyses,
            "analysis": analysis,
            "file_blocks": file_blocks,
            "stage_1_2_result": stage_1_2_result,
            "stage_1_3_result": stage_1_3_result,
            "template_name": template_name,
            "query_count": query_count,
            "comp_count": comp_count,
            "concept_merge_stats": (concept_count_before, concept_count_after),
            "dedup_was_run": dedup_was_run,
            "current_domain": current_domain,
            "incremental_associations": incremental_associations,
            "query_resolutions": query_resolutions,
            "enrich_enabled": getattr(config, "enrich_enabled", True),
        }
    except Exception as e:
        print(f"  [prepare] ❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
        raise
