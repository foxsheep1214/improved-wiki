"""_ingest_prepare.py — Stage 0-2 synthesis / source-page prep (extracted from ingest.py)."""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from _core import (
    Config,
    detect_domain as _detect_domain,
    detect_template_type,
    load_template,
    file_sha256,
    load_progress,
    save_progress,
    parse_file_blocks,
    set_current_file as _set_current_file,
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
from _ingest_skip import _stage_0_2_should_skip
from _ingest_chunks import _run_chunk_pipeline

def _prepare_source_page(
    global_digest: dict, raw_file: Path, config: Config,
    template_content: str, progress: dict | None, file_blocks: list,
    verbose: bool,
) -> list:
    """Stage 2.6: generate the source page (dedicated LLM call) and merge into file_blocks."""
    current_domain = _detect_domain(raw_file, template_content, global_digest)
    if progress and progress.get("stage") in ("stage_2_4_done", "stage_2_3_done") and "source_page_response" in progress:
        source_page_response = progress["source_page_response"]
        print(f"  [stage 2.6] (cached) Source page already generated")
    else:
        source_page_response, _ = stage_2_6_source_page(
            global_digest, raw_file, config,
            template=template_content, current_domain=current_domain, verbose=verbose
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
                "stage": "stage_1_1_done", "extracted_text": extracted_text,
                "extract_method": method,
            })

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
                cp = {"stage": "stage_1_1_done", "extracted_text": extracted_text,
                      "extract_method": method, "stage_1_2": stage_1_2_result}
                save_progress(config, h, cp)
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
        needs_digest = (
            not progress or progress.get("stage") not in ("stage_2_1_done", "stage_2_2_done", "stage_2_3_done")
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            fut_images = executor.submit(_run_image_pipeline)
            fut_digest = executor.submit(stage_2_1_global_digest, extracted_text, raw_file, config,
                                        template_content, verbose=verbose) if needs_digest else None

            stage_1_2_result, stage_1_3_result = fut_images.result()

            # Persist Stage 1.2/1.3 immediately, before awaiting the digest future.
            # fut_digest.result() below can raise ConversationPending (conversation-
            # mode cache miss), which propagates out of this function before a
            # later save_progress call would ever be reached — every subsequent
            # conversation-mode round-trip would otherwise re-run
            # _run_image_pipeline() from scratch for this source, forever.
            if not progress or "stage_1_2" not in progress:
                save_progress(config, h, {
                    "stage": "stage_1_1_done", "extracted_text": extracted_text,
                    "extract_method": method,
                    "stage_1_2": stage_1_2_result, "stage_1_3": stage_1_3_result,
                })

            global_digest = fut_digest.result() if fut_digest else progress.get("global_digest", {})

        if needs_digest:
            _verify_stage_2_1_digest(global_digest, raw_file)
        else:
            print(f"  [stage 2.1] (cached) Global Digest — {len(global_digest)} keys")
            _verify_stage_2_1_digest(global_digest, raw_file)

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
                save_progress(config, h, {
                    "stage": "stage_1_1_done", "extracted_text": extracted_text,
                    "extract_method": method, "stage_1_2": stage_1_2_result,
                    "stage_1_3": stage_1_3_result,
                })
                print(f"  [caption] Inlined VLM captions as alt text into "
                      f"extracted_text ({len(extracted_text):,} chars)")

        # Stage 2.2 + 2.4: Chunk Analysis → Generation (barrier-free pipeline)
        chunk_analyses, analysis, raw_response, file_blocks, incremental_associations = _run_chunk_pipeline(
            extracted_text, global_digest, raw_file, config, template_content,
            progress, verbose)

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

        analysis["__source_hash"] = h
        analysis["__extract_method"] = method

        print(f"  [prepare] ✅ done — {len(file_blocks)} blocks")
        current_domain = _detect_domain(raw_file, template_content, global_digest)
        return {
            "raw_file": raw_file, "config": config,
            "h": h, "method": method, "extracted_text": extracted_text,
            "global_digest": global_digest, "chunk_analyses": chunk_analyses,
            "analysis": analysis, "raw_response": raw_response,
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
