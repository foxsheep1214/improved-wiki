# Scripts Reference — improved-wiki

Full script inventory by category. Entry points (user-facing) are **bold**.

| Category | Scripts |
|----------|---------|
| Core | `ingest.py`, `_core.py`, `_llm_api.py`, `_paths.py`, `_language.py`, `_frontmatter.py` |
| Stage Modules (Phase 0-3) | `_stage_1_extract.py` (1.1 facade → `_stage_1_1_scanned.py` / `_stage_1_2_images.py` / `_stage_1_3_caption.py`), `_stage_2_analyze.py` (2.1-2.2), `_stage_2_3_incremental.py` (2.3: existing-wiki association detect), `_stage_2_4_generation.py` (2.4), `_stage_2_5_dedup.py` (2.4 dedup 收尾, ex-2.5), `_stage_2_6_source_page.py` (2.6: source page), `_stage_2_7_query_generation.py` (2.7), `_stage_2_8_query_resolve.py` (2.7 resolve 收尾, ex-2.8), `_stage_2_9_comparison.py` (2.9), `_stage_3_4_review.py` (3.4), `_stage_2_base.py` (公共导入), `_stage_3_write.py` (3.1 incl. page-merge, 3.5), `_stage_3_2_inject_images.py` (3.2), `_stage_3_7_embed.py` (3.7, final stage), `_stage_validators.py` (Stage 0 验证门 + StageValidationError) |
| Ingest orchestrator splits | `ingest.py` (CLI + `ingest_one`/`batch_ingest`) → `_ingest_skip.py` (Stage 0.2 去重/skip), `_ingest_chunks.py` (chunk 流水线), `_ingest_prepare.py` (综合/source page), `_ingest_write.py` (写盘 + post-ingest) |
| Merge/Enrich | `_enrich_wikilinks.py`, `_source_lifecycle.py` |
| Lint | **`wiki-lint.sh`**, `wiki-lint-semantic.py`, **`validate_ingest.py`**, `validate-frontmatter.sh`, `normalize_raw_names.py` |
| Graph | **`graph.py`** |
| Queue | `wiki-monitor.sh`, `run-queue.sh` |
| Embeddings | **`build_embeddings.py`**, **`search_wiki.py`** |
| Repair | `sweep_reviews.py`, `enrich_wikilinks_retroactive.py`, `cross_source_dedup.py`（跨源去重 CLI，在用）；一次性修复脚本已退役 → `archive/scripts/` |
| Search | `search_local.py` (local source search for deep-research) |
| Review guard | `review_fix_guard.py` |
| Lint internals | `_lint_suggest.py`, `_lint_fixes.py` |
| Dedup internals | `_dedup.py`, `_dedup_embedding.py`, `_dedup_storage.py` |
| Other internals | `_conversation_router.py`, `_llm_call.py`, `_frontmatter_array.py`, `_ingest_sanitize.py`, `_review_utils.py`, `_source_filter.py`, `_wiki_keyword.py`, `_context_probe.py`, `_watch.py` |
