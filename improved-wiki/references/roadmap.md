# Roadmap — planned enhancements (not yet built)

Future features. Distinct from `known-issues.md` (bugs/workarounds): these are
capabilities that don't exist yet and are intentionally deferred.

---

## TODO: 高级知识合成命令(Synthesize)—— 跨源/跨页 synthesis / thesis 自动生成

**优先级:** 低-中(能力增强,非阻断;ingest 主链质量已达标)。
**状态:** 未开始。调研已完成(2026-07-02)。

### 现状 / gap(2026-07-02 调研确认)

- `comparison` 仅**源内**(Stage 2.9,只用当前书自己的概念);`finding` / `methodology` /
  `thesis` / `synthesis` 只走 Stage 2.2 的 `schema_typed_candidates` —— 仅当**单一源的单个
  chunk 字面包含**该内容时才生成。
- **跨源/跨页的 `synthesis` / `thesis`(把多本书、多页合成一个新结论或假设)目前没有任何
  自动机制。** 全库唯一的跨源工具 `cross_source_dedup.py` 只合并去重、不造新页;`schema.md`
  写了"证据够了在 synthesis 页里解决矛盾"但**无代码实现,纯手工**。
- NashSU 同样没有:其唯一真合成器 `deep-research.ts` 合成的是**新抓的外部 web 结果**(不是
  已有 wiki 页),且手动触发。→ 这是整个 Karpathy 范式的共同边界,**不是 bug**。

### 参考实现(GitHub 调研)

- **`microsoft/graphrag`**(工业标杆):Leiden 层次社区 → 每个社区用 LLM 生成 *community
  report*(摘要)→ 全局查询对社区摘要做 map-reduce。系统化的重型版。
- **`Ar9av/obsidian-wiki` 的 `wiki-synthesize` skill**(轻量版):扫全库建**共现矩阵**(有多少
  页同时链接 A 和 B)→ 打分(共现数 / 跨域 / 共标签 / 是否 hub / 能否消解矛盾)→ 给 top-N
  让 LLM 写 `synthesis/` 页。**这正是 improved-wiki 缺的那一步。**

### 建议方案

- 作为 **Ingest / Lint / Graph 之外的第 4 个 peer 命令**(如 `python3 "$SKILL_DIR/scripts/synthesize.py"`),
  **不塞进 ingest** —— 保持"ingest 只做单源忠实抽取"的 NashSU 对齐;手动/周期触发,非每次 ingest。
- **复用现有 `graph.py` 的 Louvain 社区 + 共现信号**:结构已经算好了(社区 / knowledge-gaps /
  surprising-connections),只差"社区 or 高共现对 → LLM 综述成页"这最后一步。
- 流程:① 从 graph 取社区 / 高共现对 → ② dedup vs 已有 `synthesis/` 页 → ③ 打分排序(共现、
  跨域、跨源、能否消解 REVIEW 里挂着的矛盾)→ ④ LLM 为 top-N 写 `synthesis` / `thesis` 页
  (grounded 到贡献页,`related:` 引全部来源)→ ⑤ 对新页 `auto-ingest`(拆 concept/entity +
  建 wikilink)。
- 输出类型:`synthesis`(跨切结论)/ `thesis`(假设及其演化)—— 两类目录 `schema.md` 已定义。

### 落地时要注意

- 忠实性门禁:综述必须 grounded 到具体贡献页,禁止杜撰(沿用 ingest 的 no-fabrication 政策)。
- 幂等 + 去重:重跑不得对同一社区/概念对重复造页(参考 review 的 content-stable id 思路)。
- 与 lint/graph 的关系:Synthesize 是 Graph 的下游消费者(读社区),但独立命令,不自动触发。
