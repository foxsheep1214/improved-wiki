"""_ingest_write.py — Stage 3+ file writing + post-ingest (extracted from ingest.py)."""
from __future__ import annotations

import time
from pathlib import Path

from _core import (
    Config,
    is_safe_ingest_path,
    list_existing_slugs,
    load_schema_md,
    schema_folders,
    parse_wiki_schema_routing,
    BASE_PAGE_DIRS,
    is_stage_done,
    get_stage_payload,
    mark_stage_done,
    load_cache,
    save_cache,
    clear_progress,
    load_progress,
)
from _stage_3_write import (
    _stage_3_1_wiki_path_for_source,
    _stage_3_1_auto_correct_wiki_path,
    _stage_3_1_schema_route,
    _stage_3_1_canonicalize_sources_field,
    _stage_3_1_stamp_frontmatter_dates,
    stage_3_1_build_slug_dirs,
    stage_3_1_normalize_page_links,
    stage_3_1_write_wiki_file,
    stage_3_5_aggregate_repair,
)
from _stage_3_2_inject_images import stage_3_2_inject_images
from _stage_3_4_review import stage_3_4_review_suggestions
from _stage_validators import validate_stage_outputs
from _enrich_wikilinks import enrich_wikilinks_batch

# Monotonic counter fields in a cache entry's `stages` dict: these should
# never regress across write passes. On a write_phase-resume pass the prepared
# dict carries empty chunk_analyses/global_digest (the 2.x short-circuit), so a
# naive rebuild would zero these counts and trip validate_ingest's
# "N chunk(s) analyzed" check (Orin #5 / 2026-06-25 Fardo 18/19 false-fail).
_STAGE_COUNTER_FIELDS = (
    "global_digest_keys", "chunks_analyzed", "file_blocks_generated",
    "concepts_identified", "concepts_core", "concepts_supporting",
    "concepts_generated", "entities_generated",
    "images_extracted", "images_captioned", "images_injected",
    "queries_generated", "comparisons_generated", "review_items",
)


def _preserve_stage_counters(prev_stages: dict, new_stages: dict) -> dict:
    """Return new_stages with monotonic counters preserved as max(old, new).

    Non-counter fields (coverage_core / coverage_supporting / coverage_pct —
    ratios, not counts) keep the new value. prev_stages may be empty (first
    write); then new_stages is returned unchanged.
    """
    if not prev_stages:
        return dict(new_stages)
    out = dict(new_stages)
    for k in _STAGE_COUNTER_FIELDS:
        if k in out:
            out[k] = max(int(prev_stages.get(k, 0) or 0), int(out[k] or 0))
    return out


def reconstruct_enrich_candidates(
    files_written_paths: list[str], wiki_dir: Path, listing_pages: set[str]
) -> list[tuple[str, "Path"]]:
    """Rebuild the (rel_path, full_path) enrich list from a persisted file list.

    On a ``write_loop_done`` resume the write loop is skipped, so enrich
    candidates must be reconstructed from ``files_written_paths``. Those are
    stored relative to wiki_root and therefore carry a leading ``wiki/`` segment
    (e.g. ``wiki/concepts/foo.md``). The fresh write loop, by contrast, feeds
    the enricher wiki_dir-relative paths (``_stage_3_write`` strips ``wiki/`` →
    ``concepts/foo.md``).

    The enrichment prompt is keyed by these rel_paths, so the two conventions
    must match EXACTLY — otherwise a resume produces a different prompt hash than
    the fresh run and the conversation router fires a spurious SECOND enrichment
    handoff for the same ingest (bug 2026-07-01). This helper strips the
    ``wiki/`` prefix so resume matches the fresh convention, and builds
    full_path from wiki_dir/rel (== wiki_root/p) so the on-disk target is
    unchanged. Listing pages (index/log/overview/schema) are excluded.
    """
    out: list[tuple[str, Path]] = []
    for p in files_written_paths:
        rel = p[len("wiki/"):] if p.startswith("wiki/") else p
        if Path(rel).name in listing_pages:
            continue
        out.append((rel, wiki_dir / rel))
    return out


def _is_redundant_duplicate_write(full_path, content: str, written_this_run: dict) -> bool:
    """True when this exact (path, content) was already written earlier in THIS
    write loop — a duplicate FILE block (e.g. the source page emitted by both
    2.6 and a later step). Re-merging a page against our own byte-identical
    just-written output wastes one LLM merge handoff per duplicate —
    delegate-mode.md documented "2-3 redundant source-page merge prompts per
    ingest" and told the operator to reuse the first merge result by hand; now
    enforced in code. A duplicate path with DIFFERENT content is NOT redundant:
    that is the designed same-slug collision merge — let it through.
    """
    return written_this_run.get(full_path) == content


def _reconstruct_blocks_from_disk(
    config: Config, files_written_paths: list[str]
) -> list[tuple[str, str]]:
    """Read the just-written wiki pages back as (wiki_dir-relative path, content).

    Post-write stages — Stage 3.4 review, go/no-go validation, and the cache
    stage-stats — must operate on the ACTUAL pages on disk, not on the
    in-memory ``file_blocks`` list. On a write_phase / write_loop_done resume,
    ``file_blocks`` is legitimately ``[]`` (the prepare short-circuit returns no
    blocks), which previously made those stages: (3.4) re-fire a redundant
    review over "0 new pages", (validation) report spurious "0 FILE blocks /
    source page missing" failures, and (cache) record zeroed stage stats.

    Reading from disk is stable across every resume pass (the files don't change
    once written + enriched), so a fresh pass and a resume pass produce the
    SAME review input — the conversation-mode hash matches and the cached
    review answer is reused instead of triggering a second LLM call.

    ``files_written_paths`` are wiki_root-relative (e.g. ``wiki/concepts/x.md``);
    the returned paths are wiki_dir-relative (e.g. ``concepts/x.md``) to match
    the ``file_blocks`` convention every downstream consumer expects. Listing
    pages (index/log/overview) are not in ``files_written_paths`` at this point,
    so they are naturally excluded.
    """
    blocks: list[tuple[str, str]] = []
    for p in files_written_paths:
        full = config.wiki_root / p
        if not full.exists():
            continue
        rel = p[len("wiki/"):] if p.startswith("wiki/") else p
        try:
            blocks.append((rel, full.read_text(encoding="utf-8")))
        except OSError:
            continue
    return blocks


def _do_write(prepared: dict, verbose: bool = False) -> dict:
    """Stage 3+ for one book.  Writes wiki files, updates cache, runs validation.
    MUST be called serially — modifies shared wiki/ state.
    """
    raw_file = prepared["raw_file"]
    config = prepared["config"]
    h = prepared["h"]
    method = prepared["method"]
    extracted_text = prepared["extracted_text"]
    global_digest = prepared["global_digest"]
    chunk_analyses = prepared["chunk_analyses"]
    analysis = prepared["analysis"]
    file_blocks = prepared["file_blocks"]
    stage_1_2_result = prepared["stage_1_2_result"]
    stage_1_3_result = prepared["stage_1_3_result"]
    template_name = prepared["template_name"]
    query_count = prepared.get("query_count", 0)
    comp_count = prepared.get("comp_count", 0)

    print(f"\n=== [write] {raw_file.name} ===")

    # Write wiki files (same logic as ingest_one Stage 3+)
    source_path = _stage_3_1_wiki_path_for_source(raw_file, config)
    files_written_paths: list[str] = []
    hard_failures: list[str] = []
    source_block: tuple[str, str] | None = None

    # Base page types + any schema-defined folders (NashSU schema-driven routing):
    # a page in a schema folder (e.g. wiki/methodology/, wiki/people/) is accepted
    # instead of being auto-corrected/dropped. Non-schema folders still fall through
    # to auto-correct (typo safety net).
    _schema_md_text = load_schema_md(config)
    _VALID_SUBDIRS = set(BASE_PAGE_DIRS) | schema_folders(_schema_md_text)
    # Precise type→dir map for write-time schema routing (NashSU
    # validateWikiPageRouting parity). Built once per book.
    _routing = parse_wiki_schema_routing(_schema_md_text)
    _LISTING_PAGES = {"index.md", "log.md", "overview.md", "schema.md"}

    try:
        from _language import detect_language
        expected_lang = detect_language(extracted_text[:5000]) if extracted_text else "unknown"
    except ImportError:
        expected_lang = "unknown"

    canonical_source = f"raw/{raw_file.relative_to(config.raw_root)}"
    today_str = time.strftime("%Y-%m-%d")

    # ── Wikilink enrichment setup (round iv, 2026-06-22) ──
    # After every page in this ingest is written, one batched LLM call (via
    # the conversation router — see _enrich_wikilinks.enrich_wikilinks_batch)
    # suggests [[wikilinks]] for body terms matching existing wiki pages or
    # sibling pages from this same ingest. Gated by --enrich-wikilinks /
    # --no-enrich. existing_slugs is a pre-loop snapshot of the wiki.
    # enrich_candidates collects (rel_path, full_path) for non-listing pages
    # written this ingest; the batch call happens once, after the loop.
    enrich_enabled = prepared.get("enrich_enabled", True)
    existing_slugs = list_existing_slugs(config) if enrich_enabled else []
    enrich_candidates: list[tuple[str, Path]] = []

    # Option A: stage-aware resume.  If the write phase (3.1 write loop —
    # incl. same-slug page-merge / slug-collision handling — + enrichment +
    # 3.2 inject) already completed in a prior run, skip it entirely.
    # Re-running the write loop would spuriously fire page-merge LLM
    # round-trips because post-write steps (enrichment, image injection) have
    # mutated page bodies.  Restore file list from the marker.
    write_phase_done = is_stage_done(config, h, "write_phase")
    # write_loop_done is the finer-grained 3.1-only gate: the write loop
    # completed but enrichment/3.2 have not. An enrich ConversationPending
    # handoff fires AFTER the write loop; without this marker, resume would
    # re-run 3.1 and spuriously re-merge every page. On write_loop_done resume
    # we still need to run enrich + 3.2, so enrich_candidates are
    # reconstructed from the persisted file list instead of collected in-loop.
    write_loop_done = (not write_phase_done
                       and is_stage_done(config, h, "write_loop_done"))
    if write_phase_done:
        print("  [write] write_phase marker present — skipping 3.1/3.2")
        _wp = get_stage_payload(config, h, "write_phase")
        files_written_paths = _wp.get("files_written", [])
        source_block = ("source", "")  # source page already written
        hard_failures = []
        stage_3_2_result = {"injected": _wp.get("images_injected", 0)}
    elif write_loop_done:
        print("  [write] write_loop_done marker present — skipping 3.1 write loop")
        _wlp = get_stage_payload(config, h, "write_loop_done")
        files_written_paths = list(_wlp.get("files_written", []))
        hard_failures = []
        # If the source page was written in the prior loop, skip the placeholder
        # build below; otherwise leave source_block=None so the placeholder
        # runs and creates the source page (it hadn't been reached before the
        # enrich handoff).
        _src_rel = str(source_path.relative_to(config.wiki_root))
        source_block = ("source", "") if _src_rel in files_written_paths else None
        # Reconstruct enrich_candidates from the persisted file list so the
        # enrich batch (below) still runs over the already-written pages. The
        # helper strips the leading "wiki/" so the rel_path matches the fresh
        # write-loop convention exactly (else a resume re-keys the enrichment
        # prompt and fires a spurious SECOND handoff — bug 2026-07-01).
        enrich_candidates = reconstruct_enrich_candidates(
            files_written_paths, config.wiki_dir, _LISTING_PAGES)
    _write_blocks = [] if (write_phase_done or write_loop_done) else file_blocks

    # A5 write-time link normalizer (audit 2026-07-02, M6): slug→dir universe
    # = this batch ∪ on-disk wiki, built once per book. Empty on resume passes
    # (the loop below is skipped, so the universe is unused).
    _slug_dirs = (stage_3_1_build_slug_dirs(_write_blocks, config, _VALID_SUBDIRS, _routing)
                  if _write_blocks else {})
    # D4 figure-ref backstop: wiki-relative slug of this book's source page —
    # the normalizer wraps bare 图X.X/表X.X/Fig X-X/Table X-X body refs as
    # [[<slug>|据<ref>]] and skips the source page itself (own slug match).
    _source_page_slug = source_path.relative_to(config.wiki_dir).with_suffix("").as_posix()

    # Duplicate-block guard (redundancy fix 2026-07-09): (path → content)
    # written earlier in THIS loop; see _is_redundant_duplicate_write.
    _written_this_run: dict[Path, str] = {}

    for rel_path, content in _write_blocks:
        if ".." in rel_path or rel_path.startswith("/"):
            continue
        if not is_safe_ingest_path(rel_path):
            continue

        top_dir = rel_path.split("/")[0] if "/" in rel_path else ""
        basename = Path(rel_path).name
        if basename in _LISTING_PAGES:
            pass
        elif top_dir not in _VALID_SUBDIRS:
            corrected = _stage_3_1_auto_correct_wiki_path(rel_path, content, config)
            if corrected:
                print(f"  [write] Auto-corrected: {rel_path} → {corrected}")
                rel_path = corrected
            else:
                print(f"  [write] Dropped — cannot correct path: {rel_path}")
                continue

        if not rel_path.endswith(".md"):
            rel_path = rel_path + ".md"

        # Schema routing (NashSU validateWikiPageRouting parity): place the page
        # in the directory its frontmatter `type` declares (schema typeDirs →
        # base types). Auto-corrects type↔dir mismatches the accept-list above
        # cannot catch — e.g. a type:concept page sitting in a schema folder, or
        # a schema type:person page written to entities/.
        if basename not in _LISTING_PAGES:
            routed = _stage_3_1_schema_route(rel_path, content, _routing)
            if routed != rel_path:
                print(f"  [write] Schema-routed: {rel_path} → {routed}")
                rel_path = routed

        # Skip per-block language check for minerU OCR — OCR text from garbled
        # PDFs confuses the detector (e.g. C0 control chars → wrong language).
        # All minerU extraction methods carry a `mineru-*` label, so match by
        # prefix; an explicit list previously missed variants and fired false
        # warnings.
        if not method.startswith("mineru"):
            if expected_lang not in ("unknown", "English"):
                try:
                    from _language import detect_language
                    block_lang = detect_language(content[:2000])
                    if block_lang not in (expected_lang, "English") and block_lang != "unknown":
                        print(f"  [lang] ⚠️  {rel_path}: expected {expected_lang}, got {block_lang}")
                except ImportError:
                    pass

        content = _stage_3_1_canonicalize_sources_field(content, canonical_source)
        content = _stage_3_1_stamp_frontmatter_dates(content, today_str)

        # A5 (audit M6): single write-time normalization pass — related →
        # prefixed bare slugs (unresolvable dropped), bare body wikilinks
        # prefixed when uniquely resolvable, H1 wikilinks de-linked,
        # self-links removed, bare figure/table refs wrapped as source-page
        # links (D4 backstop). Loud per-page prints, never silent.
        if basename not in _LISTING_PAGES:
            content = stage_3_1_normalize_page_links(
                rel_path, content, _slug_dirs, source_page_slug=_source_page_slug)

        full_path = config.wiki_dir / rel_path
        is_listing = basename in _LISTING_PAGES

        if _is_redundant_duplicate_write(full_path, content, _written_this_run):
            print(f"  [skip] {rel_path} — duplicate block, identical to "
                  f"content already written this ingest")
            continue

        do_merge = full_path.exists() and not is_listing

        try:
            stage_3_1_write_wiki_file(full_path, content, config, merge=do_merge)
        except OSError as e:
            print(f"  [write] HARD ERROR: {rel_path} — {e}")
            hard_failures.append(rel_path)
            continue

        _written_this_run[full_path] = content
        files_written_paths.append(str(full_path.relative_to(config.wiki_root)))
        if full_path == source_path:
            source_block = (rel_path, content)
        action = "[merge]" if do_merge else "[overwrite]" if is_listing and full_path.exists() else "[write]"
        print(f"  {action} {rel_path}")

        if enrich_enabled and not is_listing:
            enrich_candidates.append((rel_path, full_path))

    # Mark the 3.1 write loop complete so an enrich/3.2 ConversationPending
    # resume skips the loop (preventing spurious page re-merge). Only when the
    # loop ran fresh — not on write_phase_done / write_loop_done resumes.
    if not (write_phase_done or write_loop_done):
        mark_stage_done(config, h, "write_loop_done",
                        payload={"files_written": files_written_paths})

    if enrich_enabled and enrich_candidates and not write_phase_done:
        # Enrich the ACTUAL written content (post-merge) so links target real
        # pages. One batched call for the whole ingest — see
        # _enrich_wikilinks.enrich_wikilinks_batch. Not wrapped in try/except:
        # a malformed response or routing error now fails the ingest visibly,
        # same as any other stage.
        pages_for_enrichment = [
            (rel_path, full_path.read_text(encoding="utf-8"))
            for rel_path, full_path in enrich_candidates
        ]
        enriched_pages = enrich_wikilinks_batch(pages_for_enrichment, existing_slugs, config)
        for rel_path, full_path in enrich_candidates:
            if rel_path in enriched_pages:
                full_path.write_text(enriched_pages[rel_path], encoding="utf-8")
                print(f"  [enrich] {rel_path} (+wikilinks)")

    if not source_block:
        # Build NashSU-quality source page from digest data (no LLM needed)
        book_meta = analysis.get("book_meta", {})
        outline = analysis.get("outline", [])
        key_claims = analysis.get("key_claims", [])
        title = book_meta.get("title", raw_file.stem)
        authors = book_meta.get("authors", [])
        year = book_meta.get("year", "")
        publisher = book_meta.get("publisher", "")

        lines = [
            "---",
            "type: source",
            f'title: "{title}"',
            f"created: {today_str}",
            f"updated: {today_str}",
            "tags: []",
            "related: []",
            f'sources: ["{canonical_source}"]',
            "---",
            "",
            f"# {title}",
            "",
        ]
        if authors:
            lines.append(f"**Authors:** {', '.join(str(a) for a in authors[:5])}")
        if year:
            lines.append(f"**Year:** {year}")
        if publisher:
            lines.append(f"**Publisher:** {publisher}")
        lines.append("")

        if outline:
            lines.append("## Table of Contents & Key Concepts")
            lines.append("")
            for ch in outline[:40]:
                if isinstance(ch, dict):
                    ch_title = ch.get("title", "")
                    topics = ch.get("key_topics", [])
                    topics_str = ", ".join(str(t) for t in topics[:4]) if topics else ""
                else:
                    ch_title = str(ch)
                    topics_str = ""
                lines.append(f"1. **{ch_title}**" + (f" — {topics_str}" if topics_str else ""))
            lines.append("")

        if key_claims:
            lines.append("## Key Takeaways")
            lines.append("")
            for claim in key_claims[:10]:
                if isinstance(claim, dict):
                    lines.append(f"- {claim.get('claim', str(claim))}")
                else:
                    lines.append(f"- {str(claim)}")
            lines.append("")

        placeholder_content = "\n".join(lines) + "\n"
        try:
            stage_3_1_write_wiki_file(source_path, placeholder_content, config)
            files_written_paths.append(str(source_path.relative_to(config.wiki_root)))
        except OSError as e:
            hard_failures.append("source-placeholder")

    # Stage 3.2: Image injection
    if not write_phase_done:
        stage_3_2_result: dict = {"injected": 0}
        if source_path.exists():
            stage_3_2_result = stage_3_2_inject_images(config, raw_file, source_path, method)

        # Mark write phase complete so a post-review resume skips 3.1-3.2
        # (prevents spurious page-merge / re-enrichment / re-injection).
        mark_stage_done(config, h, "write_phase", payload={
            "files_written": files_written_paths,
            "images_injected": stage_3_2_result.get("injected", 0),
        })

    # Reconstruct the review set from the pages actually on disk. Done on EVERY
    # pass (not just resumes): by Stage 3.4 the pages are written AND enriched,
    # so disk content is identical on a fresh run and on any later resume. That
    # determinism makes the Stage 3.4 conversation-mode prompt hash stable, so a
    # post-write resume reuses the cached review answer instead of firing a
    # second redundant review over "0 new pages". It also feeds validation and
    # cache stats the real on-disk pages even when file_blocks is [] on resume
    # (fixes false go/no-go failures and zeroed cache stage-stats). Falls back
    # to the in-memory file_blocks only if nothing was written.
    review_blocks = _reconstruct_blocks_from_disk(config, files_written_paths) or file_blocks

    # Total captioned images on disk. stage_1_3_result["captioned"] is NEW
    # captions (0 on a cache-hit resume), unusable for stats/scoring. Count
    # .caption.txt files in the media dir for the true total (bug 2026-06-25).
    _total_captioned = 0
    _media_dir = stage_1_2_result.get("media_dir")
    if _media_dir:
        from pathlib import Path as _P
        try:
            _total_captioned = sum(1 for _ in _P(_media_dir).glob("*.caption.txt"))
        except OSError:
            _total_captioned = 0

    # Stage 3.4: Review (quality review of generated pages)
    stage_3_4_result = stage_3_4_review_suggestions(
        review_blocks, raw_file, config, verbose=verbose)

    # Go/no-go validation
    go_nogo_warnings = validate_stage_outputs(
        config, raw_file, method, extracted_text,
        stage_1_2_result, stage_1_3_result,
        review_blocks, source_path,
    )


    # Stage 3.5: Aggregate repair
    index_log_files = stage_3_5_aggregate_repair(source_path, raw_file, analysis, h, method, config)

    # Update cache
    try:
        rel = str(raw_file.relative_to(config.raw_root))
    except ValueError:
        rel = str(raw_file)
    # Stage stats: derive page counts from review_blocks (the real on-disk set)
    # and take the max against any in-memory analysis/params, so a write_phase
    # resume — where file_blocks/analysis/query_count are empty — records the
    # true counts instead of overwriting the cache entry with zeros.
    _n_concepts = sum(1 for p, _ in review_blocks if "concepts/" in p)
    _n_entities = sum(1 for p, _ in review_blocks if "entities/" in p)
    _n_queries = sum(1 for p, _ in review_blocks if "queries/" in p)
    _n_comps = sum(1 for p, _ in review_blocks if "comparisons/" in p)
    _n_blocks = max(len(file_blocks), len(review_blocks))
    cache = load_cache(config)

    # B1 fix (2026-06-25, Orin #5 / "0 chunks analyzed" validator false-fail):
    # On a write_phase-resume pass, chunk_analyses/global_digest/etc. are empty
    # (_ingest_prepare short-circuit sets them to [] to skip 2.x), so rebuilding
    # the stages dict would overwrite the real first-pass counts with 0. The
    # validator then reads chunks_analyzed=0 and fails. Preserve monotonic
    # counters via _preserve_stage_counters (max(old, new)).
    _prev_entry = cache.get("entries", {}).get(rel, {}) or {}
    _prev_stages = _prev_entry.get("stages", {}) or {}

    # On write_phase-resume the prepare short-circuit sets chunk_analyses=[].
    # Fall back to the progress file's saved list so chunks_analyzed stays accurate.
    _chunks_analyzed = len(chunk_analyses)
    if not _chunks_analyzed:
        _prog = load_progress(config, h) or {}
        _chunks_analyzed = len(_prog.get("chunk_analyses") or [])

    _new_stages = {
        "global_digest_keys": len(global_digest),
        "chunks_analyzed": _chunks_analyzed,
        "file_blocks_generated": _n_blocks,
        "concepts_identified": analysis.get("concepts_identified", _n_concepts),
        "concepts_core": analysis.get("concepts_core", 0),
        "concepts_supporting": analysis.get("concepts_supporting", 0),
        "concepts_generated": max(analysis.get("concepts_generated", 0), _n_concepts),
        "entities_generated": max(analysis.get("entities_generated", 0), _n_entities),
        "coverage_core": analysis.get("coverage_core", 1.0),
        "coverage_supporting": analysis.get("coverage_supporting", 1.0),
        "coverage_pct": analysis.get("coverage_pct", 1.0),
        "images_extracted": stage_1_2_result.get("count", 0),
        "images_captioned": _total_captioned,
        "images_injected": stage_3_2_result.get("injected", 0),
        "queries_generated": max(query_count, _n_queries),
        "comparisons_generated": max(comp_count, _n_comps),
        "review_items": stage_3_4_result.get("items", 0),
    }
    _merged_stages = _preserve_stage_counters(_prev_stages, _new_stages)

    cache["entries"][rel] = {
        "hash": h,
        "timestamp": int(time.time() * 1000),
        "filesWritten": files_written_paths + index_log_files,
        "method": method,
        "template": template_name,
        "sourceHash": h,
        "fileBlockCount": _merged_stages["file_blocks_generated"],
        "stages": _merged_stages,
    }
    if hard_failures:
        print(f"  [cache] SKIPPED — {len(hard_failures)} hard failure(s)")
        return {"status": "hard-error", "hard_failures": hard_failures,
                "files_written": files_written_paths + index_log_files}
    try:
        save_cache(config, cache)
        clear_progress(config, h)
        print(f"  [cache] saved")
    except OSError as e:
        return {"status": "hard-error", "error": str(e),
                "files_written": files_written_paths + index_log_files}

    # Note: Detailed validation moved to separate 'validate' command (Phase 2 refactor)
    # Ingest now focuses on generation (Stages 0-3.5) with per-stage validation
    # For detailed quality checks, run: python3 validate.py <source_slug>
    # Stage 3.7 (embeddings) runs in the post-ingest section of ingest_one —
    # single entry point, mandatory attempt against local Ollama bge-m3
    # (prints an install reminder instead of silently skipping if unavailable).

    return {"status": "ok", "files_written": cache["entries"][rel]["filesWritten"]}
