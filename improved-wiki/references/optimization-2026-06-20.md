# Ingest Pipeline 优化实现计划（2026-06-20）

## 🎯 新增 4 个 Stage

### Stage 2.2.1 · Incremental Association Detection
- **模块**：`_stage_2_2_1_incremental.py`
- **作用**：检测新源 concept/entity 与 wiki 已有页面的关联
- **跳过条件**：wiki 为空

### Stage 2.3.1 · Concept Dedup & Merge
- **模块**：`_stage_2_3_1_dedup.py`
- **作用**：对同一本书内的概念去重合并
- **跳过条件**：单 chunk 书
- **关键**：防止重复概念页面

### Stage 2.5.1 · Cross-source Query Resolution
- **模块**：`_stage_2_5_1_query_resolve.py`
- **作用**：自动关闭已有答案的 query
- **跳过条件**：Stage 2.5 无 query 或 wiki 为空
- **关键**：减少冗余 query，自动关联知识

### Stage 3.4.1 · Quality Scoring Card
- **模块**：`_stage_3_4_1_quality.py`
- **作用**：量化 ingest 质量（0.0-1.0）
- **跳过条件**：无（总是执行）
- **输出**：overall_score，needs_review flag

## 📋 集成任务清单

### 需要修改 ingest.py：

1. **Stage 2.2.1**（在 chunk_analyses 完成后）
   ```python
   from _stage_2_2_1_incremental import detect_incremental_associations
   associations = detect_incremental_associations(wiki_root, chunk_analyses)
   checkpoint["incremental_associations"] = associations
   ```

2. **Stage 2.3.1**（在 file_blocks 生成后）
   ```python
   from _stage_2_3_1_dedup import extract_concept_blocks, find_duplicate_concepts, apply_merge_rules
   concepts = extract_concept_blocks(file_blocks)
   duplicates = find_duplicate_concepts(concepts)
   merge_rules = generate_merge_rules(concepts, duplicates)
   checkpoint["concept_merge_rules"] = merge_rules
   file_blocks = apply_merge_rules(file_blocks, merge_rules)
   ```

3. **Stage 2.5.1**（在 query blocks 生成后）
   ```python
   from _stage_2_5_1_query_resolve import extract_query_blocks, resolve_queries, update_file_blocks_after_resolution
   queries = extract_query_blocks(file_blocks)
   resolutions = resolve_queries(file_blocks, wiki_root, queries)
   checkpoint["query_resolutions"] = resolutions
   file_blocks = update_file_blocks_after_resolution(file_blocks, resolutions)
   ```

4. **Stage 3.4.1**（在 aggregate repair 完成后）
   ```python
   from _stage_3_4_1_quality import calculate_quality_score, generate_quality_card_md
   quality_result = calculate_quality_score(...)
   checkpoint["quality_metrics"] = quality_result
   if quality_result["needs_review"]:
       # 写入 wiki/lint/audit/ 文件
   ```

## 📊 质量评分维度（Stage 3.4.1）

- 文本覆盖 (25%)：extracted_chars / original_chars
- 图片质量 (20%)：extracted_images / expected_images
- 概念密度 (25%)：concept_count / (text_kb * 3)
- Review 质量 (20%)：1 - (review_items / file_blocks)
- 去重完整性 (10%)：concepts_after / concepts_before

**overall_score < 0.65 时标记为 needs_review**

## 📁 新文件清单

✅ 已创建：
- `_stage_2_2_1_incremental.py` - 增量关联检测
- `_stage_2_3_1_dedup.py` - 概念去重合并
- `_stage_2_5_1_query_resolve.py` - 跨源查询解析
- `_stage_3_4_1_quality.py` - 质量评分卡

⏳ TODO：
- 在 ingest.py 中集成以上 4 个 stage
- 添加验证函数 `_verify_stage_*`
- 端到端测试

## 🔄 执行流程

```
2.2 → 2.2.1(增量关联) → 2.3(利用关联优化生成) → 2.3.1(去重) → 
2.4 → 2.5 → 2.5.1(查询解析) → 2.6 → 3.1-3.3 → 
3.4 → 3.4.1(质量评分) → [3.5] → 4.1
```

---

## 预期效果

| 优化 | 效果 |
|------|------|
| 概念去重 | 减少 15-25% 重复页面 |
| 增量学习 | 减少 10-20% 孤儿页面 |
| 跨源查询 | 减少 20-30% 冗余 query |
| 质量评分 | 快速识别问题 ingest |

