# Scripts Reference — improved-wiki

Full script inventory by category. Entry points (user-facing) are **bold**.

| Category | Scripts |
|----------|---------|
| Core | `_config.py`, `_progress.py`, `_schema.py`, `_parse.py`, `_retry.py`, `_exit_codes.py`, `_paths.py`; `_core.py` is the compatibility facade |
| Stage Modules (Phase 0-3) | `_stage_1_extract.py` (1.1 facade → `_stage_1_1_scanned.py` / `_stage_1_2_images.py` / `_stage_1_3_caption.py`), `_stage_2_analyze.py` (2.2 + chunker), `_stage_2_3_incremental.py` (2.3: existing-wiki association detect), `_stage_2_4_generation.py` (2.4 unified generation), `_dedup_intra_source.py` (2.4 dedup tail), `_source_page.py` (source-page validation/fallback helpers), `_stage_3_review.py` (3.1 + 3.5), `_stage_2_base.py` (shared Stage-2 domain helpers), `_stage_3_write.py` (3.2 + 3.3), `_stage_3_4_inject_images.py` (3.4), `_stage_3_7_embed.py` (3.7, final stage), `_stage_validators.py` (go/no-go checks) |
| Ingest orchestration | **`ingest.py`** (compatibility facade) → `_ingest_cli.py` (CLI), `_ingest_runner.py` (single source), `_batch_supervisor.py` (prefetch + serial spine), `_batch_status.py` (read-only status), `_batch_coordination.py` (coordinator flock + durable reservation + prefetch pause), `_batch_worker_status.py` (heartbeat + lease), `_ingest_skip.py`, `_ingest_chunks.py`, `_ingest_prepare.py`, `_ingest_write.py` |
| Merge/Enrich | `_enrich_wikilinks.py`, `_source_lifecycle.py` |
| Lint | **`wiki-lint.sh`**（默认结构+语义+emit-review+fix+fix-links+sweep+一轮 dedup；exit 101 通过 `_lint_run_state.py` 续接同一次逻辑运行，语义完整扫描只做一次，Sweep 的 5 批上限跨续接累计；随后 exit 102 等用户确认 delete-orphans；确认后用 `--delete-orphans-only`；`--diagnostic-only`/`--structural-only` 为非修改模式）, `wiki-lint-fix.py`（修复/孤页删除执行器）, `wiki-lint-semantic.py`, `lint_verify_semantic.py`（improved-wiki 独有：对 severity=="warning" 语义发现用全文再核验一遍，非 NashSU parity，lint 后手动跑），**`validate_ingest.py`**, `normalize_raw_names.py` |
| Graph | **`graph.py`**（独立命令；Lint 不调用） |
| Queue | **`wiki-monitor.sh`**, **`run-queue.sh`** (thin launchers) → `queue_cli.py` (atomic scan/merge/run); `run-ingest.sh` maps exit 101 for task UIs |
| Embeddings | **`build_embeddings.py`**, **`search_wiki.py`** |
| Repair | `sweep_reviews.py`, `enrich_wikilinks_retroactive.py`, `cross_source_dedup.py`（跨源去重 CLI）, `rebuild_index.py`（index.md 确定性全量重建，不调 LLM，无页数上限；与 Stage 3.3 的 ingest 聚合修复互补） |
| Deep Research | `search_local.py`（v0.6.7 AnyTXT 的项目内 CLI analogue：1–3 queries、15 条全局上限、WebSearchResult JSON）, `write_research_page.py`（20 来源去重门禁、thinking 清理、确定性 query page 原子写入；不 auto-ingest） |
| QC / Review guard | `qc_stage22.py` (Stage 2.2 响应离线质检), `review_fix_guard.py` |
| Lint internals | `_lint_suggest.py`, `_lint_fixes.py` |
| Dedup internals | `_dedup.py`, `_dedup_embedding.py`, `_dedup_storage.py` |
| Other internals | `_conversation_router.py`, `_llm_call.py`, `_llm_api.py`（handoff/缓存底座）, `_frontmatter.py`（frontmatter 解析 + 三层 page merge）, `_frontmatter_array.py`, `_ingest_sanitize.py`, `_file_block_repair.py`（0.6.6 截断 FILE block 定向恢复）, `_media_integrity.py`（图片 sidecar 校验 + 定向重采集）, `_task_manifest.py`（任务清单同步）, `_page_ref.py`（页面引用归一）, `_queue_store.py`（队列持久化）, `_language.py`（语言检测 + 输出指令）, `_review_utils.py`, `_source_filter.py`, `_wiki_keyword.py`, `_context_probe.py`, `_watch.py` |
