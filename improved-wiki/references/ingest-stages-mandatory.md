---
name: improved-wiki
description: "强制 Ingest Stage 清单——improved-wiki 流水线的 15 个 active Stage（含 Phase 0 前置门）+ Lint + Graph 规范，每 Stage 含作用/产物/go-no-go。用于约束 ingest 时不漏步。"
tags: [ingest, mandatory, pipeline]
related: [SKILL.md, known-issues, scanned-pdf-ocr-pipeline, image-caption-strategy]
---

# 强制 Ingest Stage 清单

improved-wiki 流水线 = **15 个 active Stage（含 Phase 0 前置门，跨 4 个 Phase: 0-3）+ Lint + Graph**（源内去重原 2.5 并入 2.4 收尾，功能保留、编号退休；Stage 2.7 query 生成 + 跨源解析已于 2026-07-12 整体移除，对齐 NashSU）。编号与 `ingest.py` 代码一致，**编号即执行顺序**。Graph 是独立命令（与 Ingest/Lint 并列，不属于 ingest 管线）。

**执行由代码强制，不靠人工遵守**：全部 stage 由 `ingest.py` 串行调度，agent 只答 prompt、无法跳过任何 stage（2.9 的跳过条件也是代码内置判断）。本清单是行为说明书（每 stage 作用/产物/go-no-go），不是纪律清单。唯一仍靠 agent 自觉的规则：不得绕过 `ingest.py` 手写 wiki 页冒充消化产物。（Stage 0.1 命名检查已于 2026-07-08 接入 `_do_prepare`——每个候选文件在 0.2 去重前自动过 `stage_0_1_check_file`，违规或项目无命名规则即 raise。）

> **无静默回退策略**：ingest 路径禁止任何静默回退（caption key 缺失、caption 批次重试耗尽、embedding stack 缺失、LLM page-merge 失败、config 解析失败 → 一律 `raise RuntimeError` 暂停，不降级）。完整政策见 SKILL.md「No-silent-fallback policy」段。唯一例外：cache/stage-progress 状态文件损坏 → 告警+重置。

## 阶段编号 → 代码函数

| Stage | 代码函数 | 说明 |
|-------|---------|------|
| 0.1 | `stage_0_1_check_file`（`_do_prepare` 内置，2026-07-08 接入；批量修复用 CLI `--check/--fix`） | raw 命名规范检查（前置门） |
| 0.2 | 源页存在性检查 | 源页去重（`wiki/sources/<rel>.md`） |
| 1.1 | `stage_1_1_extract_text` | 文本提取（minerU hybrid-engine，所有 PDF 统一） |
| 1.2 | `stage_1_2_extract_images` | 图片提取（融进 1.1 chunk 处理） |
| 1.3 | `stage_1_3_caption_images` | 图片 caption（VLM，configurable provider） |
| 2.1 | _(已移除，对齐 NashSU)_ | 原 Global Digest（并入 2.2 滚动） |
| 2.2 | `_stage_2_2_analyze_chunk` | 逐 chunk 分析（**全部 chunk 分析完**再进入 2.3） |
| 2.3 | `stage_2_3_*`（`_stage_2_3_incremental.py`） | 已存在 wiki 关联检测（在 2.2 与 2.4 之间，读 wiki） |
| 2.4 | `_stage_2_4_generate_*` + `_dedup_intra_source.py` | 概念/实体逐 chunk 生成（源锚定；≤1 chunk 单发）+ 源内概念去重收尾（embedding 语义初筛 cosine≥0.82 + LLM 确认，多 chunk；无回退） |
| 2.6 | `stage_2_6_source_page` | 源页生成（源索引；2.4 之后） |
| 2.7 | _(已移除，对齐 NashSU，2026-07-12)_ | 原问题生成 + 跨源 query 解析（信号改走 3.4 REVIEW suggestion → process-reviews） |
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

### Stage 0.1 · Raw 文件命名规范检查
- **作用**：确保 raw/ 下文件符合项目命名规范（规则块以 `<project>/schema.md` 的 ```yaml 为准；datasheet 厂商表另在 `raw/Datasheet/VENDORS.yaml`）。
- **流程**（2026-07-08 起代码强制）：`_do_prepare` 对每个候选文件调 `stage_0_1_check_file`——schema.md 缺失或无规则块 → raise（先起草规则）；违规 → raise（`normalize_raw_names.py --fix` 重命名后重跑）。范围与全库扫描一致：仅检查规则声明文件夹下的 `.pdf`（`raw/queries/*.md` 桥接件天然放行）；warn 级启发式不阻断。全库批量检查/修复仍用 `normalize_raw_names.py --check/--fix`。
- **go/no-go**：候选文件全部合规。

### Stage 0.2 · 源页去重检查
- **作用**：判断候选文件该跳过、续跑还是从头消化。**统一口径（与 SKILL.md / batch-digest-loop.md 一致）**：agent 在选文件前的批量预检用源页 `wiki/sources/<raw-rel-path>.md` 存在性快查；代码 Stage 0.2 的最终裁决以 **`ingested` marker 为主**（`_finalize_book` 在 Stage 3.7 embeddings 之后置位，见 `scripts/_ingest_skip.py::_stage_0_2_should_skip`），源页存在性为辅。**不依赖 `ingest-cache.json`**——缓存不可靠：可被删、跨对话丢失、并发损坏。
- **四状态决策**（`stage_4_1` marker 已于 2026-07-08 改名为 `ingested`，已消化书的 stages.json 已同步迁移）：
  1. `ingested` marker 在 + 源页存在 → **skip**（整本完成）。
  2. `ingested` marker 在 + 源页不存在 → **stale marker**（源页被外部删了）→ 清 marker、重新消化。
  3. `ingested` marker 不在 + 源页存在 → **resume**（已写盘但 post-write stages 未跑完；`write_phase` marker 让 3.1 写盘不重跑，resume 便宜且不重复合并已写页）。
  4. `ingested` marker 不在 + 源页不存在 → **fresh ingest**。
- **go/no-go**：状态 1 跳过；其余进入/续跑 Stage 1.1。
- **历史**：曾设想"源页引用的 concepts/entities 丢失 >80% → 重消化"的 wikilink-completeness 校验，但该块代码写在一个无条件 `return False` 之后、**从未执行**，已于 2026-06-25 作为 dead code 删除（commit `1dfd4f9`）。当前**没有引用页完整性校验**；`ingested` marker 是唯一完整性信号。

---

## Phase 1：Extraction

### Stage 1.1 · 文本提取
- **作用**：所有 PDF（文本版/扫描版/混合版）统一走本地持久化 minerU API 服务器（`mineru.cli.fast_api`，端口 19999），按 32 页/chunk（`MINERU_CHUNK_SIZE`）调 `/file_parse`，`backend=hybrid-engine`、`parse_method=auto`（按页自动判 txt vs VLM OCR），保留表格/公式/图片。method 标签恒为 `mineru-api`。fitz 仅用于 `--dry-run` 的 PDF 类型诊断（text/mixed/scanned），不参与提取决策。
- **NashSU 对齐**：NashSU 用 minerU **云** API（mineru.net，需 token，pipeline/vlm，200 页上限）；improved-wiki 用**本地**免费服务器（hybrid-engine/auto，无 token，无页数上限）——有意偏离。garbled-font 预检测与提取质量门已于 2026-07-08 移除（NashSU 二者皆无；minerU 3.4.0 上 OCR 影响有限）。`verify_stage_0` 的 ≥100 字符基本非空校验是唯一提取门。
- **为什么不用 PyMuPDF 直抽**：在数据手册/图表密集型 PDF 上漏检表格/公式/图（实测 73 表格/7 公式/157 图 vs 0/0/2）。
- **并发限制**：系统级最多 1 个 minerU 任务，`fcntl.flock` 文件锁（超时 3600s），等待时打印 `[mineru] Waiting for lock...`。免费、无需 API key。详见 `scanned-pdf-ocr-pipeline.md`。
- **chunk 粒度**：`MINERU_CHUNK_SIZE=32` 页/次。本地 /file_parse 同步端点无硬超时，chunk 化只为崩溃恢复粒度（每 chunk 完成缓存 stats.json）+ 控制单次等待。总提取时间由 minerU 处理瓶颈决定、与 chunk 数无关，故选较小 chunk：单次等待短（~32 页）、崩溃恢复粒度细（丢 ≤32 页），代价仅是 fitz 切分+HTTP overhead 略增（每 chunk 几秒，相对总时间微小）。
- **产物**：每页一个 `p<NNN>.txt`（页号 1:1）。
- **go/no-go**：`verify_stage_0` ≥100 字符（基本非空，防空提取浪费下游 LLM）。
- **已知坑**：`mineru -b pipeline` CLI 在 3.4.0 有 502 bug，不可用；API path（hybrid-engine/auto）是唯一提取后端。

### Stage 1.2 · 图片提取
- **作用**：图片存盘（harvest）融进 Stage 1.1 chunk 循环——每个 chunk 调 `/file_parse` 后，`_stage_1_2_harvest_images()` 从响应 `images`（base64）+ `content_list`（页码映射）存图到 `wiki/media/<type>/<pdf-stem>/`，文件名 `p<NNN>-mineru_<md5前8>.<ext>`。manifest 汇总（`_stage_1_2_extract_from_mineru`）+ PPTX/DOCX 提取（`_stage_1_2_extract_images_office`，从 zip 内 `ppt/media`/`word/media` 取图）+ Markdown 提取（`_stage_1_2_extract_markdown_images`，解析 `![[ref]]`/`![alt](ref)` 复制本地图片，NashSU `extractAndSaveMarkdownImages` parity）仍为独立 1.2 阶段（`stage_1_2_done` marker）。全本跑完汇总 `_manifest.json`，并直接调 Stage 1.3 配文字。
- **NashSU 对齐**：mineru 取图对齐（本地 API base64 vs 云 zip markdown，架构差异）；无 `extractAndSaveSourceImages` 的 pdfium 回退（1.1 no-silent-fallback 延伸，minerU 必跑或 raise）；Markdown 图片提取于 2026-07-08 补齐（此前 .md 源不提图，是唯一缺口）。
- **产物**：`wiki/media/<type>/<pdf-stem>/p<NNN>-mineru_<id>.<ext>` + `_manifest.json`。
- **go/no-go**：抽出图总数 >0；确实无图则在 source 页 `## Embedded Images` 写"无嵌入图"。
- **尺寸过滤**：`MINERU_IMG_MIN_WIDTH/HEIGHT` 默认 20px（故意低，保留公式截图）。
- **注意**：API 路径按 `page+md5前8` 命名，不做跨页 sha256 全局去重（同一图重复出现在不同页会各存一份）。

### Stage 1.3 · 图片 captioning
- **作用**：对每张图用 VLM 生成 2-4 句描述（与源文本同语言，NashSU `captionImage` parity）。**一图一调用** + 上下文感知 prompt（NashSU `buildCaptionPromptWithContext` parity）。
- **依赖**：`~/.agents/config.json` 配置 caption_provider（primary，无 env-var 替代路径）+ 可选 caption_fallback_provider（2026-07-08）。
- **产物**：每图一个 `.caption.txt`。
- **go/no-go**：每张图有 caption 文件且长度 ≥20 字符。
- **failover / 无回退**：primary 重试耗尽自动切 fallback（打一行日志，非静默）；无 provider → `raise RuntimeError` 暂停；孤立单图全部 provider 耗尽 → 写 `[待重试]` 占位符（下次运行重试，非质量降级）；连续失败 → 判定全部 VLM 路径宕机 `raise RuntimeError` 暂停。重试次数、fallback 串行化（`_FALLBACK_SEMAPHORE`）、推荐 provider 配置等细节见 `image-caption-strategy.md`（权威）。

---

## Phase 2：Analysis & Generation

### Stage 2.1 · Global Digest（已移除，对齐 NashSU，2026-07-08）
- **原作用**：整本单次 LLM → 6 块结构化 YAML digest，作 2.2 逐 chunk 分析的整本先验。
- **为什么去掉**：NashSU 的 globalDigest 是逐 chunk **过程中滚动产生**（初始空，每 chunk 产出 "Updated Global Digest" 合并），**无独立整本 digest 先验**。improved-wiki 2.2 已有滚动机制（`updated_global_digest` → `accumulated_digest`），原 2.1 只给 accumulated 种子。去掉 2.1 后 2.2 纯滚动（初始空），对齐 NashSU。
- **影响**：2.4/2.6/2.9 的 `global_digest` 数据源从 2.1 改为 2.2 滚动最终值（`_run_chunk_pipeline` 返回 5 元组含 `global_digest`）。`stage_2_1_done` marker 去掉（已消化书 stages.json 残留无害，代码不再读）。`_verify_stage_2_1_digest` 迁移到 2.2 完成后校验滚动最终 digest。`_stage_2_1_global_digest` / `_stage_2_1_build_prompt` 已作为 dead code 清理；`_stage_2_1_chunk_text`（切块函数）保留供 2.2 用。

### Stage 2.2 · Chunk Analysis
- **作用**：对源文本切块分析。chunk 大小由 context probe 动态决定（`target_tokens = min(64K, ctx×0.33)`，见 `references/context-probe.md`）：短源 1 块；长源按 chunk 预算切分。每 chunk 输出 `entities_found`/`concepts_found`/`claims`/`source_quotes`/`formulas`/`connections_to_existing_wiki`/`schema_typed_candidates`/`updated_global_digest`。
- **NashSU 对齐（2026-07-08）**：`accumulated_digest` 初始空（不再种子自 2.1），每 chunk 产出 `updated_global_digest` 滚动合并（NashSU `Updated Global Digest` parity）。2.2 完成后，最终 `accumulated_digest` 解析回 dict 作 `global_digest` 给 2.4/2.6/2.9。短源（1 chunk）= 整本 digest（对齐 NashSU 短源 Step 1）。`updated_global_digest` 必含 5 字段（book_meta/outline/key_entities/key_concepts/key_claims），首 chunk 建立 book_meta+outline。
- **NashSU 对齐 · digest 传递量与颗粒度（2026-07-09 用户裁定）**：chunk→chunk 传递的 digest 是**紧凑连续性台账，不是档案**——对齐 NashSU `ingest.ts` 的 `LONG_SOURCE_DIGEST_MAX = 15_000` 固定上限（`_stage_2_analyze.py::_DIGEST_PROMPT_CAP`，刻意**不**随模型 context 缩放；chunk 大小才缩放）+ "compact document-level digest" 指令。规则：先前所有 concept/entity 的**名字必须存活**（供后续 chunk 去重/关联），但每条压成一行短语，禁止逐字累积完整定义/key_details。详细内容不经 digest 传递——每个 chunk 的完整分析（concepts_found/claims/formulas）单独持久化在 `chunk_analyses`，2.4 逐 chunk 生成用各自 chunk 的全量分析，2.6 的 Main Arguments 用全书 `chunk_claims`、Key Concepts/Entities 清单用 2.4 实际生成的 slug 全集，均不依赖 digest 的详细度。此前 6K→24K→动态 target_chars 三版上限均为该裁定之前的过渡方案，已废除。
- **per-handoff subagent 隔离**：每 chunk fresh subagent 答单 chunk（7/8 事故政策；当晚扩展为**所有** LLM handoff 均派 fresh subagent、主对话只编排，见 `delegate-mode.md` L4）。
- **existing-slugs 相关性 cap（2026-07-09）**：chunk prompt 里的已有 wiki 页清单不再全量嵌入（6253 页曾产生单行 259KB×每 chunk，撑爆答题 subagent 的 Read），按"slug token 在本 chunk 文本中的包含率"排序取前 `_EXISTING_SLUGS_CAP=1000`（≈40K 字符，对齐 NashSU index 40K trim；2.4/2.6 早有同类 cap）。确定性排序，prompt 哈希跨 resume 稳定。
- **go/no-go**：`stages.chunks_analyzed ≥ 1`；2.2 完成后 `_verify_stage_2_1_digest` 校验滚动最终 digest 5 字段（`not analyze_only` 时）。

### Stage 2.4 · Generation（single-pass pipeline）
- **作用**：2.2 **分析完所有 chunk** 后，2.3 验证已存在 wiki 关联，再逐 chunk 生成概念/实体页（源锚定；≤1 chunk 走单发）。**不是** analyze→generate 逐 chunk 交错——全部分析在前，生成在后（2.3 夹在中间，需要全量分析结果）。默认生成 source/concept/entity；若 `schema.md` 声明了额外 typed 文件夹（NashSU schema 驱动路由），LLM 可把贴切的页路由进去（人物→people/、方法→methods/ 等），写盘阶段会接受这些 schema 文件夹。
- **并行生成（2026-07-09 默认开启）**：多 chunk 时用预计算 slug 清单（`_build_gen_inventory`，slug = slugify(name)，取自已缓存的 2.2 分析）取代"chunk N+1 靠 chunk N 实际产出的 slug 去重"——去重是确定性引用查表，不是像 2.2 rolling digest 那样的内容依赖，NashSU 本身也不对生成分 chunk（一次整书调用），没有"必须串行"的先例要对齐。一次 ingest.py 调用会吐出该书全部未缓存 chunk 的生成 prompt，主对话可批量并发派 sub-agent 作答（同一 `--parallel` 并发上限，见 `batch-parallel-prefetch.md`）。选项退出：`IMPROVED_WIKI_PARALLEL_GEN=0`/`false`/`no`/`off` 回退旧的严格串行累积路径（排查回归时用）。
- **子步骤（生成前）· 增量关联验证**：`stage_2_3_resolve_proposed_connections` 拿 2.2 chunk 分析自报的 `connections_to_existing_wiki` 去磁盘验证（确认被引用的 concept/entity/source 页真实存在），产出 verified `incremental_associations` 作为 2.4 生成的 Linkable pages 列表——只允许 LLM wikilink 到真实存在的页面，防止幻觉链接。wiki 为空时跳过。
- **子步骤（生成后收尾）· 源内概念去重**（原 Stage 2.5，已并入 2.4）：对同一本书内部概念去重合并（防同名异义重复页）。**embedding 语义初筛**（cosine ≥0.82，复用 `_dedup_embedding.candidate_pairs`，取代旧的词级 Jaccard，能抓跨语言/同义重复如 傅里叶变换 vs Fourier transform）+ LLM 逐组确认，失败保守不合并。**无回退**：embedding stack 不可用则 `raise` 暂停（不退回 Jaccard）。跳过条件：单 chunk 书。go/no-go：多 chunk 时 `concept_merge_rules` 已记录（可为 `[]`）。
- **子步骤（生成后）· 源页生成**：所有 chunk 生成完，`stage_2_6_source_page` 从 global digest 生成源页（源索引，列出概念/实体/问题/对比），并入 file_blocks。源页正文按 doctype 分支：book → `## Book Summary` + `## Table of Contents & Key Concepts` + `## Key Takeaways`；paper → `## Paper Summary` + `## Methodology & Results` + `## Key Takeaways`（论文无章节目录，不套 chapter）。与 NashSU Step 2 把 source page 作为生成产物 item 1 对齐。go/no-go：source page 路径为 `wiki/sources/<stem>.md`。
- **产物**：FILE blocks（`---FILE:wiki/<path>---...---END FILE---`）。
- **go/no-go**：`stages.file_blocks_generated ≥ 1`；source page FILE block 存在；概念页路径在 `wiki/concepts/` 下。
- **completion path**：单遍生成产出 0 concept（或单发被截断）→ per-concept 生成（每 concept 一次 LLM 调用）补齐缺口。

### Stage 2.7 · Query Auto-Generation（已移除，对齐 NashSU，2026-07-12）
- **原作用**：基于 2.4 的 concept/entity 生成 0-5 个开放问题 query 页 + 跨源 query 解析收尾（原 2.8）+ queries/index.md 维护。
- **为什么去掉**：NashSU 的 ingest 从不生成 query 页（生成清单只有 source/entities/concepts/index/log/overview + REVIEW 块）；NashSU 中 `queries/` = 保存的聊天回答 + 深度研究结果，只来自用户主动行为。
- **信号去向**："本书提出但未回答的研究问题"由 Stage 3.4 的 REVIEW `suggestion` item（含 `search_queries`）承接，经 `/improved-wiki process-reviews` 人工裁决（Deep Research → query 页带答案落地 / Create Page / Skip）。详见 `query-generation.md`（墓碑）与 `process-reviews.md`。
- **影响**：`stage_2_9_done` resume marker 名称保留（缓存兼容）；`queries_generated` 缓存统计移除；已存在的 query 页保留不动。

### Stage 2.9 · Comparison Auto-Generation（源内）
- **作用**：源内概念对比（两个高度相关概念 → 对比页，对比维度 ≥4，至多 2 页）。详见 `comparison-generation.md`。
- **跳过条件**：本次 concept 和 entity 都为空，或 concept 数 <2（无对比对）。
- **go/no-go**：comparison FILE block 或 `---COMPARISONS_IN_SOURCE: 0---` 标记；frontmatter 含 `type: comparison`+`title:`。

---

## Phase 3：Write & Enrich

### Stage 3.1 · Write files（含 source page gate）
- **作用**：Phase 3 唯一磁盘写入入口。先 source page gate（无 source 页则从 digest 生成 stub 追加），再原子写盘（.tmp → rename）。
- **go/no-go**：page_blocks 数 == 写盘成功数；source page 已落盘。

### Stage 3.2 · 图片注入
- **作用**：在 source 页末尾追加 `## Embedded Images` 段，列出所有图 + caption。
- **go/no-go**：source 页含 `## Embedded Images` + ≥1 行图引用。

### Stage 3.4 · Review
- **作用**：满足 NashSU 3 条件（≥4 FILE 块 / ≥10K 字符 / 未闭合 REVIEW）时跑一次 LLM，输出 5 类 review items（confirm/suggestion/missing-page/contradiction/duplicate），写入 `wiki/REVIEW/<type>/<date>-<source>-<slug>.md` + `review-suggestions.json`。运行在已写盘文件上。
- **go/no-go**：review items 数量 ≥0（prompt 要求 ≥5 条，但门禁实际接受 ≥0——即使 0 也要记）；`wiki/REVIEW/` 结构合法。
- **时机偏离 NashSU（有意，audit M2 2026-07-07）**：NashSU 在 `writeFileBlocks` **之前**对 in-memory generation 跑 review；improved-wiki 在 3.1 写盘**之后**对已落盘文件跑。这是刻意选择，理由：(1) review items 本就是非阻断 triage（`resolved: false` 等人工处理），NashSU 的"写盘前"也只是时机不同、并不拦截写盘，故"写盘前拦截能力"在 NashSU 侧也不成立；(2) 写盘后 review 看到 enrichment/wikilink-merge/page-merge 之后的真实 on-disk 内容，finding 反映最终状态，对 lint/cross-source dedup 友好，而写盘前看到的是 pre-enrichment 内容、易产出过时 finding；(3) 真正的结构性失败拦截已由 Stage 2.6 `_stage_2_6_validate_required_sections` 硬门禁（缺 section 直接 raise）覆盖。代价：review 发现问题时页已落盘，需后续修复——但 review items 本就不阻断，该代价可接受。不额外跑 pre-write LLM pass（双倍 review 成本对非阻断 triage 项 ROI 低）。

### Stage 3.5 · Aggregate Repair + Cache
- **作用**：log.md 程序化 append（LLM 不参与，防丢历史）+ index.md LLM 整页重写（喂入磁盘扫描的权威页面清单，全分类同步；LLM 失败/超容量门/>250 页时退回 Sources 单行 append 兜底）+ overview.md LLM 重写（改进 prompt：禁止源清单堆砌、按主题综述；5 段结构校验；失败保留当前；超限压缩模式；首次 ingest 创建）+ 写 `ingest-cache.json`。
- **go/no-go**：每个本次 raw 文件都有 hash 记录；index/log/overview 已更新。

### Stage 3.7 · Embeddings
- **作用**：把 wiki/ 页面 chunk 化 + embed 写到 LanceDB。默认本地 Ollama bge-m3（`http://127.0.0.1:11434/v1`），无需 export 环境变量。
- **依赖**：lancedb 已装 + Ollama 运行 + bge-m3 已拉取。
- **产物**：`lancedb/` 表 + `embed-cache.json`。
- **go/no-go**：LanceDB 表存在 + 已写 ≥N chunk。
- **无回退**：stack 缺失 → `raise RuntimeError` 暂停。页面已落盘，修好 stack 后重跑从 3.7 恢复（write_phase marker 跳过 3.1-3.5）。

---

## （已移除）Phase 4：Validation — 对齐 NashSU

原 Stage 4.1（ingest 末尾自动跑 `validate_ingest.py` 体检）**已移除**：NashSU 无 post-ingest 验证 stage。NashSU 唯一的 ingest 期检查是 schema 路由（`validateWikiPageRouting`），improved-wiki 已在**写盘期 Stage 3.1**（`_stage_3_1_auto_correct_wiki_path`）做了，故自动保留。`validate_ingest.py` 保留为**独立手动工具**（见下文"可选手动验证"）。Stage 3.7（embeddings）现为最后一个 stage，之后 `_finalize_book` 置 `ingested` 完成标记（2026-07-08 从 `stage_4_1` 改名；已消化书的 stages.json 已同步迁移，`_stage_0_2_should_skip` 读 `ingested` 为唯一完整性信号）。

---

## 强制顺序与依赖

```
0.1 → 0.2 → 1.1 → 1.2 → 1.3 → 2.2 → 2.3 → 2.4 → 2.6 → 2.9
     → 3.1 → 3.2 → 3.4 → 3.5 → 3.7

（1.2→1.3 是 image pipeline（1.3 依赖 1.2 输出，串行；1.3 内部 caption 派发 ×4 线程）。
   原先与 image pipeline 并行的 Stage 2.1 已于 2026-07-08 移除。
   2.4 含源内去重收尾[原 2.5]；Stage 2.7 已于 2026-07-12 移除）
```

关键依赖：
- 1.2 先于 1.3（先有图才能 caption）；1.2/1.3 先于 3.2（注入图引用）
- 2.2 对所有源运行（短源 1 chunk / 长源 N chunk）；2.2 必须全部 chunk 分析完才进 2.3
- 2.3 在 2.2 与 2.4 之间检测已存在 wiki 关联（wiki 为空跳过）；2.4 生成后收尾跑源内去重（原 2.5，单 chunk 跳过）；2.6 源页在 2.4 之后
- Phase 2 全在内存（2.3→2.4→2.6→2.9 串行），产出统一由 3.1 写盘
- 2.9 conditional（无 concept 或 concept <2 跳过）
- **3.1 写盘时同名 slug 走 page-merge**（NashSU parity）
- 3.4 在已写盘文件上运行；3.5 在所有页面写盘后
- 3.7 强制（缺 stack 暂停），是**最后一个 stage**；之后 `_finalize_book` 置完成标记

## Resume marker 粒度 ≠ stage 编号

上面的 2.1…3.7 编号是**叙事/可观测层**，不是崩溃恢复的实际单位。`<hash>.stages.json` 里真正的 done-marker 更粗：`stage_1_1/1_2/1_3_done`、`stage_2_2_done`（wiki-独立↔依赖的分界点；`stage_2_1_done` 已随 Stage 2.1 于 2026-07-08 移除——存量 stages.json 里残留的该 key 无害，代码不再读）、`stage_2_3_done`（覆盖 2.3+2.4）、`stage_2_9_done`（覆盖 2.5/2.6/2.9 整段；名称在 2.7 移除后保留不变，缓存兼容）、`write_loop_done`、`write_phase`、`ingested`（`ingest.py::_finalize_book` 所置的整书完成标记，非某个 stage 模块自己的标记；2026-07-08 从 `stage_4_1` 改名）。崩溃恢复是从**段边界**重启，不是逐 stage、逐 chunk。

**对未来"合并/拆分 stage"讨论的含义**：任何编号调整默认只是文档层 renumber-only，代码与 marker 不动；但有两条**载荷性边界**碰了就坏，不能移动：
1. `stage_2_2_done | stage_2_3_done` —— wiki-独立/依赖分界；批量 prefetch 靠在这里精确停住（`raise PrepareStopAfter("1.5")`）才能让下一本书的 prefetch 并行跑。
2. `write_loop_done | write_phase` —— 中间夹着 wikilink enrichment 的非幂等 handoff；合并会让 resume 重跑非幂等的 Stage 3.1 写盘，重复 merge 每一页。同时要保持 artifact-before-marker 的写序（防 2026-06-25 的静默丢失 bug），碰这段边界时不要打乱写序。

## 自动验证（ingest.py 内置）

关键 Stage 完成后有实时硬门禁（`_verify_stage_*`），失败直接 `RuntimeError`：

| Stage | 门禁检查 |
|-------|---------|
| 2.2 | chunk 分析非空（`_verify_stage_2_2_chunks`）；滚动汇总 digest 含 5 必需 key + ≥1 concept（`_verify_stage_2_1_digest`——函数名是 2.1 时代遗留，现在 2.2 汇总后运行；缓存恢复时缺有效 digest 会失效 marker 重跑 2.2） |
| 2.4 | ≥1 FILE block；source page FILE block 存在；路径正确（`_verify_stage_2_4_file_blocks`，**写盘前** in-memory 检查） |
| 2.6 | source page 必需 H2 节齐全（`_stage_2_6_validate_required_sections`，doctype-aware） |

> 硬 raise 门禁只有以上几处（外加 Phase 0 的 `verify_stage_0` ≥100 字符提取门——1.1 的 `_verify_stage_1_1_text` 质量门已于 2026-07-08 移除）。source page 的**落盘**由 2.4 的写盘前门禁保证；写盘后 `validate_stage_outputs` 只做软校验（返回 warning 列表、**不 raise**），没有 3.1 raise 门。

可选手动验证（**不再自动运行**——已为对齐 NashSU 移除）：`python3 scripts/validate_ingest.py`（全阶段体检，独立工具）。其它手动补充：
```bash
./scripts/wiki-lint.sh --summary                    # 结构性 lint（wikilink 健康）
test -d wiki/media/*/<slug> && find wiki/media/<type>/<slug> \( -name '*.jpeg' -o -name '*.png' \) | while read f; do [ -f "$f.caption.txt" ] || echo "MISSING CAPTION: $f"; done
```

## 项目特定策略

每个 wiki 项目可在 `wiki/methodology/` 写 per-project 决策页（VLM 选择、批量大小等），引用本清单，**不放本清单复制**。通用消化策略是本 skill 的责任。若对某本书有偏离本清单的处理（如用户点名跳过某 stage），在 `wiki/methodology/` 显式记录偏离及原因——显式记录 = 合规；静默偏离 = 违规。

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
