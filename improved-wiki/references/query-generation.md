# Stage 2.7 · Query Auto-Generation — RETIRED (2026-07-12)

> **本阶段已整体移除**（NashSU parity 裁定，2026-07-12）。本文件保留为墓碑，
> 防止旧记忆/旧文档引用时误判功能仍在。

## 为什么移除

NashSU 的 ingest **从不生成 query 页**——其生成清单只有 source summary /
entities / concepts / index / log / overview + REVIEW 块。NashSU 中
`wiki/queries/` = "保存的聊天回答 + 研究"（README 原文），页面只来自用户主动行为：

1. **Deep Research** 结果（`deep-research.ts` → `wiki/queries/`）
2. **保存聊天回答**（`chat-message.tsx` save 路径）
3. **人工触发的 lint 断链 stub**（`lint-view.tsx` 单条 Fix / 勾选 Batch Fix）

improved-wiki 的 Stage 2.7（每本书自动生成 0-5 个"开放问题" query 页 +
跨源 query 解析收尾 + queries/index.md 维护）是无 NashSU 对应物的扩展通道，
产出的是**没有答案的空问题页**；NashSU 链路里 query 页诞生时就带着研究成果。

## 信号去哪了

"本书提出了值得研究的开放问题"这个信号**没有丢失**，改走 NashSU 原生通道：

```
ingest → Stage 3.4 REVIEW suggestion item（研究问题 + search_queries）
       → /improved-wiki process-reviews 人工裁决
           [Deep Research] → 研究结果落成 query 页（带答案）
           [Create Page]   → 手动建页
           [Skip]          → 关闭
```

Stage 3.4 的 suggestion 定义已同步扩充为 NashSU 措辞（"a research question,
source type, or comparison that would materially improve the wiki"）。

## 移除清单（代码考古用）

- `_stage_2_7_query_generation.py` / `_query_resolve_cross_source.py` — 删除
- `_ingest_prepare.py` — 2.7 调用 + 跨源解析 + `_stage_2_7_queries_index_block`
  + `query_count`/`query_resolutions` 缓存字段
- `_ingest_write.py` — `queries_generated` 统计
- `validate_ingest.py` — Stage 2.7 校验段
- 测试：`test_stage_2_7_skip.py` / `test_query_resolve_cross_source.py` /
  `test_query_digest_packer.py` 删除；`test_queries_index_backlinks.py` /
  `test_design_rulings_20260702.py` / `test_stratified_grounding.py` 修剪
- `stage_2_9_done` resume marker 名称保留（缓存兼容）

## 现在 query 页的三个来源

| 来源 | 文档 |
|------|------|
| Deep Research | `references/deep-research.md` |
| 保存聊天回答 | `references/save-chat-to-wiki.md` |
| Review 裁决（Deep Research / Create Page） | `references/process-reviews.md` |

已存在的 ingest 生成 query 页（RadarWiki/HardwareWiki 存量）保留不动——
它们是内容，不是机制。
