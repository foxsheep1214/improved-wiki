# Roadmap — planned enhancements (not yet built)

Future features. Distinct from `known-issues.md` (unresolved limitations): these are
capabilities that don't exist yet and are intentionally deferred.

---

## TODO: 高级知识合成命令(Synthesize)—— 跨源/跨页 synthesis / thesis 自动生成

**优先级:** 低-中(能力增强,非阻断;ingest 主链质量已达标)。
**状态:** 未开始。调研已完成(2026-07-02)。

### 现状 / gap（2026-08-03 按 NashSU 0.6.7 更新）

- 普通 ingest 已把 `comparison` / `synthesis` / `finding` / `methodology` /
  `thesis` 统一接入 Stage 2.2 `schema_typed_candidates` → Stage 2.4 FILE
  generation。候选可使用滚动 digest 与冻结的
  `wiki/index.md` 快照识别已有 living page，但正文仍必须由当前输入的真实
  来源证据支撑。按 NashSU bundled schema，普通来源可建立 cross-cutting
  synthesis 初稿或 speculative working thesis，后续来源再 merge/update；
  项目 schema 可以声明更严格门槛。
- Deep Research 页本身整合多个真实来源，但 v0.6.7 直接保留这一张 query
  page（仅可选单页 embedding），不再 auto-ingest，也不会自动派生
  synthesis/thesis。若未来 Synthesize 命令要复用研究证据，应直接读取已保存
  的 grounded query page，而不是依赖二次 ingest。
- **仍缺少的是主动扫描既有 wiki 多页/多书、再自动创造新的 synthesis /
  thesis 的机制。** `cross_source_dedup.py` 只合并去重；普通单源 ingest
  不会读取整库正文来主动做新结论。
- NashSU 0.6.7 同样没有既有 wiki 的自动全库合成器；`deep-research.ts`
  合成的是用户主动触发后取得的多来源研究结果。这个 roadmap 项仍是新增
  peer capability，不是 ingest 对齐缺陷。

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
  (grounded 到贡献页,`related:` 引全部来源)→ ⑤ 确定性写盘并按配置做单页 embedding；
  不把生成页再 auto-ingest 成第二轮摘要/Review。
- 输出类型:`synthesis`(跨切结论)/ `thesis`(假设及其演化)—— 两类目录 `schema.md` 已定义。

### 落地时要注意

- 忠实性门禁:综述必须 grounded 到具体贡献页,禁止杜撰(沿用 ingest 的 no-fabrication 政策)。
- 幂等 + 去重:重跑不得对同一社区/概念对重复造页(参考 review 的 content-stable id 思路)。
- 与 lint/graph 的关系:Synthesize 是 Graph 的下游消费者(读社区),但独立命令,不自动触发。
