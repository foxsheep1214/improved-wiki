# NashSU 搜索与关联架构（源码实证）

> 2026-06-19 直接阅读 NashSU GitHub 源码（github.com/nashsu/llm_wiki）整理。
> 用于澄清 improved-wiki "NashSU parity" 声称在搜索/检索侧的实际含义。

## 两条独立的"语义关联"路径

NashSU 把"找关联页面"拆成两个完全独立的系统：

### 路径 1：知识图谱关联（graph-relevance.ts）— 纯确定性，无模型

源文件：`src/lib/graph-relevance.ts`

4 个信号算两个页面的关联度，权重与 improved-wiki graph.py 一致（2026-06-29 逐行对齐 0.5.3）：

| 信号 | 权重 | 含义 |
|------|------|------|
| direct link | ×3.0 | [[wikilinks]] 互链 |
| source overlap | ×4.0 | 引用了同一个 raw 源文件 |
| Adamic-Adar | ×1.5 | 共同邻居的图拓扑度量（1/log(degree)） |
| type affinity | ×1.0 | 页面类型亲和矩阵（entity↔concept 1.2, concept↔synthesis 1.2 等） |

计算过程：读 wiki/*.md → 提取 frontmatter (title/type/sources) + wikilinks → 构建 RetrievalGraph → calculateRelevance 逐对打分 → getRelatedNodes 排序返回 top-N。

**无 LLM、无 embedding、无模型调用**。纯文件读写 + 数学计算。

improved-wiki 的 `graph.py` 已对齐 0.5.3（2026-06-29 重写；早期版本把四信号都拿来造边，是当时最大偏离，已修正）：
- **边 = 链接关系**（`[[wikilinks]]` + 本 wiki 的 `related:` frontmatter 约定）；source-overlap / Adamic-Adar **只作边权重**，不再造边。
- **双图**（NashSU 实际架构）：retrieval graph（含 query 页）供 `calculateRelevance` 的 Adamic-Adar 邻居/度数打分；display graph（删 query 页）供节点/边/社区/渲染。
- **calculateRelevance 逐行移植**：direct=(正+反)×3.0、source=共享源数×4.0、AA=Σ 1/log(max(deg,2))×1.5（无阈值）、type-affinity 用 0.5.3 原矩阵（默认 0.5）×1.0 无条件叠加。
- **社区/洞察对齐**：cohesion = intra/(n(n-1)/2) 密度；`detectKnowledgeGaps`（isolated=linkCount≤1、稀疏社区、邻居跨≥3 社区的 bridge，无 betweenness）；`findSurprisingConnections` 已移植；`graph-filters` 默认隐藏结构页 + 删 query 类型。

**有意的 CLI 偏离**（非缺口，已记录）：Louvain 固定 `seed=42`（NashSU 不设种子，CLI 需可复现）；`related:` 作链接来源（本 wiki 约定，NashSU 页面无此字段）；落盘产物 `graph.json` / 自包含 `graph.html` / `REVIEW/knowledge-gaps.md` / `clusters/*`（NashSU 仅 app 内渲染）；建图排除自身产出目录 `GRAPH_SKIP_DIRS={REVIEW,clusters,media,lint}`；`--mode query` 排除已链接页（“建议新链接”语义）。

### 路径 2：搜索检索（Rust 后端 search.rs）— 混合 keyword + vector

源文件：`src-tauri/src/commands/search.rs`（Rust），前端入口 `src/lib/search.ts`（TypeScript）

这是 improved-wiki **没有对齐**的部分——improved-wiki 用 Python search_wiki.py + LanceDB，架构不同。

#### 搜索流程

```
用户查询 → tokenize → [keyword 搜索 wiki/*.md] + [vector 搜索 LanceDB] → RRF 融合 → 排序返回
```

返回 `mode: "keyword" | "vector" | "hybrid"`：
- vector 无结果或没配 embedding → mode="keyword"（纯关键词降级）
- keyword 无结果但 vector 有 → mode="vector"
- 两者都有 → mode="hybrid"

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
| vector 搜索 | 有（LanceDB chunk 级） | 有（LanceDB page 级） |
| 融合策略 | RRF (K=60) | RRF (K=60)（`_wiki_keyword.rrf_merge`） |
| embedding 默认 | 无默认（用户配远程 API） | 本地 Ollama bge-m3 |
| embedding 后端 | Google/Volcengine/OpenAI 兼容 | OpenAI 兼容（默认 Ollama） |
| 降级策略 | vector 失败 → 纯 keyword | vector 失败 → 纯 keyword（degrade，不报错） |
| chunk 级搜索 | 有（page 聚合 blended） | 无（page 级直接搜） |
| 模式上报 | mode: keyword\|vector\|hybrid | mode: keyword\|vector\|hybrid |

**剩余差异**（非阻断）：
1. NashSU **不内置任何本地模型**——embedding 完全由用户配置远程 API；improved-wiki 默认用本地 Ollama bge-m3 是**自己的添加**，不是 NashSU 的做法
2. NashSU 有 chunk 级向量搜索 + page 聚合，improved-wiki 是 page 级
3. embedding 后端可配置性：improved-wiki 目前默认 Ollama，未暴露远程 API 配置开关

## 什么 "NashSU parity" 实际覆盖

improved-wiki 的 "NashSU parity" 声称主要覆盖：
- ✅ ingest 流程（heading path, overlap, CJK slug, PPTX/DOCX, sources union merge, schema routing, aggregate repair, page merge, wikilink enrichment, source lifecycle）
- ✅ graph 关联（4 信号 + 双图 retrieval/display + Louvain 社区 + gaps + surprising + filters）— 已对齐 0.5.3（2026-06-29），少数有意 CLI 偏离见上文
- ✅ 搜索检索 — **已对齐**（hybrid keyword+vector+RRF K=60，本地 Ollama bge-m3；vector 失败降级 keyword-only）

未来可选增强：
1. chunk 级向量搜索 + page 聚合（blended）
2. embedding 后端可配置（支持远程 OpenAI 兼容 API，不只 Ollama）
