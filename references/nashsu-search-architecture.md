# NashSU 搜索与关联架构（源码实证）

> 2026-07-29 以本地 `llm_wiki-0.6.6` 的
> `src/lib/embedding.ts`、`src/lib/text-chunker.ts`、
> `src-tauri/src/commands/search.rs` 与 `vectorstore.rs` 重新逐行核对。
> 用于澄清 improved-wiki "NashSU parity" 声称在搜索/检索侧的实际含义。

## 两条独立的"语义关联"路径

NashSU 把"找关联页面"拆成两个完全独立的系统：

### 路径 1：知识图谱关联（graph-relevance.ts）— 纯确定性，无模型

源文件：`src/lib/graph-relevance.ts`

4 个信号算两个页面的关联度，权重与 improved-wiki graph.py 一致（2026-06-29 逐行移植）：

| 信号 | 权重 | 含义 |
|------|------|------|
| direct link | ×3.0 | [[wikilinks]] 互链 |
| source overlap | ×4.0 | 引用了同一个 raw 源文件 |
| Adamic-Adar | ×1.5 | 共同邻居的图拓扑度量（1/log(degree)） |
| type affinity | ×1.0 | 页面类型亲和矩阵（entity↔concept 1.2, concept↔synthesis 1.2 等） |

计算过程：读 wiki/*.md → 提取 frontmatter (title/type/sources) + wikilinks → 构建 RetrievalGraph → calculateRelevance 逐对打分 → getRelatedNodes 排序返回 top-N。

**无 LLM、无 embedding、无模型调用**。纯文件读写 + 数学计算。

improved-wiki 的 `graph.py` 参考当时的 NashSU 实现（2026-06-29 重写；早期版本把四信号都拿来造边，是当时最大偏离，已修正）：
- **边 = 链接关系**（`[[wikilinks]]` + 本 wiki 的 `related:` frontmatter 约定）；source-overlap / Adamic-Adar **只作边权重**，不再造边。
- **双图**（NashSU 实际架构）：retrieval graph（含 query 页）供 `calculateRelevance` 的 Adamic-Adar 邻居/度数打分；display graph（删 query 页）供节点/边/社区/渲染。
- **calculateRelevance 逐行移植**：direct=(正+反)×3.0、source=共享源数×4.0、AA=Σ 1/log(max(deg,2))×1.5（无阈值）、type-affinity 用 0.5.3 原矩阵（默认 0.5）×1.0 无条件叠加。
- **社区/洞察对齐**：cohesion = intra/(n(n-1)/2) 密度；`detectKnowledgeGaps`（isolated=linkCount≤1、稀疏社区、邻居跨≥3 社区的 bridge，无 betweenness）；`findSurprisingConnections` 已移植；`graph-filters` 默认隐藏结构页 + 删 query 类型。

**有意的 CLI 偏离**（非缺口，已记录）：Louvain 固定 `seed=42`（NashSU 不设种子，CLI 需可复现）；`related:` 作链接来源（本 wiki 约定，NashSU 页面无此字段）；落盘产物 `<runtime>/graph.json` / 自包含 `<runtime>/graph.html` / `.llm-wiki/knowledge-gaps.md` / `wiki/clusters/*`（NashSU 仅 app 内渲染）；建图排除 wiki artifact 目录 `GRAPH_SKIP_DIRS={REVIEW,clusters,media,lint}`；`--mode query` 排除已链接页（“建议新链接”语义）。

### 路径 2：搜索检索（Rust 后端 search.rs）— 混合 keyword + vector

源文件：`src-tauri/src/commands/search.rs`（Rust），前端入口 `src/lib/search.ts`（TypeScript）

improved-wiki 用 Python + LanceDB 实现同一数据与排序契约；语言/运行时不同，
但 chunk、索引更新、page aggregation 与 hybrid fusion 行为已对齐。

#### 搜索流程

```
用户查询 → tokenize → [keyword 搜索 wiki/*.md] + [vector 搜索 LanceDB] → RRF 融合 → 一跳图扩展 → 返回
```

返回 `mode: "keyword" | "vector" | "hybrid"`：
- vector 无结果或没配 embedding → mode="keyword"（纯关键词降级）
- keyword 无结果但 vector 有 → mode="vector"
- 两者都有，或图扩展加入了邻居 → mode="hybrid"

#### 一跳图扩展（`blend_graph_results`，v0.6.6 起）

> 2026-09-25 补记：07-29 的逐行核对漏了这一段（v0.6.0 无，v0.6.6/v0.6.11 有）。

- 融合排序的前 `min(limit, 20)` 条作种子，在 wikilink 图（无向）上取一跳邻居；
  邻居得分 = Σ 1/(种子名次)，按分数降序、路径升序。
- 结果窗口留给图邻居的份额 = `ceil(limit × (0.30 − 0.15 × vector 覆盖率))`，
  夹在 `[1, limit−1]`；无 vector 命中时 30%，vector 占满窗口时 15%。
- 其余名额按原排名填入（跳过被选为邻居的页），图邻居追加在末尾。已在排名里的页
  保留原结果；新页的 snippet 为 `Graph neighbor of <种子标题>`，score =
  图分数 / (K+1)。结果带 `graph_related_to`（种子标题）。

improved-wiki 在 `_search_graph.py` 移植，链接解析复用 `graph.py`（正文 wikilink +
`related:`），与 Graph 命令一致。两处有意偏离：页面集合与 keyword 扫描一致（不含
REVIEW/clusters/media/lint 与根目录 `index.md`/`log.md`——search.rs 会把链接全库的
index.md 当成任何查询的头号邻居）；`type: redirect` 跳转页代表其目标页，无法解析
目标的跳转页不作为邻居。

#### keyword 搜索（Rust 实现）

CJK bigram 分词 + 加权评分：

| 匹配类型 | 分值 |
|---------|------|
| 文件名精确匹配 | +200.0 |
| 标题包含完整短语 | +50.0 |
| 正文短语每出现一次 | +20.0（上限 10 次） |
| 标题 token 命中 | ×5.0/token |
| 正文 token 命中 | ×1.0/token |

CJK 处理：中文 token > 2 字时拆成 bigrams + 单字 + 原词，去重后全部参与匹配。
停用词过滤：中英文常见停用词（的/是/了/the/is/a/...）。

#### vector 搜索

- 调用用户配置的远程 embedding API 对 query 向量化
- 在 LanceDB 中搜索 chunk 级向量（不是 page 级）
- chunk 结果按 page 聚合：top chunk score + tail chunks score × 0.3（blended）
- 支持三类 embedding 后端：
  - **Google** (generativelanguage.googleapis.com) — `:embedContent` 端点，`x-goog-api-key` 鉴权
  - **Volcengine/豆包** (ark.cn-beijing.volces.com) — `/embeddings` 或 `/embeddings/multimodal`（doubao-embedding-vision）
  - **任何 OpenAI 兼容** `/v1/embeddings` 端点 — 标准 `data[0].embedding` 解析
- embedding 配置由前端 `useWikiStore` 传入 `SearchEmbeddingConfig { enabled, endpoint, api_key, model, output_dimensionality, extra_headers }`
- **embedding 是可选的**——不配或调用失败 → 自动降级到纯 keyword，不报错

#### RRF 融合（Reciprocal Rank Fusion）

```
RRF_K = 60.0
score = 1/(K + token_rank) + 1/(K + vector_rank)
```

keyword 排名和 vector 排名独立计算，RRF 合并。同时保留 `vector_score` 字段供 UI 显示。

vector-only 结果（keyword 没命中的页面）会被 materialize 进结果集——读文件内容提取 title/images，用 chunk text 构造 snippet。

## improved-wiki vs NashSU 搜索对比

> 更新 2026-06-25：搜索侧已对齐——`search_wiki.py` + `_wiki_keyword.py` 实现了 hybrid keyword+vector+RRF(K=60)。下表反映当前状态。

| 维度 | NashSU | improved-wiki |
|------|--------|---------------|
| 语言 | Rust (Tauri backend) | Python (search_wiki.py) |
| keyword 搜索 | 有（CJK bigram + 加权评分） | 有（`_wiki_keyword.keyword_search`，CJK bigram + 加权评分） |
| keyword 扫描范围 | 遍历 `wiki/` 全部 md，计满 10,000 个即停（REVIEW 也计数） | **有意偏离**：先剪掉 `WIKI_ARTIFACT_DIRS`（REVIEW/clusters/media/lint）再遍历，无文件数上限。两个库均超 1.2 万页，旧的"排序后截前 1 万"会静默丢掉 sources/synthesis/thesis 等整目录；根目录 `index.md`/`log.md`（列全部标题，~1 MB）也跳过，与 vector 的 `SKIP_STEMS` 一致 |
| snippet | 在原文件（含 frontmatter）上取锚点 | **有意偏离**：打分仍用全文，snippet 只取正文，避免返回 `--- type: … title: …` |
| redirect 跳转页 | search.rs 不处理（无此约定） | improved-wiki 独有（dedup 合并留下）：结果替换为 `redirect:` 目标页并去重，附 `redirected_from` |
| vector 搜索 | 有（LanceDB chunk 级） | 有（LanceDB chunk 级，`wiki_chunks` 表） |
| 融合策略 | RRF (K=60) | RRF (K=60)（`_wiki_keyword.rrf_merge`） |
| embedding 默认 | disabled；用户配置 endpoint/model | 本地 Ollama bge-m3（CLI 有意默认） |
| embedding 后端 | Google/Volcengine/OpenAI 兼容 | Google/Volcengine/OpenAI 兼容（env 配置，默认 Ollama） |
| 降级策略 | vector 失败 → 纯 keyword，同时保留 last error | vector 失败 → stderr 明示原因后纯 keyword |
| chunk 级搜索 | top-K×3（至少 30）后 page 聚合 blended | 同：top-K×3（至少 30）后 page 聚合 blended |
| 模式上报 | mode: keyword\|vector\|hybrid | mode: keyword\|vector\|hybrid |
| 全量重建后维护 | compact + prune verified old versions | compact + prune verified old versions（`delete_unverified=False`；失败不使已成功的新索引失效） |
| ingest 更新 | 只对 written paths 做 page-scoped replace | 同；不再每本书全库 rebuild |
| chunker | target/max/min/overlap=`1000/1500/200/200`，section-aware，code/table indivisible | `text-chunker.ts` Python 直接移植 |
| 持久化 vector cache | 无 | 无；旧 `embed-cache.json` 仅为历史遗留且不再读取 |
| batch response | 校验 count/index/finite/dimension，失败逐条重试 | 同 |
| request timeout | 8 秒 | 默认 8 秒；`EMBEDDING_TIMEOUT_SECONDS` 可覆盖 |
| 页面删除 | source/lint cascade 后按 page 删除 vector rows（non-critical） | 同；路径型 page id 避免跨目录同名碰撞 |

**有意差异**：
1. NashSU 默认关闭 embedding；improved-wiki CLI 为现有 HardwareWiki/RadarWiki
   保留本地 Ollama bge-m3 默认。
2. NashSU ingest 把 embedding 当 non-critical，甚至允许一个 page 只写入部分
   成功 chunks；improved-wiki 的完成契约更严格：touched page 必须 exact coverage，
   否则 Stage 3.7 暂停且不置 `ingested`。
3. NashSU `page_id` 是文件 stem；improved-wiki 使用 wiki-relative path-derived id，
   避免不同 schema 目录中同名页面相互覆盖。

## 什么 "NashSU parity" 实际覆盖

improved-wiki 的 "NashSU parity" 声称主要覆盖：
- ✅ ingest 流程（heading path, overlap, CJK slug, PPTX/DOCX, sources union merge, schema routing, aggregate repair, page merge, wikilink enrichment, source lifecycle）
- ✅ graph 关联（4 信号 + 双图 retrieval/display + Louvain 社区 + gaps + surprising + filters）— 已对齐（2026-06-29），少数有意 CLI 偏离见上文
- ✅ 搜索检索 — **已对齐**（hybrid keyword+vector+RRF K=60 + 一跳图扩展（2026-09-25 补齐）；vector 失败会明确报警并在本次查询继续 keyword-only，`--keyword-only` 可主动跳过 vector）

## 0.6.6 ingest embedding 的实际流程

`ingest.ts` 在写页后逐个调用 `embedPage`；`vector_upsert_chunks` 删除该
`page_id` 的旧 chunks 后批量加入新 chunks。普通 ingest 不扫描全库。Settings
中的 “Re-index all” 才走 full rebuild：先准备所有 pages，若任一 page 仍有
failed chunks 则不 clear live table；全部准备成功后 clear + serialized upsert，
最后 optimize。

improved-wiki 的 Stage 3.7 采用同一 page-scoped 更新方式。显式 `embed` 则先在
内存中准备全部 rows，再以一次 LanceDB overwrite 替换 live table，并额外核验
最终 row count；这是 CLI 环境下对 NashSU clear+serialized-upsert 的等价安全实现。
source lifecycle、lint orphan cascade 与 cross-source dedup 合并删除 Markdown 后，
也会按 page id 删除对应 rows；该清理保持 NashSU 的 non-critical 语义。CLI 没有桌面
文件监听器，ingest 之外改页/删页（dedup 改链、review 修复、补链、手工编辑）造成的
漂移用 `build_embeddings.py sync` 对账：重新分块（不调 embedding）逐页比对
chunk 文本/面包屑/标题，只重嵌缺失与已变页、删除已消失页。`search_wiki.py` 在
vector 结果里丢弃文件已不存在的页并提示 sync。
