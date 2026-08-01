---
name: improved-wiki
description: "强制 Ingest Stage 清单——improved-wiki 流水线的 14 个 active Stage（含 Phase 0 前置门）+ Lint + Graph 规范，每 Stage 含作用/产物/go-no-go。用于约束 ingest 时不漏步。"
tags: [ingest, mandatory, pipeline]
related: [SKILL.md, known-issues, scanned-pdf-ocr-pipeline, image-caption-strategy]
---

# 强制 Ingest Stage 清单

improved-wiki 流水线 = **14 个 active Stage（含 Phase 0 前置门，跨 4 个 Phase: 0-3）+ Lint + Graph**（源内去重原 2.5 并入 2.4 收尾；Stage 2.7 query 生成已移除；Stage 2.9 comparison 独立生成已按 NashSU 0.6.6 退休，comparison 与其他 schema typed 页统一走 2.2→2.4）。编号即执行顺序：Phase 0–2、Phase 3 全部按编号从小到大执行（Phase 3 于 2026-08-01 重新编号，此前为对齐 NashSU 而形成的 3.4a→3.1→3.5→3.2→3.4b 乱序已消除；执行顺序未变，只改编号）。Graph 是独立命令（与 Ingest/Lint 并列，不属于 ingest 管线）。

**执行由代码强制，不靠人工遵守**：全部 stage 由 `ingest.py` 调度，agent 只答 prompt、无法跳过任何 active stage。本清单是行为说明书（每 stage 作用/产物/go-no-go），不是纪律清单。唯一仍靠 agent 自觉的规则：不得绕过 `ingest.py` 手写 wiki 页冒充消化产物。（Stage 0.1 命名检查已于 2026-07-08 接入 `_do_prepare`——每个候选文件在 0.2 去重前自动过 `stage_0_1_check_file`，违规或项目无命名规则即 raise。）

> **无静默回退策略**：ingest 路径禁止任何静默回退（caption key 缺失、caption 批次重试耗尽、embedding stack 缺失、LLM page-merge 失败、config 解析失败 → 一律 `raise RuntimeError` 暂停，不降级）。完整政策见 SKILL.md「No-silent-fallback policy」段。显式恢复路径只有：cache/stage-progress 状态损坏时告警+重置；未闭合 FILE 块的一次 exact-path 定向修复；缺失 source summary 时从完整 Stage 2 analysis 写确定性恢复页。后二者对齐 NashSU，都会打印日志，且绝不补 concept/entity 数量。

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
| 2.4 | `stage_2_4_generate_all` + `_dedup_intra_source.py` | 全部分析完成后一次整书生成：**强制源页** + key 概念/实体 + schema-typed 页（含 comparison/synthesis/finding/thesis/methodology；整书上下文与源锚定）+ 源内概念去重收尾 |
| 2.6 | _(已并入 2.4，对齐 NashSU 0.6.6，2026-08-01)_ | 原独立源页调用；源页现由 2.4 同一次调用产出，缺失时写确定性 fallback（`_ensure_source_page`） |
| 2.7 | _(已移除，对齐 NashSU，2026-07-12)_ | 原问题生成 + 跨源 query 解析（信号改走 3.4 REVIEW suggestion → process-reviews） |
| 2.9 | _(已移除，对齐 NashSU 0.6.6，2026-07-28)_ | comparison 并入 2.2→2.4 schema-typed 生命周期 |
| 3.1 | `stage_3_1_prepare_review_suggestions` | 写盘前对 in-memory FILE generation 运行内容质量审查并严格校验；只保存 checkpoint，不写 REVIEW 页（原 3.4a） |
| 3.2 | `stage_3_2_write_wiki_file` | 文件写盘（含同名 slug 三层 page-merge，NashSU parity）（原 3.1） |
| 3.3 | `stage_3_3_aggregate_repair` | 聚合修复（index/log/overview）（原 3.5） |
| 3.4 | `stage_3_4_inject_images` | 图片注入 source 页（原 3.2） |
| 3.5 | `stage_3_5_persist_review_suggestions` | 将 3.1 已校验结果写入 REVIEW 页与 runtime JSON（原 3.4b） |
| 3.6 | `save_cache` | 在页面、聚合、媒体、review 均完成后更新 ingest cache |
| 3.7 | `stage_3_7_embed_new_pages` | 嵌入向量化（配置 provider；默认本地 Ollama bge-m3）— **最后一个 stage**，之后 `_finalize_book` 置完成标记 |

Phase 划分：0 前置检查 / 1 提取 / 2 分析生成 / 3 写入富化。
（无 Phase 4：post-ingest 验证体检已为对齐 NashSU 移除——NashSU 无此 stage。NashSU 唯一的 ingest 期检查"schema 路由"在写盘期的 Stage 3.2 做；`validate_ingest.py` 保留为独立手动工具。）

---

## Phase 0：Pre-Ingest Gates

### Stage 0.1 · Raw 文件命名规范检查
- **作用**：确保 raw/ 下文件符合项目命名规范（规则块以 `<project>/schema.md` 的 ```yaml 为准；datasheet 厂商表另在 `raw/Datasheet/VENDORS.yaml`）。
- **流程**（2026-07-08 起代码强制）：`_do_prepare` 对每个候选文件调 `stage_0_1_check_file`——schema.md 缺失或无规则块 → raise（先起草规则）；违规 → raise（`normalize_raw_names.py --fix` 重命名后重跑）。范围与全库扫描一致：仅检查规则声明文件夹下的 `.pdf`（`.md` 天然放行，含 deep-research 直接摄取的 `wiki/queries/*.md`）；warn 级启发式不阻断。全库批量检查/修复仍用 `normalize_raw_names.py --check/--fix`。
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
- **影响**：2.4/2.6 的 `global_digest` 数据源从 2.1 改为 2.2 滚动最终值（`_run_chunk_pipeline` 返回 5 元组含 `global_digest`）。`stage_2_1_done` marker 去掉（已消化书 stages.json 残留无害，代码不再读）。`_verify_stage_2_1_digest` 迁移到 2.2 完成后校验滚动最终 digest。`_stage_2_1_global_digest` / `_stage_2_1_build_prompt` 已作为 dead code 清理；`_stage_2_1_chunk_text`（切块函数）保留供 2.2 用。

### Stage 2.2 · Chunk Analysis
- **作用**：对源文本切块分析。chunk 大小由 context probe 动态决定（`target_tokens = min(64K, ctx×0.33)`，见 `references/context-probe.md`）：短源 1 块；长源按 chunk 预算切分。每 chunk 输出 `entities_found`/`concepts_found`/`claims`/`source_quotes`/`formulas`/`connections_to_existing_wiki`/`schema_typed_candidates`/`updated_global_digest`。
- **Schema/Purpose 上下文（NashSU 0.6.6 parity）**：把根目录
  `schema.md` 的语义部分作为 AUTHORITATIVE 路由/Frontmatter 契约注入每个
  chunk；机器命名 YAML 仅供 Stage 0.1，不进入 LLM 上下文。可选
  `purpose.md` 同时注入，用于内容优先级而非改写事实。另把
  `wiki/index.md` 按 NashSU 的 40K 上限冻结为每源快照注入；超大 index
  优先保留 synthesis/thesis 分区，使分析能复用并更新既有 living pages，
  但 index 标题/描述本身不当作事实证据。候选类型来自结构化
  `type→dir` 表；仅排除 ingest 自管的 source/entity/concept、用户发起的
  query 与应用维护的 overview。comparison、synthesis、finding、thesis、
  methodology 和自定义 schema 类型都可作为 typed candidate。
- **NashSU 对齐（2026-07-08；0.6.6 typed 扩展于 2026-07-28）**：`accumulated_digest` 初始空（不再种子自 2.1），每 chunk 产出 `updated_global_digest` 滚动合并（NashSU `Updated Global Digest` parity）。2.2 完成后，最终 `accumulated_digest` 解析回 dict 作 `global_digest` 给 2.4/2.6。短源（1 chunk）= 整本 digest（对齐 NashSU 短源 Step 1）。`updated_global_digest` 必含 5 字段（book_meta/outline/key_entities/key_concepts/key_claims），可选第六字段 `schema_typed_candidates` 只保留后续 chunk 需要的真正重要候选；首 chunk 建立 book_meta+outline。
- **NashSU 对齐 · digest 传递量与颗粒度**：chunk→chunk 传递的是**紧凑 document-level digest，不是档案**——对齐 NashSU `LONG_SOURCE_DIGEST_MAX = 15_000` 固定上限 + “incorporates this chunk and preserves prior cross-chunk context”。稳定名称只为仍重要且后文需要的 concept/entity 保留；外围细节可压缩或丢弃，不再强制“所有历史名字必须存活”。每 chunk 的完整分析单独持久化，供后续从全书上下文中选择 key pages/core claims；digest 不承担全量清单职责。
- **NashSU 对齐 · 条目策略/数量**：2.2 只识别 new/materially updated 的 key entities、key concepts、genuinely supported schema-typed candidates 与 core claims；`mentioned` 仅作分析上下文，不生成页面。无各类型 page 数、每 chunk claim 数、source quote 数或响应字节数下限；QC 检查所有候选列表的 placeholder、结构和已输出 claim 的 evidence。
- **per-handoff subagent 隔离**：每 chunk fresh subagent 答单 chunk（7/8 事故政策；当晚扩展为**所有** LLM handoff 均派 fresh subagent、主对话只编排，见 `delegate-mode.md` L4）。
- **existing-slugs 相关性 cap（2026-07-09）**：chunk prompt 里的已有 wiki 页清单不再全量嵌入（6253 页曾产生单行 259KB×每 chunk，撑爆答题 subagent 的 Read），按"slug token 在本 chunk 文本中的包含率"排序取前 `_EXISTING_SLUGS_CAP=1000`（≈40K 字符，对齐 NashSU index 40K trim；2.4/2.6 早有同类 cap）。确定性排序，prompt 哈希跨 resume 稳定。
- **go/no-go**：`stages.chunks_analyzed ≥ 1`；2.2 完成后 `_verify_stage_2_1_digest` 始终校验滚动最终 digest 5 字段及类型，包含 fresh prefetch 与 cached prefetch resume，验证通过后才允许写 `stage_2_2_done`。

### Stage 2.4 · Generation（single whole-source pass）
- **作用**：2.2 **分析完所有 chunk** 后，2.3 验证已存在 wiki 关联，再对整本来源执行**一次**统一 generation，生成分析推荐的 key 概念/实体与项目 schema-typed 页。prompt 使用与 NashSU 0.6.6 同序的最终滚动 digest + 全部 chunk analyses，并额外保留每个 chunk 的有界原文证据；不能回退为按 chunk 分波/串行生成。comparison、synthesis、finding、thesis、methodology 不再有旁路或专门 stage。完整语义 schema 以 AUTHORITATIVE 形式注入；每个 genuinely supported 的 `schema_typed_candidate` 在生成前按结构化 `type→dir` 重新解析，忽略 LLM 自报的 folder。`mentioned`、passing/background 项不允许生成“补充基础页”。
- **schema 语义与数量**：每类候选都必须满足项目 schema 的语义门（例如 finding 要证据锚点、methodology 要可复用条件/步骤、thesis 要可证伪、comparison 要真实多维对比）。按 NashSU bundled schema，当前来源可建立 speculative working thesis，也可建立区别于 source summary 的 cross-cutting synthesis；后续来源经同路径合并/更新。项目 schema 若声明更严格门槛则服从项目 schema。不设各类型条数目标、下限或上限，也不再截断 typed candidate 清单（旧 per-chunk 40 / all-chunks 120 展示上限已移除）。2.2 推荐只是候选，不预先承诺建页；但 synthesis/thesis 不得仅因仍是单来源初稿或 speculative 而在 2.4 被二次静默拒绝。某次 2.4 调用若没有任何候选达到该门槛，必须只返回精确哨兵 `NO_KEY_PAGES`；普通空白、解释性文字或损坏输出仍是硬失败。source 页不受该哨兵影响：它在同一次 2.4 调用中强制产出，模型遗漏时写确定性 fallback。
- **整书上下文与预算**：`build_consolidated_stage_2_context` 在 `source_budget` 内确定性构建共享上下文，每个 chunk 在 analyses 与 raw 两段都必须有代表。**降级方式是按字段整体丢弃，不是切 JSON**（2026-07-30）：analyses 装不下时按固定优先级逐级丢整字段（`source_quotes` → `connections_to_existing_wiki` → `formulas`/`key_details` → `definition`/`significance`/`evidence`/`rationale` → `claims`），选第一个能**完整**渲染每个 chunk payload 的档位，使每份分析始终是可解析对象。若最终 digest 或最低明细档位仍超限，则用可解析的 JSON head/tail envelope 明示截断，绝不从字符串中部切断语法。旧实现按 chunk 均分后用 balanced excerpt 切，实测 20 chunk（`source_budget=104,000`）需 198,130 字符只给 ~60,000，等于把 20 份从 JSON 中间切断的碎片喂给生成模型。raw 证据改为**吃剩余预算**（不再按固定 0.28/0.68 份额预切）：短源保留全文，长源把预算让给跨 chunk analyses（实测 20 chunk raw 从 ~28K 升到 ~42K）。被丢弃的字段与档位写进上下文自身的 `## Context Budget` 段并打印一行——不静默截断。改档位表或份额必须同步 `STAGE_2_CONTEXT_POLICY_VERSION`（= `GENERATION_POLICY_VERSION`，尚未跨写盘边界的 2.3+ 缓存会失效重跑）。Stage 2.4 的 generation token ceiling 对齐 NashSU 0.6.6：64K/128K/256K/512K context 分别为 8K/16K/24K/32K。
- **明确的 improved-wiki 扩展（不改变 NashSU 主顺序）**：生成前的 Stage 2.3 用 `stage_2_3_detect_incremental_associations` 将候选与真实页面匹配并保留 type-prefixed exact path；同类型命中是 **UPDATE EXISTING** 目标，跨类型命中只链接。`stage_2_3_resolve_proposed_connections` 另验证 2.2 自报连接。生成后的源内语义去重（原 Stage 2.5）使用 embedding 初筛（cosine ≥0.82）+ LLM 确认；embedding 不可用则暂停，不回退 Jaccard。两项都是 improved-wiki 扩展，但 Stage 2.4 仍只有一次整书 generation。
- **子步骤（生成后）· 源页生成**：`stage_2_6_source_page` 复用与 Stage 2.4 **完全相同、确定性重建且不重复缓存**的整书上下文，生成一个简洁、自由结构的 source summary 并入 file_blocks。只选核心论点/证据和最相关 wikilink；不列出全部生成页、全部章节主题或全部 chunk claims；无固定 H2/条目数量。它仍是独立的 resumable call，而不是重复使用原文前缀。若 source FILE 块未闭合，先 exact-path 定向修复；若仍缺失/不合规，使用 NashSU deterministic fallback，把完整 Stage 2 analysis 原样保留到最低限度 source 页（不截断、不另调 LLM）。go/no-go：最终恰好一个、路径为 `wiki/sources/<stem>.md`、frontmatter/END marker 完整且正文非空。
- **产物**：FILE blocks（`---FILE:wiki/<path>---...---END FILE---`）。
- **go/no-go**：2.4 可产生 0 个可选 key/schema-typed FILE block；`stages.file_blocks_generated ≥ 1`，且 source page FILE block 存在（`_verify_or_die` 硬门禁，源页由本次调用或确定性 fallback 保证）。概念页目录**不是**硬门禁：`_stage_validators.py` 只对路径异常打印告警，真正的归位由 Stage 3.2 的 schema 路由在写盘时自动纠正。
- **失败处理**：0 个新/更新 key/schema-typed 页可以是合法结果（无候选、均由其他 chunk 覆盖、只有跨类型 link-only 关联，或模型以精确 `NO_KEY_PAGES` 判断候选均未达到独立建页/实质更新门槛）。同类型已有页本身不是跳过来源新贡献的理由，但边缘性提及也不强制制造更新。解析器丢弃未闭合的 FILE block，并把其安全路径交给一次 targeted repair handoff；repair 只接受请求路径，额外页面全部丢弃。若任一已经开始但未闭合的推荐路径仍未恢复则硬暂停；绝不运行“逐条目全量补齐”。

### Stage 2.7 · Query Auto-Generation（已移除，对齐 NashSU，2026-07-12）
- **原作用**：基于 2.4 的 concept/entity 生成 0-5 个开放问题 query 页 + 跨源 query 解析收尾（原 2.8）+ queries/index.md 维护。
- **为什么去掉**：NashSU 的 ingest 从不生成 query 页（生成清单只有 source/entities/concepts/index/log/overview + REVIEW 块）；NashSU 中 `queries/` = 保存的聊天回答 + 深度研究结果，只来自用户主动行为。
- **信号去向**："本书提出但未回答的研究问题"由 Stage 3.4 的 REVIEW `suggestion` item（含 `search_queries`）承接，经 `/improved-wiki process-reviews` 人工裁决（Deep Research → query 页带答案落地 / Create Page / Skip）。详见 `query-generation.md`（墓碑）与 `process-reviews.md`。
- **影响**：`stage_2_9_done` resume marker 名称保留（缓存兼容）；`queries_generated` 缓存统计移除；已存在的 query 页保留不动。

### Stage 2.9 · Comparison Auto-Generation（已移除，对齐 NashSU 0.6.6，2026-07-28）
- **为什么去掉**：NashSU 0.6.6 把 comparison 当普通 schema-declared page type，由 analysis 的 typed recommendation 与统一 FILE generation 处理；没有独立 comparison LLM call、固定正文模板、zero sentinel 或数字 cap。
- **当前路径**：comparison 与 synthesis/finding/thesis/methodology 一样走 2.2 `schema_typed_candidates` → 2.3 association/dedup → 2.4 unified FILE blocks。现有 comparison 页保留。
- **兼容性**：`stage_2_9_done` 仅作为旧 checkpoint 的 marker 名称保留，现覆盖 2.4 去重收尾 + 源页保证（`_ensure_source_page`）；详见 `comparison-generation.md`。

---

## Phase 3：Write & Enrich

### Stage 3.1（生成）+ 3.5（持久化）· Review
- **作用**：满足 NashSU 3 条件（≥4 FILE 块 / ≥10K 字符 / 未闭合 REVIEW）时跑一次 LLM，输出 5 类 review items（confirm/suggestion/missing-page/contradiction/duplicate）。3.1 在 3.2 之前审查 in-memory FILE generation、严格解析校验并把规范化 items 写入 `review_prepared` checkpoint；3.5 在 3.2→3.3→3.4 后把同一批 items 持久化为 `wiki/REVIEW/<type>/<date>-<source>-<slug>.md` + `review-suggestions.json`。两段之间不做第二次 LLM 调用。
- **审查输入 = 写盘投影（2026-07-30）**：3.1 拿到的不是原始生成块，而是 `project_write_result_blocks` 的**确定性投影**——与写循环共用 `resolve_ingest_write_path`（路径安全/聚合页丢弃/auto-correct/`.md`/schema 路由），再跑同一条 sanitize → canonicalize sources → stamp dates → `stage_3_2_normalize_page_links(strict_missing_targets=True)`。原因：这两步都是确定性的，审原始草稿会让 reviewer 为**随后会被写时去链的链接**开 `missing-page`（落盘即已解决），并让 `affected_pages` 指向 schema 路由前的旧路径（REVIEW 页里渲染成断链）。投影**不做** page merge——合并进已有页的部分仍以本源贡献呈现，这与 NashSU pre-write reviewer 看到的内容一致。三处（写循环 / `slug_dirs` / 投影）共用同一个 resolver，禁止再出现第四份副本。
- **go/no-go**：review items 数量 ≥0（空数组 `[]` 合法）；非空 item 必须完整通过严格 schema：`type`/`severity` 枚举合法，title/description 非空，`affected_pages` 是 wiki 内安全 `.md` 路径，suggestion/missing-page 恰有 2–3 条搜索 query，其余类型 query 为空。整批先校验后写盘，任何非法 item 都 hard-fail，禁止静默跳过和路径穿越。成功后以 `review_done` 绑定 review page refs。
- **NashSU 顺序对齐**：review generation 与 validation 已移到 `writeFileBlocks` 之前；review artifact persistence 保持在 aggregate/media 之后，与 NashSU 的 parse/store 顺序一致。`review_prepared` marker 保存已验证 items，因此写盘或后续 handoff 失败后恢复不会重复调用 reviewer。

### Stage 3.2 · Write files（含 source page gate）
- **作用**：Phase 3 唯一磁盘写入入口。先 source page gate；若 LLM/旧缓存仍未提供 source 页，按 NashSU 从**完整 Stage 2 analysis**（滚动 digest + 全部 chunk analyses，不截断）生成确定性最低限度 source summary，再原子写盘（.tmp → rename）。
- **NashSU 0.6.6 更新语义**：同路径已有页若 `sources` 全部解析为当前来源，说明它只由该来源拥有；纠正来源重摄取时用新正文替换旧正文，同时 union `sources/tags/related`、锁定 `type/title/created` 并更新时间，避免被撤回的旧表述经 merge 永久残留。只要存在其他来源，仍走三层 page-merge，保留其他来源贡献。两条路径都先备份旧页。
- **同轮 slug 碰撞例外（2026-07-30）**：上面的替换语义只针对**上一次消化**留下的页。本轮写循环已写过的同路径页必须走真合并——`_is_same_run_collision` 把它标出来并强制 `replace_existing_body=False`。否则"只被本源拥有"这条判据由构造恒成立（3.2 刚把 `sources` 规范化成当前源），第二个 FILE 块会静默丢掉第一个块的正文：典型是 2.4 多吐一个 `wiki/sources/<stem>.md` 块覆盖同一次生成里的真源页，或两个候选名 slugify 撞车。碰撞时打印一行 `same-slug collision`，不静默。
- **合并后规范化**：入站 FILE block 在 merge 前规范化一次；多来源 LLM merge 完成后必须对**实际合并结果**再规范化一次，清掉旧页带入的畸形 `related`，并在同 stem 只有一个真实目标时纠正 body wikilink 的错误/大小写不匹配目录前缀。不能只规范化 merge 输入，否则 merger 会重新引入坏链接。
- **go/no-go**：任一 FILE block 或 deterministic source fallback 写失败即停止；只保留成功页用于诊断，不写 `write_loop_done`/`write_phase`。正常 source 的 source page 必须已落盘。

### Stage 3.3 · Aggregate Repair
- **作用**：紧接 3.2 写盘后执行：log.md 程序化 append（同一 source identity + hash 幂等，不重复追加）+ index.md LLM 整页重写（失败/超容量/>250 页时 Sources 单行 append）+ overview.md 尽力重写。
- **go/no-go**：log.md 必须含本 source/hash 的 INGEST block，index.md 必须含 source link；两页以 `aggregate_done` 绑定。overview 是可选修复，不作为完成硬门禁。`ingest-cache.json` 不在本 stage 内写；它在 3.4 与 3.5 之后更新，并与 task manifest 的完整 page refs 一致。

### Stage 3.4 · 图片注入
- **作用**：在 source 页末尾追加 `## Embedded Images` 段，列出所有图 + caption。
- **执行位置**：在 3.3 aggregate repair 之后、3.5 review persistence 之前，复现 NashSU 0.6.6 的 image injection 时机。
- **go/no-go**：`media_policy=required` 时 `images_injected == images_extracted`，否则不写 `write_phase` marker。

### Stage 3.7 · Embeddings
- **作用**：按 NashSU 0.6.6 的 ingest 生命周期，只把本次实际写入/更新的 knowledge pages 重新 chunk，并以 page 为单位替换其 LanceDB rows；不再为每本书隐式全库重建。
- **chunk/embedding 行为**：NashSU `text-chunker.ts` 直接移植——target/max/min/overlap 默认 `1000/1500/200/200`，按 section→paragraph→line→sentence→space 递归切分，frontmatter 不入向量，fenced code/table 不拆，向量输入为 `title + heading breadcrumb + raw chunk`。OpenAI-compatible batch 必须严格校验数量、index、有限数值和统一维度；batch 失败退回逐条，oversize 按字符边界最多自动减半 3 次。
- **后端**：默认本地 Ollama bge-m3（兼容旧项目），也接受 `EMBEDDING_ENDPOINT` 指定 Google、Volcengine/Doubao 或 OpenAI-compatible 的完整 request endpoint；并发、batch、chunk 参数均可配置。单请求 timeout 默认与 NashSU 0.6.6 一致为 8 秒，可用 `EMBEDDING_TIMEOUT_SECONDS` 覆盖；旧 `EMBEDDING_BASE_URL` 仍兼容并自动追加 `/embeddings`。
- **产物**：`.llm-wiki/lancedb/wiki_chunks`。旧 `embed-cache.json` 不再参与 ingest 或 full re-index；文件可作为旧版运行遗留保留，确认无旧 embedding 进程后再人工清理。
- **go/no-go**：本次 touched page 的每个预期 chunk 都取得合法、同维向量；page replacement 后逐页 row count 必须与预期完全相等。任何 partial response / 缺向量 / 维度不一致 / 写后行数不一致均失败，不得置 `ingested`。
- **全量重建**：仅显式执行 `build_embeddings.py --project <root> embed`；先准备全部当前 chunk 的向量，全部成功后才 overwrite live table，并验证最终 row count。
- **删除生命周期**：`ingest.py --delete` 与 lint orphan cascade 在文件成功删除后按 page id 清除对应 rows；清理失败按 NashSU 视为 non-critical 并明确告警。显式单页清理可用 `build_embeddings.py --project <root> delete --page <wiki-relative.md>`。手工旁路删文件后需 full re-index。
- **升级迁移**：旧索引采用旧 chunk 边界且没有版本元数据，不能安全地与新规则自动判别。升级后先显式 full re-index 一次；之后普通 ingest 才会稳定保持 page-scoped 增量更新。
- **无回退（ingest）**：stack 缺失或 touched-page coverage 不完整 → `raise RuntimeError` 暂停。页面已落盘，修好后重跑从 3.7 恢复（`write_phase`、`review_done`、`aggregate_done` 分别跳过已完成段）。搜索侧则按 NashSU 报警后 keyword-only，不把搜索降级等同于 ingest 完成。
- **为何 NashSU 可选而 improved-wiki 强制**：NashSU 的核心检索仍可用 keyword + graph，向量索引是可失效的搜索增强，因此 ingest 捕获 embedding 错误后仍可返回已写页面；improved-wiki 有意采用更强的完成语义：`ingested` 必须同时证明 Markdown 页面和语义索引同步。故 ingest 期 upsert 失败停在 3.7、修复后从 checkpoint 恢复；只有搜索请求本身允许按 NashSU 降级到 keyword-only。
- **最终完成门禁**：embedding 前必须同时证明 media 完整、4 个 post-write markers 齐全、cache/source hash/task manifest/page refs 一致、所有页面存在且非空；否则不得置 `ingested`。

---

## （已移除）Phase 4：Validation — 对齐 NashSU

原 Stage 4.1（ingest 末尾自动跑 `validate_ingest.py` 全量体检）**已移除**：`validate_ingest.py` 保留为独立手动工具。Stage 3.7（embeddings）仍是最后一个生成 stage；之后 `_finalize_book` 仅在轻量、确定性的 artifact completion gate 全通过时置 `ingested`（不是恢复全量内容质量 audit）。`_stage_0_2_should_skip` 仍以该 marker 决定 skip，但 marker 本身已由 media/task/cache/page 一致性门禁保护。

---

## 强制顺序与依赖

```
0.1 → 0.2 → 1.1 → 1.2 → 1.3 → 2.2 → 2.3 → 2.4 → 2.6
     → 3.4a(review generate/validate) → 3.1 → 3.5 → 3.2
     → 3.4b(review persist) → cache → 3.7

（1.2→1.3 是 image pipeline（1.3 依赖 1.2 输出，串行；1.3 内部 caption 派发 ×4 线程）。
   原先与 image pipeline 并行的 Stage 2.1 已于 2026-07-08 移除。
   2.4 含源内去重收尾[原 2.5]；Stage 2.7 与独立 Stage 2.9 已移除）
```

关键依赖：
- 1.2 先于 1.3（先有图才能 caption）；1.2/1.3 先于 3.2（注入图引用）
- 2.2 对所有源运行（短源 1 chunk / 长源 N chunk）；2.2 必须全部 chunk 分析完才进 2.3
- 2.3 在 2.2 与 2.4 之间检测已存在 wiki 关联（wiki 为空跳过）；2.4 对整书只生成一次，随后收尾跑源内去重（原 2.5，单 chunk 跳过）；2.6 复用同一整书上下文并在 2.4 之后生成源页
- Phase 2 全在内存（2.3→2.4→2.6 串行），产出统一由 3.1 写盘
- 3.4a 在 3.1 前审查并校验 in-memory generation；`review_prepared` 让 resume 不重复调用 reviewer
- **3.1 写盘时同名 slug 走 page-merge**（NashSU parity）
- 3.5 紧随 3.1；3.2 注图后由 3.4b 持久化已校验 review，再更新 cache
- 3.7 强制（缺 stack 暂停），是**最后一个 stage**；之后 `_finalize_book` 置完成标记

## Resume marker 粒度 ≠ stage 编号

上面的 2.1…3.7 编号是**叙事/可观测层**，不是崩溃恢复的实际单位。`<hash>.stages.json` 里真正的 done-marker 更粗：`stage_1_1/1_2/1_3_done`、`stage_2_2_done`（wiki-独立↔依赖的分界点）、`stage_2_3_done`（覆盖 2.3+单次整书 2.4 generation）、`stage_2_9_done`（历史名称，仅覆盖 2.4 去重收尾 + 2.6 source page tail；为缓存兼容保留）、`review_prepared`（3.4a 已验证 items）、`write_loop_done`、`aggregate_done`、`write_phase`、`review_done`、`ingested`。`generation_policy_version` 与 `stage_2_3_done` 的 file_blocks 一起持久化：尚未跨过写盘边界的旧 per-chunk cache 只失效 2.3+、保留 2.2；已经写盘的旧任务安全续完，若要采用新策略必须显式 re-ingest。写盘后的 marker 都携带 page refs/count payload；`review_prepared` 携带规范化 review items。崩溃恢复逐段验证并恢复，不把“marker 存在”当作足够证据。

**对未来"合并/拆分 stage"讨论的含义**：任何编号调整默认只是文档层 renumber-only，代码与 marker 不动；但有两条**载荷性边界**碰了就坏，不能移动：
1. `stage_2_2_done | stage_2_3_done` —— wiki-独立/依赖分界；批量 prefetch 靠在这里精确停住（`raise PrepareStopAfter("1.5")`）才能让下一本书的 prefetch 并行跑。
2. `write_loop_done | write_phase` —— 中间夹着 wikilink enrichment 的非幂等 handoff；合并会让 resume 重跑非幂等的 Stage 3.1 写盘，重复 merge 每一页。同时要保持 artifact-before-marker 的写序（防 2026-06-25 的静默丢失 bug），碰这段边界时不要打乱写序。
3. `review_prepared | write_loop_done` —— 前者固定 pre-write review 结果，后者开始记录磁盘写入；若丢掉该边界，resume 可能对已变更的磁盘页面再次调用 reviewer，破坏 prompt/结果稳定性。
4. `aggregate_done | write_phase | review_done` —— log/index、media 完整性、review artifacts 分别绑定自己的页面集合；cache/finalization 只能消费三者均完成后的并集，避免重复 append log、重复注图或重写 review。

## 自动验证（ingest.py 内置）

关键 Stage 完成后有实时硬门禁（`_verify_stage_*`），失败直接 `RuntimeError`：

| Stage | 门禁检查 |
|-------|---------|
| 2.2 | chunk 分析结果齐全且无 error；滚动汇总 digest 含 5 必需 key 且类型正确（无 ≥1 concept 数量门槛；`_verify_stage_2_1_digest` 函数名是 2.1 时代遗留） |
| 2.4 | 全部 chunk analysis 已完成；整书只执行一次 generation；可选 key/schema-typed 页可为 0（仅精确 `NO_KEY_PAGES` 可作为模型主动弃权）；与 2.6 source block 合并后 ≥1 FILE block、source page 存在且路径正确（`_verify_stage_2_4_file_blocks`，**写盘前** in-memory 检查） |
| 2.6 | 首次响应先解析完整 FILE block；未闭合 exact path 做一次 targeted repair；仍缺失/错误则从完整 Stage 2 analysis 生成 deterministic fallback。最终必须恰好一个 exact-path、frontmatter/END 完整且正文非空的 source block；不检查固定 H2 或 claim 数 |
| 3.4a | review YAML 严格 schema + wiki 内安全路径；整批校验后才写 `review_prepared`，不写 REVIEW artifacts |
| 3.1 | 写入无 hard failure；成功页先落盘再写 `write_loop_done` |
| 3.5 | log source/hash 与 index source link 两个确定性 postcondition |
| 3.2 | required media 全量注入后才写 `write_phase` |
| 3.4b | 只持久化 `review_prepared` 的已验证 items；成功后绑定 REVIEW page refs |
| finalize | media、markers、cache、task manifest、page refs 与磁盘页面交叉一致 |

> `validate_stage_outputs` 仍是软质量校验（warning，不 raise）；上表新增的 3.x/finalize 检查是 artifact 完整性与安全门禁，不是恢复已移除的全量 post-ingest 内容质量 audit。

可选手动验证（**不再自动运行**——已为对齐 NashSU 移除）：`python3 "$SKILL_DIR/scripts/validate_ingest.py" --root "$WIKI_ROOT" --source "<source stem>"`（全阶段体检，独立工具）。其它手动补充：
```bash
"$SKILL_DIR/scripts/wiki-lint.sh" --structural-only            # 结构性只读 lint（wikilink 健康）
test -d wiki/media/*/<slug> && find wiki/media/<type>/<slug> \( -name '*.jpeg' -o -name '*.png' \) | while read f; do [ -f "$f.caption.txt" ] || echo "MISSING CAPTION: $f"; done
```

## 项目特定策略

每个 wiki 项目可在 `wiki/methodology/` 写 per-project 决策页（VLM 选择、批量大小等），引用本清单，**不放本清单复制**。通用消化策略是本 skill 的责任。若对某本书有偏离本清单的处理（如用户点名跳过某 stage），在 `wiki/methodology/` 显式记录偏离及原因——显式记录 = 合规；静默偏离 = 违规。

---

## Graph 命令（独立，与 Ingest/Lint 并列）

Graph 不在 ingest 管线内。Ingest 管线不碰图——图建在 Graph 命令，图用在 Ingest 之外（`--mode query --slug <page>` 是只读工具，为任意页面返回 top-N 建议缺失 wikilink，不自动改文件、不在 ingest 管线内调用）。触发：仅手动运行 `python3 "$SKILL_DIR/scripts/graph.py"`（ingest/lint 不自动触发，对齐 NashSU：NashSU 无 post-ingest 图重建）。详见 `graph.py --help`。

- **四信号图构建**：解析 wikilinks + `related:` + frontmatter，构建 networkx 加权无向图（direct link ×3.0 / source overlap ×4.0 / Adamic-Adar ×1.5 / type affinity ×1.0）。产物 `<runtime>/graph.json`。大书（>100 页/源）source-overlap 改用 star（成员↔source 页 hub）避免 N² clique；AA 丢弃 <0.2 的 hub 噪声对。
- **Louvain 社区检测**：社区检测 + cohesion 评分（<0.15 标记低质量）；大图 betweenness 用采样近似。
- **图谱洞察**：`wiki/REVIEW/knowledge-gaps.md`（孤立节点/桥接节点/建议缺失链接）+ `wiki/clusters/cluster-NNN.md`（社区 hub 页）。

```bash
python3 "$SKILL_DIR/scripts/graph.py" --wiki-root /path/to/wiki              # 全量
python3 "$SKILL_DIR/scripts/graph.py" --wiki-root /path/to/wiki --dry-run    # 仅统计
python3 "$SKILL_DIR/scripts/graph.py" --wiki-root /path/to/wiki --mode query --slug "page"  # 查询建议
```
依赖：`pip install networkx pyyaml`（networkx 3.x 内置 Louvain，无需 python-louvain）。

---
