# Stage 2.6 · Comparison Auto-Generation

> **触发**：Stage 2.5（query generation）完成后自动执行。产物为内存中的 FILE blocks，由 Stage 3.2 统一写盘。。
> **跳过条件**：本次无 concept 产出（纯 stub source）时自动跳过。
> **产物**：0-2 个 `wiki/comparisons/<slug>.md` 页面（消歧义 + 源内对比），或 `---COMPARISONS: 0---` 标记。

---

## 设计原理

comparison 页面承载 **"把两个事物放在一起看，才能看清各自的特点"**。它比 concept 页面高一层——concept 回答"X 是什么"，comparison 回答"X 和 Y 放在一起看，各自优劣在哪里"。

comparison 分三种触发场景：

| 场景 | 触发条件 | 执行方式 | 编号 |
|------|---------|---------|------|
| **域内消歧义** | 新 concept 名称与 wiki 中已有 concept 同名但不同 domain | 本次自动生成 | 2.5A |
| **源内概念对比** | 同一源内两个高度相关的概念天然适合对比（如 CCM vs DCM） | 本次自动生成 | 2.5B |
| **跨源对比** | 新 source 的概念与已有 wiki 概念有可比性 | 标记 suggestion，人工触发 | 2.5C |

---

## 2.5A · 域内消歧义

### 触发条件

本次生成的 concept/entity 中，有名称与 wiki 已有页面重名但属于不同 domain。

### NashSU 对齐

NashSU `domains.md` 规定：消歧义页面使用 `type: comparison`。

### Prompt 模板

```
你已经为《{title}》完成了 concept/entity 页面的生成（Stage 2.3.1 + Stage 2.5）。现在基于已生成的 concept 列表做对比分析。。

现在检查：本次生成的所有页面中，是否有名称与 **wiki 已有页面** 重名但属于不同 domain 的？

## 检查方法

1. 浏览本次所有 concept 和 entity 的 title
2. 将每个 title 与以下已有 wiki 页面列表比对：
{existing_pages_by_title_and_domain}
3. 如果 **title 相同** 且 **domain 不同** → 创建/更新消歧义页

## 已有消歧义页

{existing_disambiguation_pages}（如果存在，更新它们而非创建新的）

## 消歧义页格式

---FILE:wiki/comparisons/{term-slug}.md---
---
type: comparison
title: "{Term} (消歧义)"
domain: general
tags: [disambiguation, {相关标签}]
related: [{各领域页面 stem 列表}]
sources: []
created: {today}
updated: {today}
---

# {Term} (消歧义)

同名术语「{Term}」在 HardwareWiki 的不同领域有不同含义：

| 领域 | 含义 | 页面 |
|------|------|------|
| {domain-1} | {一句话定义} | [[{term}-{domain-1}]] |
| {domain-2} | {一句话定义} | [[{term}-{domain-2}]] |
{如有新增领域，追加行}

## 如何区分

{1-2 句话：根据上下文判断属于哪个领域}

## 参见

- [[{term}-{domain-1}]] — {说明}
- [[{term}-{domain-2}]] — {说明}
---END FILE---

## 约束

- 只生成/更新真正同名且不同域的消歧义页。不同名、同域的不需要。
- 如果已有消歧义页，在现有内容基础上**追加**新的 domain 行——不要重写整个页面。
- 如果本次没有需要消歧义的，跳过（不输出该 section）。
```

---

## 2.5B · 源内概念对比

### 触发条件

本次生成的 concept 中，有一对概念**天然适合对比理解**。

### 什么适合做 comparison？

| 适合 | 不适合 |
|------|--------|
| 同一维度的两种选择（CCM vs DCM、Buck vs Boost） | 上下游关系（MOSFET → Gate Driver） |
| 经常被混淆的概念对（EMI vs EMC、SNR vs SINAD） | 大类包含子类（DC-DC Converter → Buck） |
| 书中显式做了对比的概念对（铱星 vs 小灵通） | 三方及以上的多方对比 → 用 synthesis |

### Prompt 模板

```
现在检查本次为《{title}》生成的 concept 页面。是否有一对概念**天然适合对比理解**？

## 什么适合做 comparison？

- 同一维度的两种选择（如 CCM vs DCM、Buck vs Boost、Voltage Mode vs Current Mode）
- 经常被混淆的概念对（如 EMI vs EMC、SNR vs SINAD、PSRR vs CMRR）
- 书中显式做了对比的概念（如"铱星 vs 小灵通"）

## 什么不适合？

- 上下游关系（如"MOSFET → Gate Driver"）← 用 related 链接
- 大类包含子类（如"DC-DC Converter → Buck Converter"）← 用 related 链接
- 三方及以上的多方对比 ← 用 synthesis 页面，不用 comparison

## 本次生成的 concept 列表

{generated_concept_titles_with_short_descriptions}

## 生成格式

每个对比生成一个 FILE block：

---FILE:wiki/comparisons/{slug}.md---
---
type: comparison
title: "{概念A} vs {概念B}"
domain: {domain}
tags: [{标签，2-4个}]
related: [{概念A stem}, {概念B stem}]
sources: ["{source_title}"]
created: {today}
updated: {today}
---

# {概念A} vs {概念B}

## 为什么需要对比

{1-2 句话：这两个概念为什么适合放在一起理解——它们解决了同一个问题的不同侧面}

## 对比表

| 维度 | {概念A} | {概念B} |
|------|---------|---------|
| {维度1：如工作原理} | | |
| {维度2：如关键特性} | | |
| {维度3：如典型应用} | | |
| {维度4：如优缺点} | | |

## 选择指南

{什么时候选 A，什么时候选 B——2-3 点具体建议}

## 参见

- [[{概念A}]] — {一句话说明}
- [[{概念B}]] — {一句话说明}
---END FILE---

## 约束

- 最多输出 **2 个**对比页（宁缺毋滥）
- 对比维度 **≥ 4**（太少说明不适合做独立页）
- 每个对比 body ≥300 字符
- 如果没有好的对比对，输出 `---COMPARISONS_INTERNAL: 0---`
```

---

## 2.5C · 跨源对比（仅标记，不自动生成）

### 触发条件

新 source 的 concept 与已有 wiki concept 存在可比性。

### 为什么不能自动生成？

跨源对比需要读取**两个不同 source 的完整 concept 页面**，而非仅凭 digest 摘要。token 消耗大，且判断"是否值得对比"需要人工 review。因此本场景不作为自动生成，而是作为 suggestion 写入 review。

### 标记格式

在 Stage 2.10 review 中追加 `type: missing-page` review item：

```yaml
- id: N
  type: missing-page
  title: "建议跨源对比: {concept-new} vs {concept-existing}"
  description: |
    新源《{new-source-title}》中的「{concept-new}」与已有页面
    [[{concept-existing}]]（来自《{existing-source-title}》）涉及同一主题。
    建议生成跨源对比页。

    潜在对比维度：{1-2 个具体维度}

    新源的关键观点：{摘要}
    已有源的关键观点：{摘要}
  affected_pages:
    - concepts/{concept-new}.md
    - concepts/{concept-existing}.md
  severity: medium
```

### 人工触发后的 Prompt 模板

```
现在有两份来自不同来源的知识，它们涉及同一个主题「{topic}」。请生成跨源对比页。

## 来源 A

- 源页：[[{source-A-stem}]]
- 作者/年份：{source-A-meta}
- 关键内容（来自 concept 页面 [{concept-A}]）：
{concept-A-full-body}

## 来源 B

- 源页：[[{source-B-stem}]]
- 作者/年份：{source-B-meta}
- 关键内容（来自 concept 页面 [{concept-B}]）：
{concept-B-full-body}

## 生成格式

---FILE:wiki/comparisons/{slug}.md---
---
type: comparison
title: "{topic}: {source-A-short} vs {source-B-short}"
domain: {domain}
tags: [cross-source, {标签}]
related: [{concept-A-stem}, {concept-B-stem}, {source-A-stem}, {source-B-stem}]
sources: ["{source-A-title}", "{source-B-title}"]
created: {today}
updated: {today}
---

# {topic}: 跨源对比

## 来源

| | 来源 | 作者 | 年份 |
|---|------|------|------|
| A | {source-A-title} | {author} | {year} |
| B | {source-B-title} | {author} | {year} |

## 对比表

| 维度 | A: {source-A-short} | B: {source-B-short} |
|------|---------------------|---------------------|
| {维度1} | | |
| {维度2} | | |
| {维度3} | | |
| {维度4} | | |

## 异同分析

### 一致点

{两源观点一致的内容——意味着这些是行业共识}

### 分歧点

{两源观点不同的内容——意味着存在争议或不同设计哲学}

### A 的独特贡献

{只有 A 讲到的内容}

### B 的独特贡献

{只有 B 讲到的内容}

## 综合判断

{对两源观点的整合评价：哪个更可靠？哪个更现代？各自适用什么场景？}

## 参见

- [[{concept-A-stem}]] — {说明}
- [[{concept-B-stem}]] — {说明}
- [[{source-A-stem}]] — 来源 A
- [[{source-B-stem}]] — 来源 B
---END FILE---

## 约束

- 对比维度 ≥ 4
- body ≥ 400 字符
- 必须标注两源的分歧点（如果没有分歧，说明这两源是重复的，不值得对比）
```

---

## go/no-go 判断

- **go**：生成了消歧义页 + 源内对比页（合计 0-2 个 FILE block），或输出 `---COMPARISONS: 0---` 标记
- **no-go**：未输出任何 comparison block 也未输出 `---COMPARISONS: 0---` 标记 → Stage 2.3.5 未完成，重跑
- 每个 comparison 的 frontmatter 包含 `type: comparison` + `title:` + `domain:` 三必填字段

## 验证命令

```bash
# 检查本次 ingest 是否生成了 comparison 页面
grep -l "sources:.*{source_title}" wiki/comparisons/*.md 2>/dev/null

# 检查消歧义页的 domain 字段
grep -l "domain: general" wiki/comparisons/*.md 2>/dev/null | while read f; do
  grep -q "消歧义" "$f" || echo "MISSING DISAMBIGUATION MARKER: $f"
done

# 检查对比页结构完整性
for f in wiki/comparisons/*.md; do
  grep -q "type: comparison" "$f" || echo "MISSING TYPE: $f"
  grep -q "## 对比表" "$f" && grep -q "## 选择指南" "$f" && continue
  grep -q "## 异同分析" "$f" && grep -q "## 综合判断" "$f" && continue
  grep -q "## 如何区分" "$f" && continue
  echo "MISSING STRUCTURE: $f"
done
```

---

## 修订记录

- **2026-06-16**：初版。分离 4.3A（消歧义）、4.3B（源内对比）、4.3C（跨源对比，仅标记）。对齐 NashSU `domains.md` 消歧义规则和 `type: comparison` schema。
