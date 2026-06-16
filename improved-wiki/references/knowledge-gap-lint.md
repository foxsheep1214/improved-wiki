# Knowledge Gap Lint — 高层知识空缺检测

> **归属**：lint 系统（非 ingest pipeline）。
> **触发**：每次 `wiki-lint` 运行时自动扫描，或在 ingest 完成后由 Stage 4.7 联动触发。
> **产物**：`wiki/REVIEW/<gap-type>/<date>-<source>-<short-slug>.md` review items（按空缺类型分子目录）。

---

## 设计原理

synthesis / finding / thesis / methodology 的形成不取决于单次 ingest 消化了什么——它们取决于**整个 wiki 的累积状态**。因此放在 per-ingest Stage 4.5 里跑是不对的：99% 的时候白算，而且语义不属于"本次 ingest 的产物质量审查"。

它们和 broken-link / orphan / missing-frontmatter 同质——都是 "wiki 结构不完整" 的信号。统一放到 lint 系统。

---

## 检测触发条件

| 页面类型 | 触发条件 | 说明 |
|---------|---------|------|
| **synthesis** | 同一 domain 下，≥5 个 concept 来自 ≥3 个不同 source | "power-electronics domain 已积累 8 个概念来自 4 本书，可以合成一篇领域综述" |
| **finding** | ≥3 条 key_claims 来自 ≥2 个不同 source，指向同一结论 | "3 本书都提到 '高频下 CCM 效率优于 DCM'，可以提炼为 finding" |
| **thesis** | 同一 domain 下已有 ≥2 个 finding | "power-electronics 已有 2 个 finding，可以提出一个可检验的工作假说" |
| **methodology** | 同一 domain 同时满足：① ≥1 个 synthesis 页 ② ≥5 个 concept 来自 ≥3 个 source ③ ≥2 个 finding ④ ≥1 个 comparison | "rf-microwave domain 已有综述、5+跨源概念、2 个发现、1 组对比——可以蒸馏出系统方法论" |

## 检测逻辑

```
每次 wiki-lint 运行时：

1. 扫描 wiki/concepts/*.md → 按 domain 分组，统计每个 domain 的 concept 数和 source 数
2. 扫描 wiki/findings/*.md → 按 domain 分组，统计 finding 数
3. 扫描 wiki/synthesis/*.md → 统计已有 synthesis 页
4. 扫描 wiki/comparisons/*.md → 统计已有 comparison 页

5. 对每个 domain，按优先级从低到高检查：
   a. synthesis:    concepts(domain) ≥5 且 distinct_sources ≥3 且 synthesis(domain) = 0 → 标记
   b. finding:      跨源 claims 指向同一结论 且 finding 页不存在 → 标记（LLM 辅助判断）
   c. thesis:       findings(domain) ≥2 且 thesis(domain) = 0 → 标记
   d. methodology:  synthesis(domain) ≥1 且 findings(domain) ≥2 且 comparisons(domain) ≥1
                    且 concepts(domain) ≥5(distinct_sources≥3) 且 methodology(domain) = 0 → 标记
```

## 数据来源

| 数据 | 来源 | 字段 |
|------|------|------|
| concept per domain | `wiki/concepts/*.md` | `domain` + `sources` frontmatter |
| finding count | `wiki/findings/*.md` | `domain` frontmatter |
| synthesis count | `wiki/synthesis/*.md` | `domain` frontmatter |
| comparison count | `wiki/comparisons/*.md` | `domain` frontmatter |
| claims cross-source | Stage 1.3 chunk analysis `claims` + concept 页面内容 | LLM 判断指向同一结论 |

## 输出格式

统一输出到 `wiki/REVIEW/<gap-type>/`，按知识空缺类型分子目录，和 lint 其他 finding 格式一致。

```yaml
# synthesis 建议
- id: N
  type: missing-page
  title: "建议生成 synthesis: {domain} 领域综述"
  description: |
    {domain} 领域当前已有 {N} 个概念页来自 {M} 个不同来源。
    建议合成一篇领域综述（wiki/synthesis/{domain}-overview.md）。
    关键词：{按重要性排列的 5-10 个关键概念}
    覆盖来源：{来源列表}
  affected_pages: [{相关 concept 列表}]
  severity: medium

# finding 建议
- id: N+1
  type: missing-page
  title: "建议提炼 finding: {结论简述}"
  description: |
    {M} 个来源（{source 列表}）的 claims 共同指向同一结论：「{结论}」。
    建议提炼为 finding 页面（wiki/findings/{slug}.md），标注证据链。
    支持的 claims：{逐条列出，标注来源和置信度}
  affected_pages: [{相关 concept 列表}]
  severity: medium

# thesis 建议
- id: N+2
  type: missing-page
  title: "建议提出 thesis: {假说方向}"
  description: |
    {domain} 领域已有 {F} 个 finding。基于这些发现，
    可以提出一个可检验的工作假说。
    可检验性问题：{如果 XX 成立，我们应该观察到 YY}
  affected_pages: [{相关 finding 列表}]
  severity: low

# methodology 建议
- id: N+3
  type: missing-page
  title: "建议蒸馏 methodology: {domain} 领域方法论"
  description: |
    {domain} 领域已满足方法论形成的全部条件：
    - 领域综述：{synthesis 页面}
    - 跨源概念：{N} 个概念来自 {M} 个来源
    - 实证发现：{F} 个 finding
    - 对比分析：{C} 个 comparison
    
    建议基于以上知识蒸馏出系统方法论（wiki/methodology/{slug}.md）。
    方法论应回答："基于我们已知的一切，在这个领域应该怎么做？"
    潜在方法论方向：{1-3 个具体方向}
  affected_pages: [{synthesis + findings + comparisons 列表}]
  severity: low
```

## methodology 的特殊性

- methodology 是知识演化链的最高一环（"基于所有已知，怎么做"），触发条件最苛刻
- 四个条件必须**全部满足**才触发建议——方法论不能凭空产生
- severity 设为 `low` 而非 `medium`：methodology 需要最成熟的知识生态，过早生成反而产生误导
- NashSU 没有 methodology 的自动触发逻辑，此条件为 improved-wiki 基于知识演化链设计

## 与 lint 系统集成

```
wiki-lint 检查项

结构健康（wiki-lint.sh）：
  ├─ broken-link
  ├─ orphan
  ├─ no-outlinks
  ├─ missing-frontmatter
  └─ invalid-domain

知识空缺（wiki-lint-semantic.py / LLM-driven）：
  ├─ missing-synthesis
  ├─ missing-finding
  ├─ missing-thesis
  └─ missing-methodology
```

所有 finding 按空缺类型写入 `wiki/REVIEW/<gap-type>/`（如 `missing-synthesis/`, `missing-methodology/`），人类打开 REVIEW 目录即知所有待处理事项。

## NashSU 对齐

NashSU 的 lint 系统在 UI 上通过 `missing-page` 类型提示用户"这里可以合成"。NashSU 没有 finding / thesis / methodology 的自动检测逻辑——这些是 improved-wiki 基于知识演化链的扩展。

## 修订记录

- **2026-06-17**：初版。从 Stage 4.5 拆分而来——高层知识空缺检测的语义属于 lint（wiki 整体健康）而非 per-ingest review（本次产物质量）。
