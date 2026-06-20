# Stage 2.5 · Query Auto-Generation

> **触发**：Stage 2.3.1（source/concept/entity generation）完成后自动执行。产物为内存中的 FILE blocks，由 Stage 3.2 统一写盘。。
> **跳过条件**：source 类型为 `datasheet` 或 `standard` 时自动跳过（纯事实罗列，不产生有意义的开放问题）。
> **产物**：0-5 个 `wiki/queries/<slug>.md` 页面，或 `---QUERIES: 0---` 标记。

---

## 设计原理

query 页面承载的是 **"这本书提出了什么它自己没回答的问题"**。它在单书 ingest 阶段即可生成——不需要跨源上下文。

query 是知识演化链中"从已知到未知"的第一跳：它把书中**隐含的认知边界**显式化为可追问的问题。

---

## 什么是好的 query？

三个条件：

1. **有根据** — 问题源于书中的具体内容（某个论断、案例、数据），不是凭空好奇
2. **可探索** — 这个问题可以通过阅读更多资料、实验、或深入分析来推进
3. **有边界** — 问题足够具体，不是"如何设计一个完美的电源"这种无限开放

### 反面示例（不生成）

| 问题 | 为什么不好 |
|------|-----------|
| "什么是电压？" | 书中已经完整回答了 |
| "如何学好硬件设计？" | 太宽泛，没有边界 |
| "未来AI会取代硬件工程师吗？" | 和这本书无关 |

### 正面示例（来自 HardwareWiki 已有 query）

| 问题 | 为什么好 |
|------|---------|
| "IPD 流程的核心价值是什么？" | 书中介绍了 IPD 但价值评估分散在各章 |
| "技术先进 vs 商业成功的平衡点在哪里？" | 书中给了案例但没给出通用框架 |
| "硬件流程中的需求变更如何管理？" | 书中提了原则但没给可操作的 checklist |

---

## Prompt 模板

```
你现在已经完成了《{title}》的消化分析（Stage 2.3.1），已经生成了 source/concept/entity 页面。请基于这些概念列表识别开放问题。。

现在请审视你从这本书中学到的所有内容，识别出书中**提出但未完全解答**的开放问题。

## 已有知识上下文

### Global Digest 摘要
{global_digest_summary}

### 已生成的 concept 页面
{generated_concept_titles}

### 已生成的 entity 页面
{generated_entity_titles}

### 书中关键论断
{key_claims_from_chunk_analyses}

## 什么是好的 query？

1. **有根据**：问题源于书中的具体内容，不是凭空好奇
2. **可探索**：可以通过阅读更多资料、实验、或深入分析来推进
3. **有边界**：问题足够具体，有明确的探索方向

## 反面示例（不要生成）

- "什么是电压？" ← 书中已经完整回答了
- "如何学好硬件设计？" ← 太宽泛
- "未来AI会取代硬件工程师吗？" ← 和这本书无关

## 正面示例

- "IPD 流程的核心价值是什么？" ← 书中介绍了 IPD 但价值评估分散在各章
- "技术先进 vs 商业成功的平衡点在哪里？" ← 书中给了案例但没给出通用框架
- "硬件流程中的需求变更如何管理？" ← 书中提了原则但没给 checklist

## 生成格式

为这本书生成 **0-5 个 query 页面**（宁缺毋滥，没有好问题就输出 0 个）。

每个 query 用 FILE block 格式输出：

---FILE:wiki/queries/{slug}.md---
---
type: query
title: "{以问号结尾的完整问题}"
domain: {domain}
tags: [{相关标签，2-4个}]
related: [{关联的 concept/entity 页面 wikilink stem，2-4个}]
sources: ["{source_title}"]
created: {today}
updated: {today}
---

# {问题标题}

## 问题背景

{2-3 句话：这个问题是从书中哪个具体内容引出的——引用章节、案例或数据}

## 书中已有的线索

{书中已经给出的部分答案、数据、案例，以 bullet points 列出，每条标注章节来源}

## 尚待探索

{书中没有回答的部分——用问句形式列出 2-4 个具体子问题}

## 参见

- [[相关概念页]] — 关系说明
- [[相关实体页]] — 关系说明
---END FILE---

## 约束

- slug 用英文 kebab-case，简短（3-6 个词），如 `ipd-core-value`、`tech-vs-business-balance`
- title 必须是一个完整的疑问句，以 `？` 或 `?` 结尾
- `related` 只引用本次 ingest 生成的页面 stem
- `sources` 只包含当前这本书
- 每个 query 必须 ≥200 字符（不含 frontmatter）
- 如果没有值得作为独立 query 的问题，输出：
  ---QUERIES: 0---
  （无值得独立成页的开放问题）
  ---END QUERIES---
```

---

## go/no-go 判断

- **go**：生成了 0-5 个 query FILE block，或 `---QUERIES: 0---` 标记
- **no-go**：未输出任何 query block 也未输出 `---QUERIES: 0---` 标记 → Stage 2.5 未完成，重跑
- 每个 query 的 frontmatter 包含 `type: query` + `title:` + `sources:` 三必填字段
- 每个 query body ≥200 字符

## 自动跳过条件

source 类型为 `datasheet` 或 `standard` 时自动跳过。这些类型是事实罗列（参数表、规范条文），不产生有意义的开放问题。

判断依据：`detect_template_type()` 返回值。

## 验证命令

```bash
# 检查本次 ingest 是否生成了 query 页面
grep -l "sources:.*{source_title}" wiki/queries/*.md 2>/dev/null | wc -l

# 检查 query 页面结构完整性
for f in wiki/queries/*.md; do
  grep -q "type: query" "$f" || echo "MISSING TYPE: $f"
  grep -q "## 问题背景" "$f" || echo "MISSING BACKGROUND: $f"
  grep -q "## 尚待探索" "$f" || echo "MISSING EXPLORATION: $f"
done
```

---

## 修订记录

- **2026-06-16**：初版。从 HardwareWiki 已有 3 个 query 页面反推模板，对齐 NashSU `type: query` schema。
