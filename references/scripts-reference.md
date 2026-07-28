# Scripts Reference — improved-wiki

Full script inventory by category. Entry points (user-facing) are **bold**.

| Category | Scripts |
|----------|---------|
| Core | `_config.py`, `_progress.py`, `_schema.py`, `_parse.py`, `_retry.py`, `_exit_codes.py`, `_paths.py`; `_core.py` is the compatibility facade |
| Stage Modules (Phase 0-3) | `_stage_1_extract.py` (1.1 facade → `_stage_1_1_scanned.py` / `_stage_1_2_images.py` / `_stage_1_3_caption.py`), `_stage_2_analyze.py` (2.2 + chunker), `_stage_2_3_incremental.py` (2.3: existing-wiki association detect), `_stage_2_4_generation.py` (2.4 key + schema-typed generation), `_dedup_intra_source.py` (2.4 dedup 收尾, ex-2.5), `_stage_2_6_source_page.py` (2.6: source page), `_stage_3_4_review.py` (3.4), `_stage_2_base.py` (shared Stage-2 domain helpers), `_stage_3_write.py` (3.1 incl. page-merge, 3.5), `_stage_3_2_inject_images.py` (3.2), `_stage_3_7_embed.py` (3.7, final stage), `_stage_validators.py` (Stage 0 验证门 + StageValidationError) |
| Ingest orchestration | **`ingest.py`** (compatibility facade) → `_ingest_cli.py` (CLI), `_ingest_runner.py` (single source), `_batch_supervisor.py` (prefetch + serial spine), `_batch_status.py` (read-only status), `_batch_coordination.py` (coordinator flock + durable reservation + prefetch pause), `_batch_worker_status.py` (heartbeat + lease), `_ingest_skip.py`, `_ingest_chunks.py`, `_ingest_prepare.py`, `_ingest_write.py` |
| Merge/Enrich | `_enrich_wikilinks.py`, `_source_lifecycle.py` |
| Lint | **`wiki-lint.sh`**, `wiki-lint-fix.py` (--fix/--fix-links/--delete-orphans 执行器), `wiki-lint-semantic.py`, `lint_verify_semantic.py`（improved-wiki 独有：对 severity=="warning" 语义发现用全文再核验一遍，非 NashSU parity，lint 后手动跑），**`validate_ingest.py`**, `normalize_raw_names.py` |
| Graph | **`graph.py`** |
| Queue | **`wiki-monitor.sh`**, **`run-queue.sh`** (thin launchers) → `queue_cli.py` (atomic scan/merge/run); `run-ingest.sh` maps exit 101 for task UIs |
| Embeddings | **`build_embeddings.py`**, **`search_wiki.py`** |
| Repair | `sweep_reviews.py`, `enrich_wikilinks_retroactive.py`, `cross_source_dedup.py`（跨源去重 CLI，在用）, `rebuild_index.py`（index.md 确定性全量重建，不调 LLM，无页数上限；NashSU 0.6.6 `rebuild_wiki_index` parity：按 frontmatter type 分组、完整相对路径、标题别名；与 Stage 3.5 的 LLM 版重写互补——后者只在 ingest 中跑且 ≤250 页） |
| Search | `search_local.py` (local source search for deep-research) |
| QC / Review guard | `qc_stage22.py` (Stage 2.2 响应离线质检), `review_fix_guard.py` |
| Lint internals | `_lint_suggest.py`, `_lint_fixes.py` |
| Dedup internals | `_dedup.py`, `_dedup_embedding.py`, `_dedup_storage.py` |
| Other internals | `_conversation_router.py`, `_llm_call.py`, `_frontmatter_array.py`, `_ingest_sanitize.py`, `_review_utils.py`, `_source_filter.py`, `_wiki_keyword.py`, `_context_probe.py`, `_watch.py` |
