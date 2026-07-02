# Stage 2.9 · Comparison Auto-Generation（源内对比）

> 把两个事物放在一起看才能看清各自特点。comparison 比 concept 高一层：concept 回答「X 是什么」，comparison 回答「X 和 Y 并置时各自优劣在哪里」。

> 同名 slug 的跨域碰撞在 Stage 3.1 写盘时走三层 page-merge 处理（frontmatter 数组 union + LLM body merge + locked 字段），不在此阶段生成消歧义页。

## 阶段契约（与代码一致）

| 属性 | 值 |
|------|-----|
| 入口函数 | `stage_2_9_comparison_generation()`（`scripts/_stage_2_9_comparison.py`） |
| prompt 构建 | `_stage_2_9_build_prompt_in_source()` —— **唯一真相源** |
| 执行位置 | `_ingest_prepare.py::_do_prepare`，顺序：2.7 query（含跨源解析收尾，原 2.8）→ **2.9 comparison**（Phase 2 最后一步） |
| 输入 | `global_digest`、`chunk_analyses`、`file_blocks`（取已生成 concept/entity 标题）、`raw_file`、`config` |
| 输出 | `(comparison_blocks, raw_response)`；blocks 并入 `file_blocks`，由 Stage 3.1/3.2 统一写盘 |
| 跳过条件 | 本次 concept **和** entity 都为空（纯 stub source），或 concept 数 <2（无对比对）时整体跳过 |
| 产物 | `wiki/comparisons/<slug>.md` 页面，或子标记 `---COMPARISONS_IN_SOURCE: 0---` |

---

## 设计原理

本阶段自动生成源内对比页：同源内**两个或多个**天然适合对比的概念，`concept ≥ 2` 时运行。既包括成对对比（如 CCM vs DCM），也包括源自身系统性 benchmark 的**多方对比（3+ 方）**（如 Phased-Array vs MIMO vs Phased-MIMO）。

跨域同名碰撞交给 Stage 3.1 写盘时的 page-merge 处理（NashSU parity）。

---

## 源内概念对比（concept ≥ 2 才运行）

**目的**：从本次生成的 concept 中找出**天然适合对比理解**的概念对。`len(concept_titles) < 2` 时整段跳过。

| 适合 | 不适合 |
|------|--------|
| 同一维度的两种选择（CCM vs DCM、Buck vs Boost、Voltage/Current Mode） | 上下游关系（MOSFET → Gate Driver）→ 用 related 链接 |
| 经常被混淆的概念对（EMI vs EMC、SNR vs SINAD、PSRR vs CMRR） | 大类含子类（DC-DC → Buck）→ 用 related 链接 |
| 书中显式做了对比的概念对 | 源中从未真正并置的无关概念堆砌 |
| **系统性多方对比（3+ 方）**：源自身在多个维度互相 benchmark 的方案（如 Phased-Array vs MIMO vs Phased-MIMO，跨 SINR/beampattern/sidelobe）。「新方法 vs 既有方案」类论文的**题眼页**，不因 3 方以上而跳过 | |

**至多生成 3 个对比页**（宁缺毋滥）。多方对比时表格加列、title/related/See Also 加项（A vs B vs C）。

### 输出 schema

```
---FILE:wiki/comparisons/{slug}.md---
---
type: comparison
title: "{Concept A} vs {Concept B}"
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

**小节标题语言（2026-07-02 用户裁决，D6）**：四个小节标题随内容语言走，两套**固定词表**（机器可解析，`_stage_2_9_headings()`）——中文源用 `为何对比 / 对比表 / 选型指南 / 参见`，英文源保持 `Why Compare / Comparison Table / Selection Guide / See Also`。任何 grep 这些标题的下游解析必须同时接受两套词表。

无合适对比对时输出：`---COMPARISONS_IN_SOURCE: 0---`。

---

## go / no-go 判断

- **go**：生成 0-N 个源内对比 FILE block，或输出 `---COMPARISONS_IN_SOURCE: 0---` 标记
- **no-go**：既无 comparison block 也无 `---COMPARISONS_IN_SOURCE: 0---` 标记 → 2.9 未完成，重跑
- 每个 comparison frontmatter 含 `type: comparison` + `title:` 两必填字段

---

## 验证命令

```bash
# 本次 ingest 生成的 comparison 页
ls wiki/comparisons/*.md 2>/dev/null

# comparison 页面结构完整性（标题两套词表都算合法：英文 / 中文，D6 裁决）
for f in wiki/comparisons/*.md; do
  grep -q "type: comparison" "$f" || echo "MISSING TYPE: $f"
  grep -Eq "## (Comparison Table|对比表)" "$f" && grep -Eq "## (Selection Guide|选型指南)" "$f" && continue
  echo "MISSING STRUCTURE: $f"
done
```

---
