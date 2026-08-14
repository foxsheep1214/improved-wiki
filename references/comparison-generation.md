# Comparison generation

`comparison` 是项目 `schema.md` 声明的普通 page type，与 `synthesis`、
`finding`、`thesis`、`methodology` 及自定义类型共用一条生命周期：

```text
Stage 2.2 schema_typed_candidates
→ Stage 2.3 existing-wiki association / dedup
→ Stage 2.4 unified grounded FILE generation
→ Stage 3.2 schema routing + write/merge
```

- Stage 2.2 只在来源真正展开了可复用的多维比较时推荐 comparison。
- Stage 2.4 使用候选中经 schema 重新解析后的 exact type/path；忽略 LLM
  自报 folder，并遵守 schema 的 frontmatter 与语义要求。
- 若 Stage 2.3 命中已有 `comparisons/<slug>`，它是 exact update target：
  Stage 2.4 仍输出该 FILE 路径，Stage 3.2 再按来源所有权执行替换或多来源
  merge；不能把“已有 comparison”解释成永久跳过。
- 与所有 key/schema-typed 页面相同：没有数量目标、最低数量或最高数量。
- 候选清单不再使用旧的 per-chunk 40 / all-chunks 120 展示截断。
- comparison 内容结构由来源和 schema 决定，不强制 Why Compare、四维表、
  Selection Guide、See Also 等固定小节。
- 零个 comparison 是正常结果，不需要 sentinel。
- source summary 只在 materially relevant 时链接 comparison，不强制追加
  “Comparisons” backlink 清单。

## 与 synthesis 的边界

- `comparison`：同一来源能实质支持，只要存在真实、多维、值得独立复用的
  对比即可。
- `synthesis`：按 NashSU bundled schema 是区别于 source summary 的跨主题
  综合/结论；一个来源在充分连接多个实质概念、finding、entity 或既有主题时
  可以建立初稿，后续来源继续 merge。不得把章节罗列冒充 synthesis，也不得
  从 index 标题杜撰其他来源证据。项目 schema 若要求多来源，则服从项目规则。

两者都由项目 `schema.md` 的最新规则裁决，不由目录名或页数配额裁决。
