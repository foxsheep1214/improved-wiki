# Scripts Reference — improved-wiki

Full script inventory by category. Entry points (user-facing) are **bold**.

| Category | Scripts |
|----------|---------|
| Core | `ingest.py`, `_core.py`, `_llm_api.py`, `_paths.py`, `_language.py`, `_frontmatter.py` |
| Stage Modules (Phase 0-3) | `_stage_1_extract.py` (1.1 facade → `_stage_1_1_scanned.py` / `_stage_1_2_images.py` / `_stage_1_3_caption.py`), `_stage_2_analyze.py` (2.2 + chunker), `_stage_2_3_incremental.py` (2.3: existing-wiki association detect), `_stage_2_4_generation.py` (2.4), `_dedup_intra_source.py` (2.4 dedup 收尾, ex-2.5), `_stage_2_6_source_page.py` (2.6: source page), `_stage_2_9_comparison.py` (2.9), `_stage_3_4_review.py` (3.4), `_stage_2_base.py` (公共导入), `_stage_3_write.py` (3.1 incl. page-merge, 3.5), `_stage_3_2_inject_images.py` (3.2), `_stage_3_7_embed.py` (3.7, final stage), `_stage_validators.py` (Stage 0 验证门 + StageValidationError) |
| Ingest orchestrator splits | `ingest.py` (CLI + `ingest_one`/`batch_ingest`) → `_ingest_skip.py` (Stage 0.2 去重/skip), `_ingest_chunks.py` (chunk 流水线), `_ingest_prepare.py` (综合/source page), `_ingest_write.py` (写盘 + post-ingest) |
| Merge/Enrich | `_enrich_wikilinks.py`, `_source_lifecycle.py` |
| Lint | **`wiki-lint.sh`**, `wiki-lint-fix.py` (--fix/--fix-links/--delete-orphans 执行器), `wiki-lint-semantic.py`, `lint_verify_semantic.py`（improved-wiki 独有：对 severity=="warning" 语义发现用全文再核验一遍，非 NashSU parity，lint 后手动跑），**`validate_ingest.py`**, `normalize_raw_names.py` |
| Graph | **`graph.py`** |
| Queue | `wiki-monitor.sh`, `run-queue.sh`, `run-ingest.sh` (手动包装：exit 101 handoff → exit 0，供把 handoff 当失败的 task UI 用) |
| Embeddings | **`build_embeddings.py`**, **`search_wiki.py`** |
| Repair | `sweep_reviews.py`, `enrich_wikilinks_retroactive.py`, `cross_source_dedup.py`（跨源去重 CLI，在用）, `rebuild_index.py`（index.md 确定性全量重建，不调 LLM，无页数上限；NashSU 0.6.4 `rebuild_wiki_index` parity，2026-07-16；与 Stage 3.5 的 LLM 版重写互补——后者只在 ingest 中跑且 ≤250 页） |
| Search | `search_local.py` (local source search for deep-research) |
| QC / Review guard | `qc_stage22.py` (Stage 2.2 响应离线质检), `review_fix_guard.py` |
| Lint internals | `_lint_suggest.py`, `_lint_fixes.py` |
| Dedup internals | `_dedup.py`, `_dedup_embedding.py`, `_dedup_storage.py` |
| Other internals | `_conversation_router.py`, `_llm_call.py`, `_frontmatter_array.py`, `_ingest_sanitize.py`, `_review_utils.py`, `_source_filter.py`, `_wiki_keyword.py`, `_context_probe.py`, `_watch.py` |
