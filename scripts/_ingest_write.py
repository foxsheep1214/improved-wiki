"""_ingest_write.py — Stage 3+ file writing + post-ingest (extracted from ingest.py)."""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from _core import (
    Config,
    is_safe_ingest_path,
    list_existing_slugs,
    is_stage_done,
    get_stage_payload,
    mark_stage_done,
    load_cache,
    save_cache,
    clear_progress,
)
from _stage_3_write import (
    _stage_3_1_wiki_path_for_source,
    _stage_3_1_auto_correct_wiki_path,
    _stage_3_1_canonicalize_sources_field,
    _stage_3_1_stamp_frontmatter_dates,
    stage_3_1_write_wiki_file,
    stage_3_5_aggregate_repair,
)
from _stage_3_2_inject_images import stage_3_2_inject_images
from _stage_3_4_review import stage_3_4_review_suggestions
from _stage_validators import validate_stage_outputs
from _enrich_wikilinks import enrich_wikilinks_batch

def cleanup_resolved_reviews(config: Config) -> int:
    """Delete review pages whose frontmatter has `resolved: true`.

    Called at the start of each ingest run. Returns count of deleted pages.
    """
    reviews_dir = config.wiki_dir / "REVIEW"
    if not reviews_dir.exists():
        return 0

    removed = 0
    for f in sorted(reviews_dir.rglob("*.md")):
        if not f.suffix == ".md":
            continue
        content = f.read_text(encoding="utf-8")
        # Check frontmatter for resolved: true
        m = re.search(r'^resolved:\s*true\s*$', content, re.MULTILINE)
        if m:
            f.unlink()
            removed += 1
            print(f"[cleanup] Resolved review removed: {f.name}")

    if removed > 0:
        print(f"[cleanup] {removed} resolved review page(s) deleted")

    return removed

def _run_post_ingest_graph(config: Config) -> None:
    """Rebuild knowledge graph after ingest (once per session, stale-guarded).

    Controlled by AUTO_BUILD_GRAPH=1. The graph needs the full wiki state,
    but rebuilding after every book in a batch would be wasteful. Uses a
    staleness guard: skips if graph.json was rebuilt < 30 minutes ago.

    Mirrors NashSU's desktop app: the graph auto-refreshes when you view it.
    """
    if os.environ.get("AUTO_BUILD_GRAPH") != "1":
        return
    graph_script = Path(__file__).parent / "graph.py"
    if not graph_script.exists():
        print("[graph] graph.py not found — skipping")
        return

    # Staleness guard: don't rebuild more than once per 30 minutes
    graph_json = config.runtime_dir / "graph.json"
    if graph_json.exists():
        age_min = (time.time() - graph_json.stat().st_mtime) / 60
        if age_min < 30:
            print(f"[graph] Skipped — graph rebuilt {age_min:.0f}m ago (staleness guard)")
            return

    import subprocess
    print("[graph] Rebuilding knowledge graph...")
    try:
        result = subprocess.run(
            [sys.executable, str(graph_script)],
            cwd=config.wiki_root, capture_output=True, text=True, timeout=600,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n")[-3:]:
                print(f"[graph] {line.strip()}")
        else:
            print(f"[graph] Failed ({result.returncode}): {result.stderr[:200]}")
    except Exception as e:
        print(f"[graph] Failed ({e}) — continuing")

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
    raw_response = prepared["raw_response"]
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

    _VALID_SUBDIRS = {"sources", "concepts", "entities", "queries", "comparisons",
                      "synthesis", "findings", "thesis"}
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

    # Option A: stage-aware resume.  If the write phase (3.1 write loop +
    # enrichment + 3.2 inject + 3.3 collision) already completed in a prior
    # run, skip it entirely.  Re-running the write loop would spuriously fire
    # page-merge LLM round-trips because post-write steps (enrichment, image
    # injection) have mutated page bodies.  Restore file list from the marker.
    write_phase_done = is_stage_done(config, h, "write_phase")
    if write_phase_done:
        print("  [write] write_phase marker present — skipping 3.1/3.2/3.3")
        _wp = get_stage_payload(config, h, "write_phase")
        files_written_paths = _wp.get("files_written", [])
        source_block = ("source", "")  # source page already written
        hard_failures = []
        stage_3_2_result = {"injected": _wp.get("images_injected", 0)}
        stage_3_3_result = {"items": _wp.get("collision_items", 0)}
    _write_blocks = [] if write_phase_done else file_blocks

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

        full_path = config.wiki_dir / rel_path
        is_listing = basename in _LISTING_PAGES
        do_merge = full_path.exists() and not is_listing

        try:
            stage_3_1_write_wiki_file(full_path, content, config, merge=do_merge)
        except OSError as e:
            print(f"  [write] HARD ERROR: {rel_path} — {e}")
            hard_failures.append(rel_path)
            continue

        files_written_paths.append(str(full_path.relative_to(config.wiki_root)))
        if full_path == source_path:
            source_block = (rel_path, content)
        action = "[merge]" if do_merge else "[overwrite]" if is_listing and full_path.exists() else "[write]"
        print(f"  {action} {rel_path}")

        if enrich_enabled and not is_listing:
            enrich_candidates.append((rel_path, full_path))

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
            "domain: general",
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

        # Stage 3.3: Cross-domain slug collision review
        from _stage_3_write import stage_3_3_slug_collision_review
        stage_3_3_result = stage_3_3_slug_collision_review(
            file_blocks, prepared.get("current_domain", "general"), config, verbose=verbose)

        # Mark write phase complete so a post-review resume skips 3.1-3.3
        # (prevents spurious page-merge / re-enrichment / re-injection).
        mark_stage_done(config, h, "write_phase", payload={
            "files_written": files_written_paths,
            "images_injected": stage_3_2_result.get("injected", 0),
            "collision_items": stage_3_3_result.get("items", 0),
        })

    # Stage 3.4: Review (quality review of generated pages)
    stage_3_4_result = stage_3_4_review_suggestions(
        file_blocks, raw_file, config, raw_response=raw_response, verbose=verbose)

    # Go/no-go validation
    go_nogo_warnings = validate_stage_outputs(
        config, raw_file, method, extracted_text,
        stage_1_2_result, stage_1_3_result,
        file_blocks, source_path,
    )


    # Stage 3.5: Aggregate repair
    index_log_files = stage_3_5_aggregate_repair(source_path, raw_file, analysis, h, method, config)

    # Update cache
    try:
        rel = str(raw_file.relative_to(config.raw_root))
    except ValueError:
        rel = str(raw_file)
    cache = load_cache(config)
    cache["entries"][rel] = {
        "hash": h,
        "timestamp": int(time.time() * 1000),
        "filesWritten": files_written_paths + index_log_files,
        "method": method,
        "template": template_name,
        "sourceHash": h,
        "fileBlockCount": len(file_blocks),
        "stages": {
            "global_digest_keys": len(global_digest),
            "chunks_analyzed": len(chunk_analyses),
            "file_blocks_generated": len(file_blocks),
            "concepts_identified": analysis.get("concepts_identified", len(file_blocks)),
            "concepts_core": analysis.get("concepts_core", 0),
            "concepts_supporting": analysis.get("concepts_supporting", 0),
            "concepts_generated": analysis.get("concepts_generated", len(file_blocks)),
            "coverage_core": analysis.get("coverage_core", 1.0),
            "coverage_supporting": analysis.get("coverage_supporting", 1.0),
            "coverage_pct": analysis.get("coverage_pct", 1.0),
            "images_extracted": stage_1_2_result.get("count", 0),
            "images_captioned": stage_1_3_result.get("captioned", 0),
            "images_injected": stage_3_2_result.get("injected", 0),
            "queries_generated": query_count,
            "comparisons_generated": comp_count,
            "review_items": stage_3_3_result.get("items", 0),
        },
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

    # Stage 3.6: Quality scoring card (always runs; flags needs_review < 0.65)
    from _stage_3_6_quality import stage_3_6_quality
    _q_review = stage_3_3_result.get("items", 0)
    _q_stats = prepared.get("concept_merge_stats", (0, 0))
    _q_dedup_ran = prepared.get("dedup_was_run", False)
    quality_result = stage_3_6_quality(
        raw_file, config, extracted_text,
        stage_1_2_result.get("count", 0), stage_1_3_result.get("captioned", 0),
        file_blocks, _q_review, _q_stats, _q_dedup_ran, verbose=verbose)

    # Note: Detailed validation moved to separate 'validate' command (Phase 2 refactor)
    # Ingest now focuses on generation (Stages 0-3.5) with per-stage validation
    # For detailed quality checks, run: python3 validate.py <source_slug>
    # Stage 3.7 (embeddings) runs in the post-ingest section of ingest_one —
    # single entry point, mandatory attempt against local Ollama bge-m3
    # (prints an install reminder instead of silently skipping if unavailable).

    return {"status": "ok", "files_written": cache["entries"][rel]["filesWritten"]}
