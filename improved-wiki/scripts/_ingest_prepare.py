"""_ingest_prepare.py — Stage 0-2 synthesis / source-page prep (extracted from ingest.py)."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from _core import (
    Config,
    PrepareStopAfter,
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
)
from _frontmatter import extract_frontmatter_title
from _frontmatter_array import parse_frontmatter_array
from _stage_1_3_caption import _stage_1_3_inline_captions
from _stage_2_6_source_page import stage_2_6_source_page
from _stage_2_7_query_generation import stage_2_7_query_generation
from _stage_2_9_comparison import (
    stage_2_9_comparison_generation,
    stage_2_9_append_source_backlinks,
)
from _stage_validators import (
    verify_stage_0,
    StageValidationError,
    _verify_stage_2_4_file_blocks,
)
from _ingest_skip import _stage_0_2_should_skip, _stop_after_stage
from _ingest_chunks import _run_chunk_pipeline

# ── A6 (audit H2): big-book grounding de-bias ──
# 2.7/2.9 grounding was `extracted_text[:source_budget]` — a pure front
# prefix, so a 1.55M-char book fed only its first ~19% and every query /
# comparison skewed to the early chapters. Sample per-chapter heads instead.
try:
    from _stage_2_analyze import _CHAPTER_ANCHOR_RE
except ImportError:  # keep prepare importable if analyze internals move
    _CHAPTER_ANCHOR_RE = re.compile(
        r"^#{1,3}\s*(第[一二三四五六七八九十百0-9]+章[^\n]*|Chapter\s+\d+[^\n]*)",
        re.MULTILINE | re.IGNORECASE)

_CHAPTER_SAMPLE_SEP = "\n\n[…]\n\n"


def _split_source_chapters(text: str) -> list[str]:
    """Split text at chapter anchors (第N章 / Chapter N headings). Front matter
    before the first anchor is its own segment; [text] when no anchor found."""
    starts = [m.start() for m in _CHAPTER_ANCHOR_RE.finditer(text)]
    if not starts:
        return [text] if text else []
    bounds = ([0] if starts[0] > 0 else []) + starts + [len(text)]
    return [seg for seg in (text[s:e] for s, e in zip(bounds, bounds[1:]))
            if seg.strip()]


def _stratified_source_sample(text: str, budget: int) -> str:
    """Concatenate equal per-chapter head slices up to ``budget`` chars.

    Texts within budget pass through whole; texts without chapter anchors
    keep the old prefix behavior (nothing to stratify on)."""
    if len(text) <= budget:
        return text
    chapters = _split_source_chapters(text)
    if len(chapters) <= 1:
        return text[:budget]
    sep_total = len(_CHAPTER_SAMPLE_SEP) * (len(chapters) - 1)
    per_chapter = (budget - sep_total) // len(chapters)
    if per_chapter <= 0:
        return text[:budget]
    sample = _CHAPTER_SAMPLE_SEP.join(ch[:per_chapter] for ch in chapters)
    return sample[:budget]


def _stage_2_7_queries_index_block(file_blocks: list, config: Config) -> tuple[str, str] | None:
    """A7 (audit H5): queries/ had no real index — lint left a `tags: [stub]`
    placeholder and no page linked the query pages. Build a queries/index.md
    listing block (Stage 3.1 overwrites listing pages, no merge) appending
    this ingest's surviving query slugs to the on-disk index, creating it
    when missing or still a lint stub. Returns None when nothing to add."""
    entries = []
    for path, content in file_blocks:
        norm = path[len("wiki/"):] if path.startswith("wiki/") else path
        if not norm.startswith("queries/"):
            continue
        stem = Path(norm).stem
        if stem == "index":
            continue
        entries.append((stem, extract_frontmatter_title(content) or stem))
    if not entries:
        return None

    index_path = config.wiki_dir / "queries" / "index.md"
    existing = ""
    if index_path.is_file():
        try:
            existing = index_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            existing = ""
    is_stub = bool(existing) and "stub" in parse_frontmatter_array(existing, "tags")
    if not existing or is_stub:
        # No frontmatter — matches the root index.md listing-page convention.
        existing = "# Queries Index\n\nOpen questions raised by ingested sources.\n"
    new_lines = [f"- [[queries/{stem}]] — {title}"
                 for stem, title in entries
                 if f"[[queries/{stem}]]" not in existing]
    if not new_lines:
        return None
    return ("queries/index.md",
            existing.rstrip("\n") + "\n\n" + "\n".join(new_lines) + "\n")


def _prepare_source_page(
    global_digest: dict, raw_file: Path, config: Config,
    template_content: str, progress: dict | None, file_blocks: list,
    verbose: bool, source_context: str = "",
    associations: dict | None = None,
    chunk_claims: list | None = None,
) -> list:
    """Stage 2.6: generate the source page (dedicated LLM call) and merge into file_blocks."""
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
        # Generated-this-ingest concept/entity slugs (from 2.4 file_blocks) —
        # feeds the source page Key Concepts/Entities (NashSU single-tier: list
        # ALL generated pages, not the curated 2.1 key_concepts).
        _gen_concepts = [s for s in _linkable if s.startswith("concepts/")]
        _gen_entities = [s for s in _linkable if s.startswith("entities/")]
        _linkable.extend(list_existing_slugs(config))
        source_page_response, _ = stage_2_6_source_page(
            global_digest, raw_file, config,
            template=template_content, verbose=verbose,
            linkable_slugs=_linkable, source_context=source_context,
            associations=associations,
            generated_concepts=_gen_concepts, generated_entities=_gen_entities,
            chunk_claims=chunk_claims,
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
    stub = f"---\ntype: source\ntitle: \"{title}\"\n"
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
    prefetch_only: bool = False,
) -> dict | None:
    """Stage 0-2 for one book.

    Two segments with different cross-book safety:
      - **Wiki-independent (0/1/2.1/2.2)** — reads only the book's own
        text/digest, writes no wiki/ state. Safe to run for several books in
        parallel ("prefetch"). ``prefetch_only=True`` runs exactly this segment
        then raises ``PrepareStopAfter("1.5")`` at the Stage 2.2/2.3 boundary.
      - **Wiki-dependent (2.3–2.9)** — Stage 2.3 reads ``config.wiki_dir`` to
        link/dedup against existing pages; 2.4–2.9 build on that. MUST run in the
        serial spine (one book at a time) so each book sees prior books' written
        pages. ``prefetch_only=False`` (default) runs the full segment, reusing
        cached 2.2.

    Returns the prepared dict for Stage 3+, or None on skip/failure.
    """
    _set_current_file(raw_file.name)
    print(f"\n=== [prepare] {raw_file.name} ===")
    try:
        # Dedup check — skip only if the ingest is truly complete (the
        # ``ingested`` completion marker is set); otherwise resume or re-ingest.
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
        # If the Stage 3.1-3.2 write phase already completed in a prior run,
        # skip the entire 2.x pipeline. Re-running Stage 2.4 generation would
        # cache-miss every resume because the generation prompt hash drifts
        # with wiki state (pages written/rewritten), looping forever before
        # _do_write can be reached. _do_write handles write_phase_done by
        # setting _write_blocks=[] and skipping 3.1/3.2, then runs
        # 3.4-3.7 over the on-disk wiki. chunk_analyses/analysis are not
        # needed post-write (3.4+ scan the wiki dir, not file_blocks).
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
                "query_count": 0, "comp_count": 0,
                "concept_merge_stats": (0, 0), "dedup_was_run": False,
                "incremental_associations": {},
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

            # Stage 0 Validation (Phase 2: per-stage verification)
            if not verify_stage_0(extracted_text):
                print(f"  [validate] ❌ Stage 0 failed: text extraction insufficient")
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
            if progress and "stage_1_2" in progress:
                stage_1_2_result = progress["stage_1_2"]
                print(f"  [stage 1.2] (cached) {stage_1_2_result.get('count', 0)} images")
            elif method.startswith("mineru"):
                # method is "mineru-api" for all PDFs (extraction quality gate
                # removed 2026-07-08; all minerU runs produce images on disk).
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
                save_progress(config, h, {"stage_1_2": stage_1_2_result})
                mark_stage_done(config, h, "stage_1_2_done")
            elif raw_file.suffix.lower() in (".md", ".markdown"):
                # .md sources (method="plain-text"): extract local images referenced
                # via ![[ref]] / ![alt](ref) — NashSU extractAndSaveMarkdownImages parity.
                stage_1_2_result = stage_1_2_extract_images(raw_file, config)
                save_progress(config, h, {"stage_1_2": stage_1_2_result})
                mark_stage_done(config, h, "stage_1_2_done")

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

        # Stage 1.2->1.3 image pipeline. The standalone whole-book global
        # digest (former Stage 2.1) was removed 2026-07-08 for NashSU
        # alignment: the digest now rolls up inside Stage 2.2 (empty seed,
        # per-chunk updated_global_digest). 1.2/1.3 no longer parallel 2.1.
        stop_after_0 = _stop_after_stage(config, "0")

        stage_1_2_result, stage_1_3_result = _run_image_pipeline()

        # Persist Stage 1.2/1.3 immediately.
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

        # 2.1 removed: global_digest starts empty; Stage 2.2 rolls it up and
        # returns the final rolled-up dict (consumed by 2.4/2.6/2.7/2.9).
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

        # Stage 2.2 → 2.3 → 2.4 chunk pipeline. ``analyze_only=prefetch_only``
        # stops at the 2.2/2.3 boundary (wiki-independent prefetch) by raising
        # PrepareStopAfter("1.5"); the spine run (prefetch_only=False) reuses the
        # cached 2.2 and runs the wiki-dependent 2.3+ tail.
        chunk_analyses, analysis, file_blocks, incremental_associations, global_digest = _run_chunk_pipeline(
            extracted_text, global_digest, raw_file, config, template_content,
            progress, verbose, analyze_only=prefetch_only)

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

        # ── Stage 2.5–2.9 tail: dedup → source page → queries → resolve → comparisons ──
        # Cached as ONE segment under stage_2_9_done. 2.8 (LLM judge) and 2.9
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
            print(f"  [stage 2.4–2.9] (cached) tail outputs restored — "
                  f"{len(file_blocks)} blocks")
        else:
            # Stage 2.4 closing sub-step: in-source concept dedup & merge
            # (multi-chunk books only). Runs before the source page so the index
            # lists de-duplicated concepts. (Former standalone Stage 2.5; folded
            # into 2.4 — embedding prefilter + LLM confirm, no-fallback raise.)
            from _dedup_intra_source import dedup_intra_source
            _stage_2_5 = dedup_intra_source(file_blocks, chunk_analyses, config, verbose=verbose)
            file_blocks = _stage_2_5["file_blocks"]
            dedup_was_run = _stage_2_5["dedup_was_run"]
            concept_count_before = _stage_2_5["concept_count_before"]
            concept_count_after = _stage_2_5["concept_count_after"]

            # Source grounding shared by 2.6/2.7/2.9 (P1): raw source trimmed to
            # the model-sized budget. Whole-book synthesis calls, so a budgeted
            # excerpt is the right analog (cf. the single-chunk 2.4 path).
            _src_grounding = (extracted_text or "")[: config.source_budget]

            # Stage 2.6: Source page generation + merge
            file_blocks = _prepare_source_page(
                global_digest, raw_file, config, template_content, progress,
                file_blocks, verbose, source_context=_src_grounding,
                associations=incremental_associations,
                # Full-book claim coverage for Main Arguments (2026-07-02):
                # the digest's key_claims skew to the front sample; the 2.2
                # chunk claims span every chapter by construction.
                chunk_claims=[c for ca in (chunk_analyses or [])
                              if isinstance(ca, dict)
                              for c in (ca.get("claims") or [])])
            _verify_stage_2_4_file_blocks(file_blocks, raw_file, incremental_associations)

            # A6 (audit H2): 2.7/2.9 use a stratified per-chapter sample, not
            # the front prefix — 2.6 keeps the prefix (the audit targets
            # query/comparison covering, not the source page digest).
            _q29_source = _stratified_source_sample(
                extracted_text or "", config.source_budget)
            _chapter_count = len(_CHAPTER_ANCHOR_RE.findall(extracted_text or ""))

            # ── Stage 2.7: Query generation ──
            query_blocks, _ = stage_2_7_query_generation(
                global_digest, chunk_analyses, file_blocks, raw_file, config,
                template=template_content, template_name=template_name, verbose=verbose,
                source_context=_q29_source,
            )
            # Stage 2.7 closing sub-step: cross-source query resolution (former
            # standalone Stage 2.8; folded into 2.7). Embedding prefilter matches
            # each new query against existing concept/entity pages; LLM judge
            # closes queries already answered elsewhere, defaults to "kept".
            # no-fallback: a missing embedding stack raises here (clean re-run —
            # stage_2_9_done is set only after the whole tail succeeds).
            if query_blocks:
                file_blocks = list(file_blocks) + query_blocks
                from _query_resolve_cross_source import (query_resolve_cross_source,
                                                       _query_resolve_update_file_blocks_after_resolution,
                                                       _query_resolve_apply_cross_refs)
                query_resolutions = query_resolve_cross_source(file_blocks, config.wiki_dir, config)
                if any(r["status"] == "closed" for r in query_resolutions.values()):
                    before_q = len(file_blocks)
                    file_blocks = _query_resolve_update_file_blocks_after_resolution(file_blocks, query_resolutions)
                    print(f"  [stage 2.7] Removed {before_q - len(file_blocks)} closed query block(s)")
                # A3: write resolve conclusions into kept query frontmatter
                # (cross_refs) instead of leaving them only in the progress cache.
                file_blocks = _query_resolve_apply_cross_refs(file_blocks, query_resolutions)
            else:
                query_resolutions = {}

            # ── Stage 2.9: Comparison generation ──
            comp_blocks, _ = stage_2_9_comparison_generation(
                global_digest, chunk_analyses, file_blocks, raw_file, config,
                template=template_content, verbose=verbose,
                source_context=_q29_source,
                chapter_count=_chapter_count,
            )
            if comp_blocks:
                file_blocks = list(file_blocks) + comp_blocks
                # A7: backlink the new comparisons from the source page block
                # (2.9 runs after 2.6 — without this they stay an inlink island).
                file_blocks = stage_2_9_append_source_backlinks(file_blocks, comp_blocks)

            # A7: refresh the real queries/index.md with this ingest's kept
            # queries, while the blocks are still in memory.
            _qidx_block = _stage_2_7_queries_index_block(file_blocks, config)
            if _qidx_block:
                file_blocks = list(file_blocks) + [_qidx_block]
                print("  [stage 2.7] queries/index.md listing block appended")

            query_count = len(query_blocks)
            comp_count = len(comp_blocks)

            # Persist tail outputs + mark the segment done so a 2.8/2.9
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
            "incremental_associations": incremental_associations,
            "query_resolutions": query_resolutions,
            "enrich_enabled": getattr(config, "enrich_enabled", True),
        }
    except Exception as e:
        print(f"  [prepare] ❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
        raise
