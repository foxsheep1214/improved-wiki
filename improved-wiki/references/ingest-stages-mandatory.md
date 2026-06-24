---
name: improved-wiki
description: "强制 Ingest Stage 清单——improved-wiki 流水线的 20 个编号 Stage + 2 个前置门 + Lint + Graph 规范，每 Stage 含作用/产物/go-no-go。用于约束 ingest 时不漏步。"
tags: [ingest, mandatory, pipeline]
related: [SKILL.md, known-issues, scanned-pdf-ocr-pipeline, image-caption-strategy]
---

# 强制 Ingest Stage 清单

improved-wiki 流水线 = **Phase 0（2 前置门）+ 20 个编号 ingest Stage + Lint + Graph**。编号与 `ingest.py` 代码一致，**编号即执行顺序**。任何 Stage 都不能跳过。Graph 是独立命令（与 Ingest/Lint 并列，不属于 ingest 管线）。

**跳过的代价**：raw 是 sacred（图也是 raw 的一部分）；缺 stage 产物则审计无法回溯；不写 cache 下次重跑；跳过的 stage 永远不会被补做，错误留在 wiki 里。

> **无静默回退策略（2026-06-24）**：ingest 路径禁止任何静默回退。主路径不能用（caption key 缺失、caption 批次重试耗尽、embedding stack 缺失、LLM page-merge 失败、`~/.agents/config.json` 解析失败）一律 `raise RuntimeError` 告警暂停，不降级。所有阶段产物已缓存，修好依赖后重跑从断点恢复。唯一例外：`load_cache`/`load_stages` 状态文件损坏 → 告警+重置（重摄是正确恢复，非质量降级）。详见 SKILL.md。

## 阶段编号 → 代码函数

| Stage | 代码函数 | 说明 |
|-------|---------|------|
| 0.1 | `normalize_raw_names.py --check` | raw 命名规范检查（前置门） |
| 0.2 | 源页存在性检查 | 源页去重（`wiki/sources/<rel>.md`） |
| 1.1 | `stage_1_1_extract_text` | 文本提取（minerU hybrid-engine，所有 PDF 统一） |
| 1.2 | `stage_1_2_extract_images` | 图片提取（融进 1.1 chunk 处理） |
| 1.3 | `stage_1_3_caption_images` | 图片 caption（MiniMax VLM） |
| 2.1 | `stage_2_1_global_digest` | 全局摘要 |
| 2.2 | `stage_2_2_chunk_analysis` | 逐 chunk 分析 |
| 2.3 | `_stage_2_3_detect_incremental_associations` | 增量关联检测（与已有 wiki 重叠） |
| 2.4 | `_stage_2_4_generate_chunk`（barrier-free 循环） | 概念/实体逐 chunk 生成 |
| 2.5 | `_stage_2_5_*`（`_stage_2_5_dedup.py`） | 源内概念去重合并（多 chunk） |
| 2.6 | `stage_2_6_source_page` | 源页面生成 |
| 2.7 | `stage_2_7_query_generation` | 问题生成 |
| 2.8 | `_stage_2_8_resolve_queries` | 跨源 query 解析（LLM judge） |
| 2.9 | `stage_2_9_comparison_generation` | 对比生成（2.9A 消歧义 / 2.9B 源内） |
| 3.1 | `stage_3_1_write_wiki_file` | 文件写盘 |
| 3.2 | `stage_3_2_inject_images` | 图片注入 source 页 |
| 3.3 | `stage_3_3_slug_collision_review` | 跨域 slug 碰撞审查 |
| 3.4 | `stage_3_4_review_suggestions` | 内容质量审查（运行在已写盘文件上） |
| 3.5 | `stage_3_5_aggregate_repair` | 聚合修复（index/log/overview）+ 缓存 |
| 3.6 | `_stage_3_6_calculate_quality_score` | 质量评分卡 |
| 3.7 | `stage_3_7_embed_new_pages` | 嵌入向量化（本地 Ollama bge-m3） |
| 4.1 | `stage_4_1_validate_ingest` / `validate_ingest.py` | 最终验证 |

Phase 划分：0 前置检查 / 1 提取 / 2 分析生成 / 3 写入富化 / 4 验证。

---

## Phase 0：Pre-Ingest Gates

### Stage 0.1 · Raw 文件命名规范检查 ⭐ 强制
- **作用**：确保 raw/ 下文件符合项目命名规范（规则记在 `<project>/raw/NAMING.md`）。
- **流程**：`NAMING.md` 不存在 → 🛑 阻止 ingest，帮用户起草；存在 → `normalize_raw_names.py --check`，违规 → 🛑 阻止。
- **go/no-go**：`raw/NAMING.md` 存在且候选文件全部合规。

### Stage 0.2 · 源页去重检查 ⭐ 任何文件选取前强制
- **作用**：判断候选文件是否已消化。**唯一依据：`wiki/sources/<raw-rel-path>.md` 是否存在**（不是 `ingest-cache.json`——缓存不可靠：可被删、跨对话丢失、并发损坏）。
- **完整性校验**：源页存在但引用的 concepts/entities 丢失 >80% → 重新消化（防上次崩溃留残页）。
- **go/no-go**：源页存在且 >80% 引用页面存在 → 跳过；否则进入 Stage 1.1。

---

## Phase 1：Extraction

### Stage 1.1 · 文本提取 ⭐ 永远不能跳
- **作用**：所有 PDF（文本版/扫描版/混合版）统一走本地持久化 minerU API 服务器（`mineru.cli.fast_api`，端口 19999），按 50 页/chunk 调 `/file_parse`，`backend=hybrid-engine`、`parse_method=auto`（按页自动判 txt vs VLM OCR），保留表格/公式/图片。method 标签 `mineru-api`（garbled 字体强制 ocr → `mineru-api-ocr`）。fitz 仅做 garbled 检测抽样，不做提取。
- **为什么不用 PyMuPDF 直抽**：在数据手册/图表密集型 PDF 上漏检表格/公式/图（实测 73 表格/7 公式/157 图 vs 0/0/2）。
- **并发限制**：系统级最多 1 个 minerU 任务，`fcntl.flock` 文件锁（超时 3600s），等待时打印 `[mineru] Waiting for lock...`。免费、无需 API key。详见 `scanned-pdf-ocr-pipeline.md`。
- **产物**：每页一个 `p<NNN>.txt`（页号 1:1）。
- **go/no-go**：平均 chars/page >100；无幻觉（chars<100 且无中文 → 重跑）。
- **已知坑**：`mineru -b pipeline` CLI 在 3.4.0 有 502 bug，默认不走；`IMPROVED_WIKI_PIPELINE_CLI=1` 可 opt-in（已知坏，仅调试用）。

### Stage 1.2 · 图片提取 ⭐ 永远不能跳
- **作用**：融进 Stage 1.1 chunk 处理——每个 chunk 调 `/file_parse` 后，`_stage_1_2_harvest_images()` 从响应 `images`（base64）+ `content_list`（页码映射）存图到 `wiki/media/<type>/<pdf-stem>/`，文件名 `p<NNN>-mineru_<md5前8>.<ext>`。全本跑完汇总 `_manifest.json`，并直接调 Stage 1.3 配文字。PPTX/DOCX 走 `_stage_1_2_extract_images_office()`（从 zip 内 `ppt/media`/`word/media` 取图）。
- **产物**：`wiki/media/<type>/<pdf-stem>/p<NNN>-mineru_<id>.<ext>` + `_manifest.json`。
- **go/no-go**：抽出图总数 >0；确实无图则在 source 页 `## Embedded Images` 写"无嵌入图"。
- **尺寸过滤**：`MINERU_IMG_MIN_WIDTH/HEIGHT` 默认 20px（故意低，保留公式截图）。
- **注意**：API 路径按 `page+md5前8` 命名，不做跨页 sha256 全局去重（同一图重复出现在不同页会各存一份）。

### Stage 1.3 · 图片 captioning ⭐ 永远不能跳
- **作用**：对每张图用 MiniMax VLM 生成 1-3 句描述（中文优先）。走 `anthropic/v1/messages` 多图 content blocks 批量调用（5 张/批）。
- **依赖**：`~/.agents/config.json` 的 `providers.minimax.api_key`，或 `CAPTION_API_KEY`/`LLM_API_KEY` env。
- **产物**：每图一个 `.caption.txt`。
- **go/no-go**：每张图有 caption 文件且长度 ≥20 字符。
- **无回退**：key 缺失或批次重试耗尽 → `raise RuntimeError` 暂停，不写占位符降级（2026-06-24）。

---

## Phase 2：Analysis & Generation

### Stage 2.1 · Global Digest
- **作用**：1 次 LLM 调用，喂整本 PDF + schema + index，输出 6 块结构化 YAML：`book_meta`/`outline`/`key_entities`/`key_concepts`/`key_claims`/`chunk_plan`。
- **产物**：progress checkpoint 中的 global digest。
- **go/no-go**：`stages.global_digest_keys ≥ 1`。

### Stage 2.2 · Chunk Analysis
- **作用**：对源文本切块分析（**永远不能跳**）。短源（≤60K 字符）1 块；长源按 ~60K/块切分。每 chunk 输出 `entities_found`/`concepts_found`/`claims`/`formulas`/`connections_to_existing_wiki`/`digest_updates`。
- **go/no-go**：`stages.chunks_analyzed ≥ 1`。

### Stage 2.3 · Incremental Association Detection
- **作用**：chunk 分析后、生成前，检测本源 entities/concepts 与 wiki 已有页面的关联，供 2.4 生成时避免重复、识别 comparison 对象。
- **跳过条件**：wiki 为空（首次 ingest）。
- **go/no-go**：wiki 非空时 `stages.incremental_associations` 已记录。

### Stage 2.4 · Generation（barrier-free pipeline）
- **作用**：与 2.2 合并为 barrier-free pipeline：analyze chunk → generate pages → next chunk。每 chunk 分析完立即生成概念/实体页。仅生成 source/concept/entity 三种 page type。利用 2.3 的 `incremental_associations` 标记 `existing_wiki_reference`。
- **产物**：FILE blocks（`---FILE:wiki/<path>---...---END FILE---`）。
- **go/no-go**：`stages.file_blocks_generated ≥ 1`；source page FILE block 存在；概念页路径在 `wiki/concepts/` 下。
- **completion path**：barrier-free 产出 0 concept → per-concept 生成（每 concept 一次 LLM 调用）补齐。

### Stage 2.5 · Concept Dedup & Merge
- **作用**：2.4 生成所有 chunk 的 concept/entity 后，对同一本书内部概念去重合并（防同名异义重复页）。确定性初筛（Jaccard ≥0.6 + 停用词过滤）+ LLM 确认，失败保守不合并。
- **跳过条件**：单 chunk 书。
- **go/no-go**：多 chunk 时 `concept_merge_rules` 已记录（可为 `[]`）。

### Stage 2.6 · Source Page Generation
- **作用**：基于 2.5 去重结果生成/更新源页面（本书索引，列出所有概念/实体/问题/对比）。
- **go/no-go**：source page 路径为 `wiki/sources/<stem>.md`。

### Stage 2.7 · Query Auto-Generation
- **作用**：基于 2.4 的 concept/entity，识别书中提出但未完全解答的开放问题，生成 `wiki/queries/<slug>.md`。详见 `query-generation.md`。
- **跳过条件**：source 类型为 `datasheet`/`standard`（纯事实罗列）。
- **go/no-go**：0-5 个 query FILE block 或 `---QUERIES: 0---` 标记；每个 query frontmatter 含 `type: query`+`title:`+`sources:`。

### Stage 2.8 · Cross-source Query Resolution
- **作用**：对 2.7 的 query 检索 wiki 已有页面是否已回答。已答 → 关闭删除；未答 → 保留。LLM judge，不确定一律 kept。
- **跳过条件**：2.7 无 query，或 wiki 为空。
- **go/no-go**：`query_resolutions` 已记录（可为 `[]`）。

### Stage 2.9 · Comparison Auto-Generation
- **作用**：2.9A 域内消歧义（新 concept 与已有 concept 同名不同 domain → 消歧义页）；2.9B 源内概念对比（两个高度相关概念 → 对比页，对比维度 ≥4，至多 2 页）。详见 `comparison-generation.md`。
- **跳过条件**：本次 concept 和 entity 都为空。
- **go/no-go**：comparison FILE block 或 `---COMPARISONS_*: 0---` 标记；frontmatter 含 `type: comparison`+`title:`+`domain:`。

---

## Phase 3：Write & Enrich

### Stage 3.1 · Write files（含 source page gate）
- **作用**：Phase 3 唯一磁盘写入入口。先 source page gate（无 source 页则从 digest 生成 stub 追加），再原子写盘（.tmp → rename）。
- **go/no-go**：page_blocks 数 == 写盘成功数；source page 已落盘。

### Stage 3.2 · 图片注入 ⭐ 永远不能跳
- **作用**：在 source 页末尾追加 `## Embedded Images` 段，列出所有图 + caption。
- **go/no-go**：source 页含 `## Embedded Images` + ≥1 行图引用。

### Stage 3.3 · Cross-domain Slug Collision Review
- **作用**：写盘后扫描新 concept 页 slug，检测与其它 domain 已有 concept 的同名碰撞，标记需消歧义。同 domain 内重叠是合法合并（2.5 处理），不重复。
- **go/no-go**：跨域碰撞数已统计（可为 0）。

### Stage 3.4 · Review ⭐ 永远不能跳
- **作用**：满足 NashSU 3 条件（≥4 FILE 块 / ≥10K 字符 / 未闭合 REVIEW）时跑一次 LLM，输出 5 类 review items（confirm/suggestion/missing-page/contradiction/duplicate），写入 `wiki/REVIEW/<type>/<date>-<source>-<slug>.md` + `review-suggestions.json`。运行在已写盘文件上。
- **go/no-go**：review items 数量 ≥0（即使 0 也要记）；`wiki/REVIEW/` 结构合法。

### Stage 3.5 · Aggregate Repair + Cache ⭐ 永远不能跳
- **作用**：程序化 append index.md/log.md（LLM 不参与，防丢历史）+ LLM 重写 overview.md（喂入当前全文作上下文）+ 写 `ingest-cache.json`。
- **go/no-go**：每个本次 raw 文件都有 hash 记录；index/log/overview 已更新。

### Stage 3.6 · Quality Scoring Card
- **作用**：对本次 ingest 量化评分（文本覆盖 25% / 图片质量 20% / 概念密度 25% / Review 质量 20% / 去重完整性 10%）。
- **产物**：`quality_metrics` + 可选 `wiki/REVIEW/audit/<date>-<source>-quality.md`。
- **go/no-go**：`quality_metrics` 已记录；`overall_score < 0.65` → 标记 needs_review。

### Stage 3.7 · Embeddings ⭐ 强制
- **作用**：把 wiki/ 页面 chunk 化 + embed 写到 LanceDB。默认本地 Ollama bge-m3（`http://127.0.0.1:11434/v1`），无需 export 环境变量。
- **依赖**：lancedb 已装 + Ollama 运行 + bge-m3 已拉取。
- **产物**：`lancedb/` 表 + `embed-cache.json`。
- **go/no-go**：LanceDB 表存在 + 已写 ≥N chunk。
- **无回退**：stack 缺失 → `raise RuntimeError` 暂停（2026-06-24）。页面已落盘，修好 stack 后重跑从 3.7 恢复（write_phase marker 跳过 3.1-3.6）。

---

## Phase 4：Validation

### Stage 4.1 · Validate Ingest
- **作用**：ingest 末尾自动运行 `validate_ingest.py`（15 阶段全验证），结果打印到 stdout。遵循 Iron Law：NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE。
- **go/no-go**：`Result: N/M`；hard failure 阻止 "ok" 状态。

---

## 强制顺序与依赖

```
0.1 → 0.2 → 1.1 → 1.2 → 1.3 → 2.1 → 2.2 → 2.3 → 2.4 → 2.5 → 2.6 → 2.7 → 2.8 → 2.9
     → 3.1 → 3.2 → 3.3 → 3.4 → 3.5 → 3.6 → 3.7 → 4.1
```

关键依赖：
- 1.2 先于 1.3（先有图才能 caption）；1.2/1.3 先于 3.2（注入图引用）
- 2.1/2.2 永远不跳（短源 1 chunk / 长源 N chunk）
- 2.3 依赖 2.2（wiki 为空跳过）；2.4 用 2.3 的增量关联；2.5 依赖 2.4（单 chunk 跳过）
- Phase 2 全在内存（2.4→2.5→2.6→2.7→2.8→2.9 串行），产出统一由 3.1 写盘
- 2.7 conditional（datasheet/standard 跳过）；2.8 conditional（2.7 无 query 或 wiki 空跳过）；2.9 conditional（无 concept 跳过）
- 3.3 在 3.2 后立即运行；3.4 在已写盘文件上运行；3.5 在所有页面写盘后；3.6 在 3.5 后
- 3.7 强制（缺 stack 暂停）；4.1 末尾自动

## 自动验证（ingest.py 内置）

每个 Stage 完成后有实时验证门禁（`_verify_stage_N()`），失败直接 `RuntimeError`：

| Stage | 门禁检查 |
|-------|---------|
| 1.1 | 提取文本 ≥500 字符；minerU ≥2000 字符 |
| 2.1 | Global Digest 含 6 必需 key；≥1 concept |
| 2.2 | chunk 分析非空 |
| 2.4 | ≥1 FILE block；source page 存在；路径正确 |
| 3.1 | source page 落盘 |

Ingest 末尾自动运行 `validate_ingest.py`（全阶段验证）。手动补充：
```bash
./scripts/wiki-lint.sh --summary                    # 结构性 lint（wikilink 健康）
test -d wiki/media/*/<slug> && find wiki/media/<type>/<slug> \( -name '*.jpeg' -o -name '*.png' \) | while read f; do [ -f "$f.caption.txt" ] || echo "MISSING CAPTION: $f"; done
```

## 项目特定策略

每个 wiki 项目可在 `wiki/methodology/` 写 per-project 决策页（VLM 选择、批量大小等），引用本清单。**不放本清单复制，也不放"跳过了哪些 stage + 原因"**。通用消化策略是本 skill 的责任。如真的跳过某个 ⭐ stage，在 `wiki/methodology/` 加说明并标注"已知违反强制清单，原因：……"——显式记录偏离 = 合规；静默跳过 = 违规。

---

## Graph 命令（独立，与 Ingest/Lint 并列）

Graph 不在 ingest 管线内。Ingest 管线不碰图——图建在 Graph 命令，图用在 Ingest（Stage 2.3 可通过 `--mode query` 查询已有图为新页面建议 wikilinks）。触发：完成一批 ingest 后手动运行，或 `AUTO_BUILD_GRAPH=1` 自动触发。详见 `graph.py --help`。

- **Stage 16 四信号图构建**：解析 wikilinks + frontmatter，构建 networkx 加权无向图（direct link ×3.0 / source overlap ×4.0 / Adamic-Adar ×1.5 / type affinity ×1.0）。产物 `graph.json`。
- **Stage 17 Louvain 社区检测**：社区检测 + cohesion 评分（<0.15 标记低质量）。
- **Stage 18 图谱洞察**：`knowledge-gaps.md`（孤立节点/桥接节点/建议缺失链接）+ `clusters/cluster-NNN.md`（社区 hub 页）。

```bash
IMPROVED_WIKI_ROOT=/path/to/wiki python3 scripts/graph.py              # 全量
IMPROVED_WIKI_ROOT=/path/to/wiki python3 scripts/graph.py --dry-run    # 仅统计
IMPROVED_WIKI_ROOT=/path/to/wiki python3 scripts/graph.py --mode query --slug "page"  # 查询建议
```
依赖：`pip install networkx python-louvain pyyaml`。

---

## 修订记录

- **2026-06-24（无回退策略 + 精简）**：ingest 路径禁止任何静默回退——caption 缺 key/批次失败、embeddings 缺 stack、LLM page-merge 失败、config.json 解析失败一律 `raise RuntimeError` 暂停，不降级（删占位符 fallback、array-merge fallback、静默 env 回退）。唯一例外：cache/stages 损坏 → 告警+重置。同时精简本文件：删 A/B 路径分流（已统一 minerU）、PyMuPDF 细节（已删）、commit hash/事故 anecdote、冗余验证清单和重复依赖说明。
- **2026-06-23**：所有 PDF 统一走 minerU hybrid-engine API，PyMuPDF 提取路径整体移除。
- **2026-06-22**：Stage 2.10（review）重编号为 3.4 移入 Phase 3；3.4/3.5/3.6 顺延为 3.5/3.6/3.7。
- **2026-06-20**：全量重编号为 Phase.Stage 形式，编号=执行顺序；新增 2.3/2.5/2.8/3.6。
