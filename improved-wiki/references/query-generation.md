# Stage 2.7 · Query Auto-Generation

> 从单本书的分析结果中，识别「这本书提出了但自己没回答的开放问题」，生成 `type: query` 页面。单源即可生成，无需跨源上下文。

## 阶段契约（与代码一致）

| 属性 | 值 |
|------|-----|
| 入口函数 | `stage_2_7_query_generation()`（`scripts/_stage_2_7_query_generation.py`） |
| prompt 构建 | `_stage_2_7_build_prompt()`（同文件，**唯一真相源**） |
| 执行位置 | `_ingest_prepare.py::_do_prepare`，顺序：2.6 源页生成 → **2.7 query**（生成 + 跨源 query 解析收尾，原 2.8 已并入；embedding 语义初筛 cosine≥0.82 + LLM judge，无回退） |
| 输入 | `global_digest`、`chunk_analyses`（取 claims）、`file_blocks`（取已生成 concept/entity 标题）、`raw_file`、`config` |
| 输出 | `(query_blocks, raw_response)`；query_blocks 并入 `file_blocks`，由 Stage 3.1/3.2 统一写盘 |
| LLM 调用 | 单次，`max_tokens = config.compute_max_tokens(4096)` |
| 产物 | 0-5 个 `wiki/queries/<slug>.md`，或 `---QUERIES: 0---` 标记 |

### 跳过条件（两个，任一命中即跳过）

1. **源类型为 `datasheet` 或 `standard`** —— 纯事实罗列（参数表、规范条文），不产生有意义的开放问题。判据：`detect_template_type(file_path, config)`。
2. **本次未生成任何 concept** —— `file_blocks` 中没有 `concepts/` 前缀的块。无概念则无从提问。

---

## 设计原理

query 页面是知识演化链中「从已知到未知」的第一跳：把书中**隐含的认知边界**显式化为可追问的问题。

它在单书 ingest 阶段即可生成——只依赖本书的 digest + 已生成的 concept/entity + chunk 论断，不需要其他源。

---

## 什么是好的 query？

三个条件同时满足：

1. **有根据（grounded）** — 源于书中具体内容（论断、案例、数据），不是凭空好奇
2. **可探索（explorable）** — 能通过阅读更多资料、实验或深入分析推进
3. **有边界（bounded）** — 足够具体，有明确探索方向

### 反面示例（不生成）

| 问题 | 为什么不好 |
|------|-----------|
| "什么是电压？" | 书中已完整回答 |
| "如何学好硬件设计？" | 太宽泛，没有边界 |
| "未来 AI 会取代硬件工程师吗？" | 与本书无关 |

### 正面示例（来自 HardwareWiki 已有 query）

| 问题 | 为什么好 |
|------|---------|
| "IPD 流程的核心价值是什么？" | 书中介绍了 IPD 但价值评估分散在各章 |
| "技术先进 vs 商业成功的平衡点在哪里？" | 书中给了案例但没给出通用框架 |
| "硬件流程中的需求变更如何管理？" | 书中提了原则但没给可操作 checklist |

---

## Prompt 结构

> ⚠️ **不在本文件复制 prompt 全文**。真实 prompt 由 `_stage_2_7_build_prompt()` 在运行时构建，以代码为准。历史上本文件曾内嵌一份中文 prose 模板，与代码的英文 prompt 长期漂移不一致——已于 2026-06-22 移除。

代码构建的 prompt 包含以下 section（按顺序）：

| Section | 内容 | 上限 |
|---------|------|------|
| `# Role` | 设定：刚为一本书生成完 source/concept/entity 页面 | — |
| `# Book Context` | 标题、规范 source 路径、Global Digest（YAML） | digest ≤3000 字符（超出截断） |
| `# Generated Concepts` | 本次生成的 concept 标题列表 | ≤80 |
| `# Generated Entities` | 本次生成的 entity 标题列表 | ≤40 |
| `# Key Claims` | 从 `chunk_analyses[].claims` 汇总 | ≤30 |
| `# Existing Wiki Pages` | 现有 slug（避免引用不存在的页面） | ≤200 |
| `# Task` + `# Output Format` + `# Constraints` | 任务说明 + FILE block 格式 + 约束 | — |

### 输出 schema（FILE block）

```
---FILE:wiki/queries/{slug}.md---
---
type: query
title: "{以 ? 或 ？ 结尾的完整问题}"
tags: [{2-4 个标签}]
related: [{2-4 个 wikilink stem，仅限本次生成的 concept/entity}]
sources: ["raw/{相对路径}"]
created: {today}
updated: {today}
---

# {问题标题}
## Background        — 2-3 句：问题由书中哪段内容引出
## Clues from the Book — 书中已有的部分答案/数据/案例，每条标章节来源
## To Explore        — 书中未答的 2-4 个具体子问题
## See Also          — [[相关概念页]] — 关系说明
---END FILE---
```

无值得独立成页的问题时，输出：`---QUERIES: 0---` … `---END QUERIES---`。

### 约束（代码内强制）

- `slug`：英文 kebab-case，3-6 个词
- `title`：完整疑问句，以 `?` 或 `？` 结尾
- `related`：**仅**本次 ingest 生成的 concept/entity stem
- `sources`：仅当前这本书
- 每个 query body ≥200 字符（不含 frontmatter）
- 直接以 `---FILE:` 或 `---QUERIES:` 开头，无前言

---

## go / no-go 判断

- **go**：生成 0-5 个 query FILE block，或输出 `---QUERIES: 0---` 标记
- **no-go**：既无 query block 也无 `---QUERIES: 0---` 标记 → 2.7 未完成，重跑
- 每个 query frontmatter 含 `type: query` + `title:` + `sources:` 三必填字段
- 每个 query body ≥200 字符

---

## 验证命令

```bash
# 本次 ingest 生成的 query 页数
ls wiki/queries/*.md 2>/dev/null | wc -l

# query 页面结构完整性
for f in wiki/queries/*.md; do
  grep -q "type: query"        "$f" || echo "MISSING TYPE: $f"
  grep -q "## Background"       "$f" || echo "MISSING BACKGROUND: $f"
  grep -q "## To Explore"      "$f" || echo "MISSING EXPLORE: $f"
done
```

---
