# Stage 2.9 · Comparison Auto-Generation

> 把两个事物放在一起看才能看清各自特点。comparison 比 concept 高一层：concept 回答「X 是什么」，comparison 回答「X 和 Y 并置时各自优劣在哪里」。

## 阶段契约（与代码一致）

| 属性 | 值 |
|------|-----|
| 入口函数 | `stage_2_9_comparison_generation()`（`scripts/_stage_2_9_comparison.py`） |
| prompt 构建 | `_stage_2_9_build_prompt_disambiguation()`（2.9A）、`_stage_2_9_build_prompt_in_source()`（2.9B）—— **唯一真相源** |
| 执行位置 | `_ingest_prepare.py::_do_prepare`，顺序：2.7 query → 2.8 query 解析 → **2.9 comparison**（Phase 2 最后一步） |
| 输入 | `global_digest`、`chunk_analyses`、`file_blocks`（取已生成 concept/entity 标题）、`raw_file`、`config` |
| 输出 | `(comparison_blocks, combined_response)`；blocks 并入 `file_blocks`，由 Stage 3.1/3.2 统一写盘 |
| 跳过条件 | 本次 concept **和** entity 都为空（纯 stub source）时整体跳过 |
| 产物 | `wiki/comparisons/<slug>.md` 页面，或子标记 `---COMPARISONS_DISAMBIGUATION: 0---` / `---COMPARISONS_IN_SOURCE: 0---` |

> **代码现状**：本阶段跑 **2.9A + 2.9B** 两路自动子阶段。`combined_response = response_2.9A + "\n" + response_2.9B`。

---

## 设计原理

comparison 在本阶段自动生成两类：

| 子阶段 | 场景 | 触发门槛 |
|--------|------|---------|
| **2.9A** | 域内消歧义：新 concept/entity 名称与 wiki 已有页面**同名但不同 domain** | 总是运行 |
| **2.9B** | 源内概念对比：同源内两个天然适合对比的概念（如 CCM vs DCM） | `concept ≥ 2` 时才运行 |

---

## 2.9A · 域内消歧义（总是运行）

**目的**：当本次生成的 concept/entity 标题与 wiki 已有页面**精确同名**但属于不同 domain 时，创建消歧义页帮助区分。

**只在真正的跨域命名冲突时生成**，不为以下情况生成：

- 相似但不同的名字（"8b/10b encoding" vs "8b10b encoding bypass"）
- 只在单一 domain 存在的术语
- domain 区别已能从标题看出的
- 同一概念的子主题/变体

> 真正的冲突示例：`Switch` 在 circuit-fundamentals 和 power-electronics 两个 domain 含义不同。NashSU `domains.md` 规定消歧义页用 `type: comparison`。

### 输出 schema

```
---FILE:wiki/comparisons/{term-slug}.md---
---
type: comparison
title: "{Term} (disambiguation)"
domain: general
tags: [disambiguation]
related: [{各 domain 专属页面 stem}]
sources: []
created: {today}
updated: {today}
---

# {Term} (disambiguation)
（表格：Domain | Meaning | Page）
## How to Distinguish — 1-2 句：如何根据上下文判断属于哪个 domain
## See Also
---END FILE---
```

无需消歧义时输出：`---COMPARISONS_DISAMBIGUATION: 0---`。

---

## 2.9B · 源内概念对比（concept ≥ 2 才运行）

**目的**：从本次生成的 concept 中找出**天然适合对比理解**的概念对。`len(concept_titles) < 2` 时整段跳过。

| 适合 | 不适合 |
|------|--------|
| 同一维度的两种选择（CCM vs DCM、Buck vs Boost、Voltage/Current Mode） | 上下游关系（MOSFET → Gate Driver）→ 用 related 链接 |
| 经常被混淆的概念对（EMI vs EMC、SNR vs SINAD、PSRR vs CMRR） | 大类含子类（DC-DC → Buck）→ 用 related 链接 |
| 书中显式做了对比的概念对 | 三方及以上 → 不是 comparison |

**至多生成 2 个对比页**（宁缺毋滥）。

### 输出 schema

```
---FILE:wiki/comparisons/{slug}.md---
---
type: comparison
title: "{Concept A} vs {Concept B}"
domain: {current_domain}
tags: [{2-4 个标签}]
related: [{concept-A-stem}, {concept-B-stem}]
sources: ["raw/{相对路径}"]
created: {today}
updated: {today}
---

# {Concept A} vs {Concept B}
## Why Compare       — 1-2 句：为何并置理解
## Comparison Table  — 4 个维度（工作原理 / 关键特性 / 典型应用 / 优缺点）
## Selection Guide   — 何时选 A、何时选 B
## See Also
---END FILE---
```

无合适对比对时输出：`---COMPARISONS_IN_SOURCE: 0---`。

---

## go / no-go 判断

- **go**：生成消歧义页 + 源内对比页（合计 0-N 个 FILE block），或输出对应 `---COMPARISONS_*: 0---` 标记
- **no-go**：既无 comparison block 也无任一 `---COMPARISONS_*: 0---` 标记 → 2.9 未完成，重跑
- 每个 comparison frontmatter 含 `type: comparison` + `title:` + `domain:` 三必填字段

---

## 验证命令

```bash
# 本次 ingest 生成的 comparison 页
ls wiki/comparisons/*.md 2>/dev/null

# 消歧义页标记检查（title 含 "(disambiguation)"）
grep -l 'title:.*(disambiguation)' wiki/comparisons/*.md 2>/dev/null

# comparison 页面结构完整性
for f in wiki/comparisons/*.md; do
  grep -q "type: comparison" "$f" || echo "MISSING TYPE: $f"
  # 消歧义页有 How to Distinguish；对比页有 Comparison Table + Selection Guide
  grep -q "## How to Distinguish" "$f" && continue
  grep -q "## Comparison Table"   "$f" && grep -q "## Selection Guide" "$f" && continue
  echo "MISSING STRUCTURE: $f"
done
```

---

## 修订记录

- **2026-06-22**：彻底重写为结构化 spec，对齐代码 `stage_2_9_comparison_generation`。① 阶段编号 2.6 / 2.5A-C → **2.9 / 2.9A-B**；② 触发改为真实执行位置（2.8 之后，Phase 2 末步）；③ 修正输出标记 `COMPARISONS_INTERNAL` → 真实的 `COMPARISONS_DISAMBIGUATION` / `COMPARISONS_IN_SOURCE`；④ 消歧义页 schema 改为代码实际的英文 `(disambiguation)` + `tags:[disambiguation]`；⑤ 标注 2.9B 仅在 `concept ≥ 2` 时运行、至多 2 页；⑥ 删除未接线的 2.9C 跨源对比（连同臆想的人工触发 prompt 模板）——本阶段只做 A/B 两路。
- **2026-06-16**：初版。分离消歧义 / 源内对比 / 跨源对比三场景，对齐 NashSU `domains.md` 与 `type: comparison` schema。
