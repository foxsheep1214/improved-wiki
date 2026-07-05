---
name: improved-wiki
description: "强制 Ingest Stage 清单——improved-wiki 流水线的 17 个 active Stage（含 Phase 0 前置门）+ Lint + Graph 规范，每 Stage 含作用/产物/go-no-go。用于约束 ingest 时不漏步。"
tags: [ingest, mandatory, pipeline]
related: [SKILL.md, known-issues, scanned-pdf-ocr-pipeline, image-caption-strategy]
---

# 强制 Ingest Stage 清单

improved-wiki 流水线 = **17 个 active Stage（含 Phase 0 前置门，跨 4 个 Phase: 0-3）+ Lint + Graph**（源内去重原 2.5 并入 2.4 收尾、跨源 query 解析原 2.8 并入 2.7 收尾，功能保留、编号退休）。编号与 `ingest.py` 代码一致，**编号即执行顺序**。任何 Stage 都不能跳过。Graph 是独立命令（与 Ingest/Lint 并列，不属于 ingest 管线）。

**跳过的代价**：raw 是 sacred（图也是 raw 的一部分）；缺 stage 产物则审计无法回溯；不写 cache 下次重跑；跳过的 stage 永远不会被补做，错误留在 wiki 里。

> **无静默回退策略**：ingest 路径禁止任何静默回退（caption key 缺失、caption 批次重试耗尽、embedding stack 缺失、LLM page-merge 失败、config 解析失败 → 一律 `raise RuntimeError` 暂停，不降级）。完整政策见 SKILL.md「No-silent-fallback policy」段。唯一例外：cache/stage-progress 状态文件损坏 → 告警+重置。

## 阶段编号 → 代码函数

| Stage | 代码函数 | 说明 |
|-------|---------|------|
| 0.1 | `normalize_raw_names.py --check` | raw 命名规范检查（前置门） |
| 0.2 | 源页存在性检查 | 源页去重（`wiki/sources/<rel>.md`） |
| 1.1 | `stage_1_1_extract_text` | 文本提取（minerU hybrid-engine，所有 PDF 统一） |
| 1.2 | `stage_1_2_extract_images` | 图片提取（融进 1.1 chunk 处理） |
| 1.3 | `stage_1_3_caption_images` | 图片 caption（VLM，configurable provider） |
| 2.1 | `stage_2_1_global_digest` | 全局摘要 |
| 2.2 | `_stage_2_2_analyze_chunk` | 逐 chunk 分析（**全部 chunk 分析完**再进入 2.3） |
| 2.3 | `stage_2_3_*`（`_stage_2_3_incremental.py`） | 已存在 wiki 关联检测（在 2.2 与 2.4 之间，读 wiki） |
| 2.4 | `_stage_2_4_generate_*` + `_stage_2_5_dedup.py` | 概念/实体逐 chunk 生成（源锚定；≤1 chunk 单发）+ 源内概念去重收尾（embedding 语义初筛 cosine≥0.82 + LLM 确认，多 chunk；无回退） |
| 2.6 | `stage_2_6_source_page` | 源页生成（源索引；2.4 之后） |
| 2.7 | `stage_2_7_query_generation` + `_stage_2_8_query_resolve.py` | 问题生成 + 跨源 query 解析（候选 top-k **全部**交 LLM judge，一次批量 handoff，无 cosine 门槛；`RESOLVE_COSINE_THRESHOLD=0.70` 仅标记 `cross_refs`；无回退） |
| 2.9 | `stage_2_9_comparison_generation` | 源内对比生成 |
| 3.1 | `stage_3_1_write_wiki_file` | 文件写盘（含同名 slug 三层 page-merge，NashSU parity） |
| 3.2 | `stage_3_2_inject_images` | 图片注入 source 页 |
| 3.4 | `stage_3_4_review_suggestions` | 内容质量审查（运行在已写盘文件上） |
| 3.5 | `stage_3_5_aggregate_repair` | 聚合修复（index/log/overview）+ 缓存 |
| 3.7 | `stage_3_7_embed_new_pages` | 嵌入向量化（本地 Ollama bge-m3）— **最后一个 stage**，之后 `_finalize_book` 置完成标记 |

Phase 划分：0 前置检查 / 1 提取 / 2 分析生成 / 3 写入富化。
（无 Phase 4：post-ingest 验证体检已为对齐 NashSU 移除——NashSU 无此 stage。NashSU 唯一的 ingest 期检查"schema 路由"在写盘期的 Stage 3.1 做；`validate_ingest.py` 保留为独立手动工具。）

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
- **已知坑**：`mineru -b pipeline` CLI 在 3.4.0 有 502 bug，不可用；API path（hybrid-engine/auto）是唯一提取后端。

### Stage 1.2 · 图片提取 ⭐ 永远不能跳
- **作用**：融进 Stage 1.1 chunk 处理——每个 chunk 调 `/file_parse` 后，`_stage_1_2_harvest_images()` 从响应 `images`（base64）+ `content_list`（页码映射）存图到 `wiki/media/<type>/<pdf-stem>/`，文件名 `p<NNN>-mineru_<md5前8>.<ext>`。全本跑完汇总 `_manifest.json`，并直接调 Stage 1.3 配文字。PPTX/DOCX 走 `_stage_1_2_extract_images_office()`（从 zip 内 `ppt/media`/`word/media` 取图）。
- **产物**：`wiki/media/<type>/<pdf-stem>/p<NNN>-mineru_<id>.<ext>` + `_manifest.json`。
- **go/no-go**：抽出图总数 >0；确实无图则在 source 页 `## Embedded Images` 写"无嵌入图"。
- **尺寸过滤**：`MINERU_IMG_MIN_WIDTH/HEIGHT` 默认 20px（故意低，保留公式截图）。
- **注意**：API 路径按 `page+md5前8` 命名，不做跨页 sha256 全局去重（同一图重复出现在不同页会各存一份）。

### Stage 1.3 · 图片 captioning ⭐ 永远不能跳
- **作用**：对每张图用 VLM 生成 2-4 句描述（与源文本同语言，NashSU `captionImage` parity）。**一图一调用** + 上下文感知 prompt（前后正文作 anchoring context，NashSU `buildCaptionPromptWithContext` parity；`CONTEXT_CHARS=150`）。
- **依赖**：`~/.agents/config.json` 配置 caption_provider，或 `CAPTION_API_KEY`/`LLM_API_KEY` env。
- **产物**：每图一个 `.caption.txt`。
- **go/no-go**：每张图有 caption 文件且长度 ≥20 字符。
- **无回退**：key 缺失 → `raise RuntimeError` 暂停；孤立单图重试 3 次仍失败 → 写 `[待重试]` 占位符（下次运行重试，非质量降级）；连续 3 次失败 → 判定 VLM 主路径宕机 `raise RuntimeError` 暂停。

---

## Phase 2：Analysis & Generation

### Stage 2.1 · Global Digest
- **作用**：1 次 LLM 调用，喂整本 PDF + schema + index，输出 6 块结构化 YAML：`book_meta`/`outline`/`key_entities`/`key_concepts`/`key_claims`/`chunk_plan`。
- **产物**：progress checkpoint 中的 global digest。
- **go/no-go**：`stages.global_digest_keys ≥ 1`。

### Stage 2.2 · Chunk Analysis
- **作用**：对源文本切块分析（**永远不能跳**）。chunk 大小由 context probe 动态决定（`target_tokens = min(64K, ctx×0.33)`，见 `references/context-probe.md`）：短源 1 块；长源按 chunk 预算切分。每 chunk 输出 `entities_found`/`concepts_found`/`claims`/`formulas`/`connections_to_existing_wiki`/`digest_updates`。
- **go/no-go**：`stages.chunks_analyzed ≥ 1`。

### Stage 2.4 · Generation（single-pass pipeline）
- **作用**：2.2 **分析完所有 chunk** 后，2.3 验证已存在 wiki 关联，再逐 chunk 生成概念/实体页（源锚定；≤1 chunk 走单发）。**不是** analyze→generate 逐 chunk 交错——全部分析在前，生成在后（2.3 夹在中间，需要全量分析结果）。默认生成 source/concept/entity；若 `schema.md` 声明了额外 typed 文件夹（NashSU schema 驱动路由），LLM 可把贴切的页路由进去（人物→people/、方法→methods/ 等），写盘阶段会接受这些 schema 文件夹。
- **子步骤（生成前）· 增量关联验证**：`_stage_2_3_resolve_proposed_connections` 拿 2.2 chunk 分析自报的 `connections_to_existing_wiki` 去磁盘验证（确认被引用的 concept/entity/source 页真实存在），产出 verified `incremental_associations` 作为 2.4 生成的 Linkable pages 列表——只允许 LLM wikilink 到真实存在的页面，防止幻觉链接。wiki 为空时跳过。
- **子步骤（生成后收尾）· 源内概念去重**（原 Stage 2.5，已并入 2.4）：对同一本书内部概念去重合并（防同名异义重复页）。**embedding 语义初筛**（cosine ≥0.82，复用 `_dedup_embedding.candidate_pairs`，取代旧的词级 Jaccard，能抓跨语言/同义重复如 傅里叶变换 vs Fourier transform）+ LLM 逐组确认，失败保守不合并。**无回退**：embedding stack 不可用则 `raise` 暂停（不退回 Jaccard）。跳过条件：单 chunk 书。go/no-go：多 chunk 时 `concept_merge_rules` 已记录（可为 `[]`）。
- **子步骤（生成后）· 源页生成**：所有 chunk 生成完，`stage_2_6_source_page` 从 global digest 生成源页（源索引，列出概念/实体/问题/对比），并入 file_blocks。源页正文按 doctype 分支：book → `## Book Summary` + `## Table of Contents & Key Concepts` + `## Key Takeaways`；paper → `## Paper Summary` + `## Methodology & Results` + `## Key Takeaways`（论文无章节目录，不套 chapter）。与 NashSU Step 2 把 source page 作为生成产物 item 1 对齐。go/no-go：source page 路径为 `wiki/sources/<stem>.md`。
- **产物**：FILE blocks（`---FILE:wiki/<path>---...---END FILE---`）。
- **go/no-go**：`stages.file_blocks_generated ≥ 1`；source page FILE block 存在；概念页路径在 `wiki/concepts/` 下。
- **completion path**：单遍生成产出 0 concept（或单发被截断）→ per-concept 生成（每 concept 一次 LLM 调用）补齐缺口。

### Stage 2.7 · Query Auto-Generation + Cross-source Resolution
- **作用**：基于 2.4 的 concept/entity，识别书中提出但未完全解答的开放问题，生成 `wiki/queries/<slug>.md`。详见 `query-generation.md`。
- **跳过条件**：source 类型为 `datasheet`/`standard`（纯事实罗列）。
- **go/no-go**：0-5 个 query FILE block 或 `---QUERIES: 0---` 标记；每个 query frontmatter 含 `type: query`+`title:`+`sources:`。
- **子步骤（生成后收尾）· 跨源 query 解析**（原 Stage 2.8，已并入 2.7）：对刚生成的 query 检索 wiki 已有页面是否已回答。用 embedding 相似度对候选（query 标题/正文 ↔ 已有 concept/entity 页向量，取代旧的标题词级 Jaccard）排序取 top-k，**全部**交 LLM judge（一次批量 handoff，一 query 一 verdict 行；**无 cosine 门槛**——`RESOLVE_COSINE_THRESHOLD=0.70` 仅用于标记写回 `cross_refs` 的结论，缺失/不可解析 verdict → kept 并 loud warn）；已答 → 关闭删除，未答/不确定一律 kept。**无回退**：embedding stack 不可用则 `raise` 暂停。**空 wiki**（无已有页可比）→ 全部 kept 且**不 embed**（真 no-op，非回退）。跳过条件：2.7 无 query。go/no-go：`query_resolutions` 已记录（可为 `[]`）。

### Stage 2.9 · Comparison Auto-Generation（源内）
- **作用**：源内概念对比（两个高度相关概念 → 对比页，对比维度 ≥4，至多 2 页）。详见 `comparison-generation.md`。
- **跳过条件**：本次 concept 和 entity 都为空，或 concept 数 <2（无对比对）。
- **go/no-go**：comparison FILE block 或 `---COMPARISONS_IN_SOURCE: 0---` 标记；frontmatter 含 `type: comparison`+`title:`。

---

## Phase 3：Write & Enrich

### Stage 3.1 · Write files（含 source page gate）
- **作用**：Phase 3 唯一磁盘写入入口。先 source page gate（无 source 页则从 digest 生成 stub 追加），再原子写盘（.tmp → rename）。
- **go/no-go**：page_blocks 数 == 写盘成功数；source page 已落盘。

### Stage 3.2 · 图片注入 ⭐ 永远不能跳
- **作用**：在 source 页末尾追加 `## Embedded Images` 段，列出所有图 + caption。
- **go/no-go**：source 页含 `## Embedded Images` + ≥1 行图引用。

### Stage 3.4 · Review ⭐ 永远不能跳
- **作用**：满足 NashSU 3 条件（≥4 FILE 块 / ≥10K 字符 / 未闭合 REVIEW）时跑一次 LLM，输出 5 类 review items（confirm/suggestion/missing-page/contradiction/duplicate），写入 `wiki/REVIEW/<type>/<date>-<source>-<slug>.md` + `review-suggestions.json`。运行在已写盘文件上。
- **go/no-go**：review items 数量 ≥0（即使 0 也要记）；`wiki/REVIEW/` 结构合法。

### Stage 3.5 · Aggregate Repair + Cache ⭐ 永远不能跳
- **作用**：log.md 程序化 append（LLM 不参与，防丢历史）+ index.md LLM 整页重写（喂入磁盘扫描的权威页面清单，全分类同步；LLM 失败/超容量门/>250 页时退回 Sources 单行 append 兜底）+ overview.md LLM 重写（改进 prompt：禁止源清单堆砌、按主题综述；5 段结构校验；失败保留当前；超限压缩模式；首次 ingest 创建）+ 写 `ingest-cache.json`。
- **go/no-go**：每个本次 raw 文件都有 hash 记录；index/log/overview 已更新。

### Stage 3.7 · Embeddings ⭐ 强制
- **作用**：把 wiki/ 页面 chunk 化 + embed 写到 LanceDB。默认本地 Ollama bge-m3（`http://127.0.0.1:11434/v1`），无需 export 环境变量。
- **依赖**：lancedb 已装 + Ollama 运行 + bge-m3 已拉取。
- **产物**：`lancedb/` 表 + `embed-cache.json`。
- **go/no-go**：LanceDB 表存在 + 已写 ≥N chunk。
- **无回退**：stack 缺失 → `raise RuntimeError` 暂停。页面已落盘，修好 stack 后重跑从 3.7 恢复（write_phase marker 跳过 3.1-3.5）。

---

## （已移除）Phase 4：Validation — 对齐 NashSU

原 Stage 4.1（ingest 末尾自动跑 `validate_ingest.py` 体检）**已移除**：NashSU 无 post-ingest 验证 stage。NashSU 唯一的 ingest 期检查是 schema 路由（`validateWikiPageRouting`），improved-wiki 已在**写盘期 Stage 3.1**（`_stage_3_1_auto_correct_wiki_path`）做了，故自动保留。`validate_ingest.py` 保留为**独立手动工具**（见下文"可选手动验证"）。Stage 3.7（embeddings）现为最后一个 stage，之后 `_finalize_book` 置 `stage_4_1` 完成标记（标记键名沿用以兼容已消化的书与跳过逻辑）。

---

## 强制顺序与依赖

```
0.1 → 0.2 → 1.1 → 1.2 → 1.3 → 2.1 → 2.2 → 2.3 → 2.4 → 2.6 → 2.7 → 2.9
     → 3.1 → 3.2 → 3.4 → 3.5 → 3.7

（2.4 含源内去重收尾[原 2.5]；2.7 含跨源 query 解析收尾[原 2.8]）
```

关键依赖：
- 1.2 先于 1.3（先有图才能 caption）；1.2/1.3 先于 3.2（注入图引用）
- 2.1/2.2 永远不跳（短源 1 chunk / 长源 N chunk）；2.2 必须全部 chunk 分析完才进 2.3
- 2.3 在 2.2 与 2.4 之间检测已存在 wiki 关联（wiki 为空跳过）；2.4 生成后收尾跑源内去重（原 2.5，单 chunk 跳过）；2.6 源页在 2.4 之后
- Phase 2 全在内存（2.3→2.4→2.6→2.7→2.9 串行），产出统一由 3.1 写盘
- 2.7 conditional（datasheet/standard 跳过 query 生成）；2.7 跨源解析收尾 conditional（无 query 或 wiki 空跳过，原 2.8）；2.9 conditional（无 concept 或 concept <2 跳过）
- **3.1 写盘时同名 slug 走 page-merge**（NashSU parity）
- 3.4 在已写盘文件上运行；3.5 在所有页面写盘后
- 3.7 强制（缺 stack 暂停），是**最后一个 stage**；之后 `_finalize_book` 置完成标记

## Resume marker 粒度 ≠ stage 编号

上面的 2.1…3.7 编号是**叙事/可观测层**，不是崩溃恢复的实际单位。`<hash>.stages.json` 里真正的 done-marker 更粗：`stage_1_1/1_2/1_3_done`、`stage_2_1_done`、`stage_2_2_done`（wiki-独立↔依赖的分界点）、`stage_2_3_done`（覆盖 2.3+2.4）、`stage_2_9_done`（覆盖 2.5/2.6/2.7/2.8/2.9 整段）、`write_loop_done`、`write_phase`、`stage_4_1`（`ingest.py::_finalize_book` 所置，非某个 stage 模块自己的标记）。崩溃恢复是从**段边界**重启，不是逐 stage、逐 chunk。

**对未来"合并/拆分 stage"讨论的含义**：任何编号调整默认只是文档层 renumber-only，代码与 marker 不动；但有两条**载荷性边界**碰了就坏，不能移动：
1. `stage_2_2_done | stage_2_3_done` —— wiki-独立/依赖分界；批量 prefetch 靠在这里精确停住（`raise PrepareStopAfter("1.5")`）才能让下一本书的 prefetch 并行跑。
2. `write_loop_done | write_phase` —— 中间夹着 3.3 enrich 的非幂等 handoff；合并会让 resume 重跑非幂等的 Stage 3.1 写盘，重复 merge 每一页。同时要保持 artifact-before-marker 的写序（防 2026-06-25 的静默丢失 bug），碰这段边界时不要打乱写序。

## 自动验证（ingest.py 内置）

关键 Stage 完成后有实时硬门禁（`_verify_stage_*`），失败直接 `RuntimeError`：

| Stage | 门禁检查 |
|-------|---------|
| 1.1 | 提取文本 ≥500 字符；minerU ≥2000 字符（`_verify_stage_1_1_text`，报错前缀写作 "Stage 0"） |
| 2.1 | Global Digest 含 6 必需 key；≥1 concept（`_verify_stage_2_1_digest`） |
| 2.2 | chunk 分析非空（`_verify_stage_2_2_chunks`） |
| 2.4 | ≥1 FILE block；source page FILE block 存在；路径正确（`_verify_stage_2_4_file_blocks`，**写盘前** in-memory 检查） |

> 硬 raise 门禁只有以上 4 处（外加 Phase 0 的 `verify_stage_0`）。source page 的**落盘**由 2.4 的写盘前门禁保证；写盘后 `validate_stage_outputs` 只做软校验（返回 warning 列表、**不 raise**），没有 3.1 raise 门。

可选手动验证（**不再自动运行**——已为对齐 NashSU 移除）：`python3 scripts/validate_ingest.py`（全阶段体检，独立工具）。其它手动补充：
```bash
./scripts/wiki-lint.sh --summary                    # 结构性 lint（wikilink 健康）
test -d wiki/media/*/<slug> && find wiki/media/<type>/<slug> \( -name '*.jpeg' -o -name '*.png' \) | while read f; do [ -f "$f.caption.txt" ] || echo "MISSING CAPTION: $f"; done
```

## 项目特定策略

每个 wiki 项目可在 `wiki/methodology/` 写 per-project 决策页（VLM 选择、批量大小等），引用本清单。**不放本清单复制，也不放"跳过了哪些 stage + 原因"**。通用消化策略是本 skill 的责任。如真的跳过某个 ⭐ stage，在 `wiki/methodology/` 加说明并标注"已知违反强制清单，原因：……"——显式记录偏离 = 合规；静默跳过 = 违规。

---

## Graph 命令（独立，与 Ingest/Lint 并列）

Graph 不在 ingest 管线内。Ingest 管线不碰图——图建在 Graph 命令，图用在 Ingest 之外（`--mode query --slug <page>` 是只读工具，为任意页面返回 top-N 建议缺失 wikilink，不自动改文件、不在 ingest 管线内调用）。触发：仅手动运行 `python3 scripts/graph.py`（ingest/lint 不自动触发，对齐 NashSU：NashSU 无 post-ingest 图重建）。详见 `graph.py --help`。

- **四信号图构建**：解析 wikilinks + `related:` + frontmatter，构建 networkx 加权无向图（direct link ×3.0 / source overlap ×4.0 / Adamic-Adar ×1.5 / type affinity ×1.0）。产物 `<runtime>/graph.json`。大书（>100 页/源）source-overlap 改用 star（成员↔source 页 hub）避免 N² clique；AA 丢弃 <0.2 的 hub 噪声对。
- **Louvain 社区检测**：社区检测 + cohesion 评分（<0.15 标记低质量）；大图 betweenness 用采样近似。
- **图谱洞察**：`wiki/REVIEW/knowledge-gaps.md`（孤立节点/桥接节点/建议缺失链接）+ `wiki/clusters/cluster-NNN.md`（社区 hub 页）。

```bash
python3 scripts/graph.py --wiki-root /path/to/wiki              # 全量
python3 scripts/graph.py --wiki-root /path/to/wiki --dry-run    # 仅统计
python3 scripts/graph.py --wiki-root /path/to/wiki --mode query --slug "page"  # 查询建议
```
依赖：`pip install networkx pyyaml`（networkx 3.x 内置 Louvain，无需 python-louvain）。

---
