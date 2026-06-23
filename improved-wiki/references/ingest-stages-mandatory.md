---
name: improved-wiki
description: "强制 Ingest Stage 清单——基于 NashSU v0.4.25 autoIngestImpl() 流水线的 20 个编号 ingest Stage + 2 个前置门（0.1/0.2）+ Lint + Graph 规范，每个 Stage 含作用/跳过代价/产物/go-no-go 判断。用于约束任何 wiki 项目执行 ingest 时不漏步。"
tags: [ingest, mandatory, nashsu, pipeline]
related: [SKILL.md §7, known-issues, multimodal-vlm-pitfalls]
---

# 强制 Ingest Stage 清单

## 为什么需要"强制"？

Karpathy LLM-Wiki 模式 + NashSU LLM Wiki app (v0.4.25) 的 `autoIngestImpl()` 流水线包含 **Phase 0（2 个前置门：0.1 源页去重 / 0.2）+ 20 个编号 ingest Stage + Lint + Graph**（编号与 `ingest.py` 代码一致）。**Ingest 任何一个 Stage 都不能跳过**——即使后续 Stage 看起来成功了，也不能"先跑再说"。**Graph 是独立命令**（与 Ingest/Lint 并列，不属于 lint），见下文「Graph 命令」段。

**跳过的代价**：
1. **raw 是 sacred**（Layer 1 原则）—— PDF 里的图也是 raw 的一部分，跳过图片提取 = 丢了一半知识
2. **审计可追溯性**（三层模型）—— 缺了 stage 产物，审计时无法回溯"为什么这个页面这么写"
3. **增量缓存前提**（ingest cache）—— 不写 hash cache，下一次 ingest 不知道哪些文件已处理
4. **错误累积**（NashSU 实测）—— 跳过的 stage 永远不会被补做，错误会一直留在 wiki 里

**违反此清单的代价**已在 2026-06-11 HardwareWiki 第一次 ingest 中真实发生：漏掉 Stage 1.2/0.6 后，source 页面里没有任何图片引用——因为没强制流程就没人会回头补。

## 阶段编号说明

本文件所有 Stage 编号已与 `ingest.py` 代码对齐。对应关系如下：
- **本文编号**：各章节 `### Stage X.Y` 使用的编号（与代码一致）
- **代码函数名**：代码中对应的实现函数

**Phase 编号体系**（优先级：Phase > Stage）：
- **Phase 0**：Pre-processing gates（0.1-0.2）
- **Phase 1**：Extraction（1.1-1.3）
- **Phase 2**：Analysis & Generation（2.1-2.9）
- **Phase 3**：Write & Enrich（3.1-3.7）
- **Phase 4**：Validation（4.1）

| 本文编号 | 代码函数名 | 说明 |
|---------|-----------|------|
| 0.1 | `normalize_raw_names.py --check` | raw 命名规范检查（前置门） |
| 0.2 | 源页存在性检查（`wiki/sources/<rel>.md`） | 源页去重 |
| 1.1 | `stage_1_1_extract_text` / `_stage_1_1_detect_pdf_type` | 文本提取 + PDF 类型检测 |
| 1.2 | `stage_1_2_extract_images` | 图片提取 |
| 1.3 | `stage_1_3_caption_images` | 图片 caption |
| 2.1 | `stage_2_1_global_digest` | 全局摘要 |
| 2.2 | `stage_2_2_chunk_analysis` | 逐 chunk 分析（NashSU 顺序递进） |
| 2.3 | `_stage_2_3_detect_incremental_associations`（`_stage_2_3_incremental.py`） | 增量关联检测（现有 wiki 重叠） |
| 2.4 | `_stage_2_4_generate_chunk`（barrier-free 循环，per-chunk）+ `_stage_2_4_per_concept_fallback`（0 块时兜底） | 概念/实体逐 chunk 生成 |
| 2.5 | `_stage_2_5_extract_concept_blocks`/`_stage_2_5_apply_merge_rules`（`_stage_2_5_dedup.py`） | 源内概念去重合并（多 chunk） |
| 2.6 | `stage_2_6_source_page` | 源页面生成 |
| 2.7 | `stage_2_7_query_generation` | 问题生成 |
| 2.8 | `_stage_2_8_resolve_queries`（`_stage_2_8_query_resolve.py`） | 跨源 query 解析（LLM judge） |
| 2.9 | `stage_2_9_comparison_generation` | 对比生成（2.9A/B/C） |
| 3.1 | `stage_3_1_write_wiki_file`（主写入循环）| 文件写盘 |
| 3.2 | `stage_3_2_inject_images` | 图片注入 |
| 3.3 | `stage_3_3_slug_collision_review`（`_stage_3_write.py`） | 跨域 slug 碰撞审查（标记消歧义） |
| 3.4 | `stage_3_4_review_suggestions`（`_stage_3_4_review.py`） | 生成内容质量审查（发现问题，不修复；运行在已写盘文件上） |
| 3.5 | `stage_3_5_aggregate_repair` | 聚合修复 + 缓存 |
| 3.6 | `_stage_3_6_calculate_quality_score`（`_stage_3_6_quality.py`） | 质量评分卡 |
| 3.7 | `stage_3_7_embed_new_pages`（`ingest.py` post-ingest） | 嵌入向量化（强制尝试，本地 Ollama bge-m3）|
| 4.1 | `stage_4_1_validate_ingest`（`ingest.py`） / `validate_ingest.py` CLI | 最终验证 |

> **编号即执行顺序**：本文 Stage 编号采用「Phase.Stage」形式（Phase 0 前置检查 / 1 原始素材提取 / 2 消化主流程 / 3 材料写入 / 4 验证检查），编号从上到下严格递增，与代码实际执行顺序一致（2026-06-20 重编号，旧编号 2.0/2.5rev/2.6 实际在 Stage 3 之后执行的错位已消除）。

## 强制 Stage 清单（20 个编号 Stage + 3 个前置门）

每个 Stage 都标了：
- **作用**：该 Stage 做什么
- **跳过代价**：跳过的具体后果
- **产物**：Stage 完成后必须存在的文件
- **go/no-go 判断**：怎么知道这个 Stage 算"真的完成"了

## Phase 0：Pre-Ingest Gates

Phase 0 包含 3 个前置检查，必须按顺序执行：

### Stage 0.1：Raw 文件命名规范检查 ⭐ **强制执行**

> **定位**：此阶段检查的是 raw/ 目录的**管理状态**（命名规则是否存在、新文件是否合规），而非单个文件的消化进度。有独立的检查逻辑。

- **作用**：确保 raw/ 下的候选文件符合该项目的命名规范。**每个知识库项目的 raw 命名规则是项目特定的，记录在 `<project>/raw/NAMING.md`**。
- **跳过代价**：不规范的文件名导致 source 页面路径混乱、wikilink 不可解析，且一旦 ingest 完成后再改 raw 文件名会导致已有 wiki 页面变孤儿。
- **检查流程**：
  1. 检查 `<project>/raw/NAMING.md` 是否存在
     - **不存在** → 🛑 **阻止 ingest，提醒用户先制定规则。** 列出 raw/ 下的文件夹和文件样本，帮用户起草 `raw/NAMING.md`（含 `yaml rules` 块）。参考 `references/raw-naming-conventions.md`。
     - **存在** → 继续步骤 2
  2. 检查候选文件是否符合规则
     - 运行共享脚本 → `python3 ~/.agents/skills/improved-wiki/scripts/normalize_raw_names.py --check`
     - 如果只有 `NAMING.md` 没有脚本 → 手动对照规则检查
  3. 不符合 → 🛑 **阻止 ingest**，列出违规文件和修正建议
  4. 全部符合 → ✅ 进入 Stage 0.2
- **go/no-go 判断**：
  - `raw/NAMING.md` 存在 **且** 候选文件全部合规 → 进入 Stage 0.2
  - 否则 → 🛑 阻止 ingest

### Stage 0.2：源页去重检查 ⭐ **任何文件选取前强制执行**

- **作用**：检查候选文件是否已消化。**唯一判断依据：`wiki/sources/<raw-rel-path>.md` 是否存在。** `<raw-rel-path>` = raw 文件相对于 `raw/` 的路径（去掉 `.pdf` 后缀），镜像 `raw/` 的目录结构。源页是 Stage 3.2 写入的不可变记录，永远不会被 pipeline 删除或覆盖。源页存在 = 消化完成 → 跳过。不存在 → 进入 Stage 1.1。
- **跳过代价**：重复消化已完成的书籍，浪费 LLM token、OCR 时间，且并行场景下可能导致 index.md / log.md 竞态覆盖。
- **为什么只用 `wiki/sources/`，不查 `ingest-cache.json`**：
  - **`wiki/sources/` 是不可变记录**：每个成功的 ingest 在 Stage 3.2 写入一个源页，pipeline 永不删除或覆盖它。
  - **`ingest-cache.json` 不可靠**：2026-06-14 HardwareWiki 两次事故——(a) agent 忽略缓存选了已消化的书；(b) 10 本书源页存在但缓存缺失。缓存可以被手动删除、跨对话丢失、runtime 目录切换后找不到、并发写入损坏。它只适合作为 ingest.py 内部的性能优化（跳过哈希计算），**绝不用于去重判断**。
- **产物**：过滤后的待消化文件列表（过滤残缺 ingest：源页存在但引用的 concepts/entities >80% 丢失的会重新消化）。
- **go/no-go 判断**（2026-06-17 改为完整性校验，不只是检查源页存在）：
  - `wiki/sources/<raw-rel-path>.md` 存在 **且** 解析 `[[wikilinks]]` 验证 >80% 的 concepts/entities 页面存在 → 跳过。
  - 源页存在但引用的 concepts/entities 丢失 >80%（或源页无任何 wikilinks）→ 不跳过，重新消化。**防止上次 ingest 中途崩溃后留下残缺源页。**
  - `wiki/sources/<raw-rel-path>.md` 不存在 → 未消化，进入 Stage 1.1。
  - **不依赖对话历史、agent 记忆、`ingest-cache.json`、或文件名猜测。**

## Phase 1：Extraction（文本、图片、字幕）

### Stage 1.1：PDF 文本提取（按 PDF 类型分两路径）

**先判断 PDF 类型**（2026-06-18 改为跳过首尾随机采样 + 四信号检测），采样方式：跳过首页（封面/扉页）和末页（索引/封底），从剩余中间页中随机挑 5 页。不足 5 页的短 PDF 全量采样中间页，不足 3 页的采全部页面。空白页（<10 chars）自动跳过不计入统计。按结果走不同路径：

- **信号 ①**：`get_text()` 平均 chars/page
- **信号 ②**：渲染页面低分 Pixmap，检查非白像素占比（>80% 即视为全页扫描图）。这是 **2026-06-14 Johnson《High-Speed Signal Propagation》教训**引入的补充检测——OCR 处理的扫描版 PDF，其背景扫描图可能以 PyMuPDF `get_images()` 无法枚举的形式存储（form XObject / masked image / inline image），导致信号 ③ 漏检。仅靠 `get_images()` 是不够的。
- **信号 ③**：`get_images()` 返回的嵌入图数量。**⭐ OCR处理扫描版的最可靠检测信号**。如果每页只有一个大图（>50% 页面面积），说明 PDF 本质是扫描版——OCR 文字层是后来嵌入的。2026-06-15 童诗白《模拟电子技术基础》：信号①=609c/p（文字层达标），信号②=7%（漏判），信号③=100%（每页一个嵌入大图）→ 判定为扫描版。**信号③ 必须作为三信号检测的首要判断依据。**

#### 路径 A：文本层 PDF（chars/page >500 且全页大图占比 <60%）

- **作用（2026-06-23 起，已不是 PyMuPDF 直抽；text/scanned/mixed 不再分流）**：交给本地持久化 minerU API 服务器（`mineru.cli.fast_api`），分 chunk（50 页/chunk）调 `/file_parse`，backend=`hybrid-engine`、parse_method=`auto`（hybrid 按页自动判 txt vs VLM OCR），保留表格/公式/图片。method 标签为 `mineru-api`（garbled 字体 PDF 强制 ocr → `mineru-api-ocr`；<2000 字符加 `-low-quality`）。fitz 采样仅做 garbled 检测，不再做三分类。**原因**：PyMuPDF 直接 `get_text()` 在数据手册类 PDF 上漏检表格/公式/图（实测对比 73 表格/7 公式/157 图 vs 0/0/2），必须靠 minerU 的版面分析补救。
- **跳过代价**：无该路径；这是三类 PDF 里最快的一档（但已不是"毫秒级"，是分钟级，因为要起 minerU 模型）
- **产物**：每页一个 `p<NNN>.txt`（与扫描版同一套产物结构，由 Stage 1.1 内部统一组装）
- **go/no-go**：平均 chars/page >500 且抽样页中全页大图占比 <60%。**但如果书籍内容以图表为核心（信号完整性、眼图、波形图、电路图等），即使字符数达标，也优先选路径 B——图表丢失的代价远大于 OCR 的时间成本。**
- **已知坑（502 workaround，见 `a79cd7d`）**：`mineru -b pipeline` CLI 在 3.4.0 有 502 Bad Gateway bug（自启的 API 服务器立刻关闭）。当前默认走"API 路径"（`_stage_1_1_extract_text_scanned()`，函数名是历史遗留，现在文本版/混合版/扫描版都走它），不再调用 pipeline CLI。设 `IMPROVED_WIKI_PIPELINE_CLI=1` 才会尝试（已知坏的）pipeline CLI 路径，method 标签为 `mineru-pipeline`。

#### 路径 B：扫描版 PDF（chars/page <50，或抽样页中 >60% 有全页大图，或图表密集型书籍）

- **作用**：同一个本地 minerU API 服务器，强制走 VLM OCR 模式，同时提取文字和图片。**⚠️ OCR 处理的扫描版 PDF 会有高质量文字层（chars/page 可达数百甚至上千），2026-06-17 新增第四信号"隐藏 OCR 层检测"：即使 chars/page >500，如果 >30% 抽样页有全页大图，判定为 `mixed` 走 OCR 路径，防止 Johnson 事故（文本达标但图表全丢）再次发生。**
- **跳过代价**：扫描版 PDF 若只抽文本层 → 全页波形图/眼图/示意图丢失 → 对于信号完整性、电路设计等图表密集型书籍，丢失了一半以上的知识价值。**2026-06-14 Johnson《High-Speed Signal Propagation》实际发生**：100% 页面有全页大图，但 OCR 文字层字符数达标，最终误判为纯文本路径，全本图表丢失（这正是当时引入第四信号的原因）。
- **产物**：每页一个 `p<NNN>.txt`（与页号 1:1 对应）+ minerU 自动提取并已经过 Stage 1.3 caption 的图片（见下方 Stage 1.2 说明）
- **go/no-go**：每页 chars >100；无幻觉（chars<100 且无中文字符 → 重跑）；确认图片已落到 `wiki/media/<slug>/`
- **关键实操**：文本版/混合版/扫描版 PDF 现在统一走同一套本地 minerU 持久 API 服务器（`mineru.cli.fast_api`，端口 `MINERU_API_PORT`，默认 19999），按 50 页/chunk（`MINERU_CHUNK_SIZE`）切分，逐 chunk POST `/file_parse`，每 chunk 最多 3 次重试、累计失败 >30% 全本 abort。免费、自动提取图片、无需 API key。**并发限制**：系统级最多 1 个 minerU 任务执行，通过文件锁 `_stage_1_1_acquire_mineru_lock()`（`fcntl.flock`，超时 3600s）而不是旧版的进程数轮询实现，等待时打印 `[mineru] Waiting for lock... (Xs elapsed)`，无需人工协调。详见 `references/scanned-pdf-ocr-pipeline.md`。

### Stage 1.2 · 图片提取 ⭐ **永远不能跳**

**PDF（默认 API 路径，2026-06-23 起）**：图片提取已经融进 Stage 1.1 的 chunk 处理里，不再是事后单独一步。每个 chunk 调 minerU `/file_parse` 拿到结果后，`_stage_1_2_harvest_images()` 立即从响应里的 `images`（base64）+ `content_list`（页码映射）把图存到 `wiki/media/<type>/<pdf-stem>/`，文件名 `p<NNN>-mineru_<md5前8位>.<ext>`（不再是 `p<N>-fig<K>.<ext>` 这种页内序号命名）。全本 OCR 跑完后 `_stage_1_1_scanned_assemble_manifest()` 汇总写 manifest.json，并直接调 Stage 1.3 的 `_stage_1_3_caption_images_batch()` 把图配上文字——也就是说对默认路径，Stage 1.2 + 1.3 已经在 Stage 1.1 内部做完了，ingest.py 里那个独立的"Stage 1.2 调用"（`_stage_1_2_extract_from_mineru()`）对这条路径基本是空跑（找不到目录，返回 0 张）。

**PDF（opt-in pipeline CLI 路径，`IMPROVED_WIKI_PIPELINE_CLI=1` 时）**：走真正独立的 Stage 1.2 —— `_stage_1_2_extract_from_mineru()` 从 `extract_tmp_dir/<stem>/<method>/images/`（minerU CLI 落盘的目录）拷贝图到 `wiki/media/`，同时读 `content_list.json` 里 minerU 自带的 `image_caption` 写成 sidecar，供 Stage 1.3 跳过重复配文字。

**PPTX / DOCX**：走 `stage_1_2_extract_images()` → `_stage_1_2_extract_images_office()`，直接从 zip 内部 `ppt/media/` / `word/media/` 取图（NashSU parity 做法），与 minerU 无关。

- **跳过代价**：图全部丢失，wiki 文字描述无法引用图，故障排查价值砍半
- **产物**：`wiki/media/<type>/<pdf-stem>/p<NNN>-mineru_<id>.<ext>`（PDF API 路径）+ manifest.json
- **go/no-go**：抽出的图总数 > 0；如确实没有图，在 source 页 `## Embedded Images` 段写"无嵌入图"
- **必须含**：
  - 文件命名带页号便于回溯
  - 尺寸过滤：`_is_image_too_small()`，阈值 `MINERU_IMG_MIN_WIDTH`/`MINERU_IMG_MIN_HEIGHT`（默认 20px，故意调得很低，公式截图哪怕只有 29px 高也要保留——MiniMax 能转录）
  - manifest.json 记录：图路径 / 来源页 / 尺寸
  - **注意**：当前 API 路径的图片 harvest 按 `page+md5前8位` 命名，不做跨页 sha256 全局去重（旧的 PyMuPDF 提取逻辑曾经做过 sha256 去重，2026-06-23 随 PyMuPDF 路径整体移除后这一步也跟着没了——同一张图如果在文档里物理重复出现在不同页，会各存一份）
- **历史**：`fitz.Pixmap(doc, xref)` 纠正旋转/翻转、CMYK→RGB 那套 PyMuPDF 提取逻辑（2026-06-15 引入）已随 2026-06-23 的 mineru-only 迁移整体删除（dead code cleanup），minerU 自己的版面分析已经处理了图像方向问题。

### Stage 1.3 · 图片 captioning ⭐ **永远不能跳**
- **作用**：对每张抽出的图，用 VLM 生成 1-3 句描述（中文优先）
- **跳过代价**：图存在但无文字说明 → LLM 和用户都不知道图里是什么 → 故障排查时无法检索
- **产物**：
  - `wiki/media/<type>/<source-slug>/p123-fig4.png.caption.txt`（每图一个 .caption.txt）
  - 或 `wiki/media/<type>/<source-slug>/captions.json`（合并清单）
- **go/no-go**：每张图都有 caption 文件；caption 长度 ≥ 20 字符（防止空 caption）
- **VLM 选择**（按本地优先 + 批量优先）：
  1. 本地 VLM（零 API 成本；MinerU 2.5 Pro 1.2B 等）—— **实测有限制**（见 `multimodal-vlm-pitfalls.md`）
  2. **多图/请求批量 API**（1 次调用 N 张图，省 60-75% 时间）⭐ **默认推荐 — `anthropic/v1/messages` 多图 content blocks**（minimax M3 国内端，HardwareWiki 实测 5 张/批 17.7 秒）
  3. 单图/请求 API（不推荐，仅在 VLM 限制必须时）
  4. Anthropic Message Batches API（50% 折扣，**24h 异步**——不适用于会话内消化）
  5. 极简 fallback：每图固定 caption "图 N：源自 <book> p<page>，内容待人工补"
- **重要（2026-06-11 强制）**：批量策略不能凭直觉选——必须先跑 `caption_sample_test.py`（20 张样本双 VLM 对比），**经验性**选型，不靠启发式
- **HardwareWiki 实测选择**（2026-06-11 无源器件篇扫描版）：`anthropic/v1/messages` 多图批量 caption（minimax M3，5 张/请求约 3.5 秒/张，比 OCR 任务更快因为输出短）。Stage 1.3 走跟 Stage 1.2 相同的 endpoint 即可。

### Stage 2.1 · Analysis（Global Digest）
- **作用**：1 次 LLM 调用，喂整本 PDF + schema + index，输出 6 块结构化 YAML
- **产物**：保存在 progress checkpoint（`.llm-wiki/.ingest-progress/<hash>.json`），成功 ingest 后写入 cache 的 `stages.global_digest_keys`
- **6 个顶层 key**（与 ingest.py `build_global_digest_prompt()` 一致）：
  - `book_meta`：标题、作者、年份、类型、语言
  - `outline`：章节大纲（含 `key_topics` 列表 + `start_marker`）
  - `key_entities`：关键实体（术语、人物、器件型号、公式符号等）
  - `key_concepts`：关键概念（设计思想、方法论、理论框架等）
  - `key_claims`：关键论断（结论、数据、设计准则等）
  - `chunk_plan`：切块计划（`estimated_total_chunks` + 每块的章节范围 + 重叠策略）
- **go/no-go**：`stages.global_digest_keys ≥ 1`（cache 中有记录）

### Stage 2.2 · Chunk Analysis
- **作用**：对源文本切块分析（**永远不能跳过，即使短源也要跑**）。短源（≤ 60K 字符）按 1 块处理（1 次 LLM 调用）；长源（> 60K 字符）按 ~60K/块切分（N 次 LLM 调用）
- **产物**：保存在 progress checkpoint，成功 ingest 后写入 cache 的 `stages.chunks_analyzed`
- **每个 chunk 的 YAML key**（与 ingest.py `build_chunk_analysis_prompt()` 一致）：
  - `chunk_index`、`chunk_total`：当前块序号和总块数
  - `entities_found`：本块发现的新实体（含名称、类型、定义、首次出现位置）
  - `concepts_found`：本块发现的新概念（含名称、定义、关键关系）
  - `claims`：本块的关键论断（含论断内容、证据类型、置信度）
  - `formulas`：本块出现的公式（含 LaTeX 表达式、变量说明、物理意义）
  - `connections_to_existing_wiki`：与已有 wiki 页面的关联
  - `digest_updates`：对 global digest 的修正/扩展/矛盾
- **go/no-go**：`stages.chunks_analyzed ≥ 1`（cache 中记录 ≥1 块）

### Stage 2.3 · Incremental Association Detection ⭐ **新增 2026-06-20**

- **作用**：在 chunk 分析完成后、生成页面之前，检测本源的 entities/concepts 与 wiki 已有页面的关联。使新源的概念生成能够自动避免重复、自动识别需要生成 comparison 的对象。
- **跳过条件**：wiki 为空（首次 ingest）时自动跳过
- **产物**：progress checkpoint 中的 `incremental_associations`（字典：concept_name → [existing_wiki_page_names]）
- **go/no-go**：
  - wiki 非空：`stages.incremental_associations` 已记录
  - wiki 为空：自动跳过，标记为完成

### 2.4 · Source/Concept/Entity Generation（统一 barrier-free pipeline）

- **作用**：与 Stage 2.2 合并为 **barrier-free pipeline**：analyze chunk → generate pages → next chunk。对所有 chunk 数统一——1 chunk = 单次循环，N chunks = N 次循环。每个 chunk 分析完立即生成概念/实体页，不等全部分析完成。**仅生成 source / concept / entity 三种 page type**。
- **为什么统一**：NashSU 对单 chunk 书用 legacy synthesis（多轮追问），但单次 synthesis LLM 调用经常因 token 超限或超时失败。barrier-free 每 chunk 一次小调用，稳定且可恢复。
- **新增 2026-06-20**：在生成时利用 Stage 2.3 的 `incremental_associations`，对匹配的概念自动标记 `existing_wiki_reference` 字段。
- **产物**：FILE blocks → `parse_file_blocks()` → 写入 `wiki/` 目录
- **输出格式**：`---FILE:wiki/<path>---\n<markdown content>\n---END FILE---`
- **go/no-go**：
  - `stages.file_blocks_generated ≥ 1`
  - source page FILE block 存在
  - 概念页路径在 `wiki/concepts/` 下（不在 bare `wiki/` 或 `wiki/sources/`）
  - 至少 1 个 chunk 产出 ≥ 1 个 block
- **fallback**：barrier-free 产出 0 个 concept → 自动降级为 per-concept 生成（每个 concept 一次 LLM 调用）

### Stage 2.5 · Concept Dedup & Merge ⭐ **新增 2026-06-20（高优先）**

- **作用**：在 2.4 生成所有 chunk 的 concept/entity 后，对同一本书内部的概念进行智能去重与合并。防止"同一概念以不同名称和定义在不同 chunk 出现"导致的重复页面。**质量关键环节。**
- **跳过条件**：单 chunk 书（≤60K 字符）自动跳过
- **产物**：progress checkpoint 中的 `concept_merge_rules`（列表：[{primary, duplicates, merge_strategy}]）
- **go/no-go**：
  - 单 chunk：跳过
  - 多 chunk：`concept_merge_rules` 已记录（可为 `[]`）
- **算法**：
  1. 收集 2.4 的所有 concept page FILE blocks
  2. LLM 1 次调用：按名称+定义相似度聚类（>80% 相似合并）
  3. 对每聚类生成合并定义（取并集 + 补充细节）
  4. 更新 FILE blocks：用合并 slug 替换所有引用，删除重复
- **输出给 2.6**：去重后的 FILE blocks

### 2.6 · Source Page Generation

- **作用**：基于 Stage 2.5 的去重结果，生成或更新源页面。源页是本书的索引，列出所有概念、实体、问题、对比。
- **产物**：唯一的 source page FILE block
- **go/no-go**：source page 路径为 `wiki/sources/<stem>.md`

### 2.7 · Query Auto-Generation ⭐ **新增 2026-06-16**

- **作用**：基于 2.4 已生成的 concept/entity 列表，识别书中**提出但未完全解答**的开放问题，生成 `wiki/queries/<slug>.md` 页面。query 是知识演化链中"从已知到未知"的第一跳——把书中隐含的认知边界显式化为可追问的问题。这是纯知识产出（内存中的 FILE blocks），由 Stage 3.2 统一写盘。
- **跳过条件**：source 类型为 `datasheet` 或 `standard` 时自动跳过（纯事实罗列，不产生有意义的开放问题）。
- **产物**：0-5 个 `wiki/queries/<slug>.md` 页面，或 `---QUERIES: 0---` 标记。
- **go/no-go**：
  - 生成了 0-5 个 query FILE block 或 `---QUERIES: 0---` 标记
  - 每个 query frontmatter 含 `type: query` + `title:` + `sources:` 三必填字段
  - 每个 query body ≥200 字符（不含 frontmatter）
- **设计说明 / prompt 结构**：见 `references/query-generation.md`（真相源为 `_stage_2_7_build_prompt`）

### Stage 2.8 · Cross-source Query Resolution ⭐ **新增 2026-06-20（高优先）**

- **作用**：对 2.7 生成的 query，自动检索 wiki 已有页面是否已经回答。发现答案则自动关闭 query；否则保留。从而减少"已问过的问题"积累。
- **跳过条件**：2.7 无 query 生成，或 wiki 为空时自动跳过
- **产物**：progress checkpoint 中的 `query_resolutions`（列表：[{query_slug, status, resolution_pages}]）
  - status: "closed" / "kept"（二选一）
  - resolution_pages: 找到的相关已有页面列表
- **go/no-go**：
  - `query_resolutions` 已记录（可为 `[]`）
  - 关闭的 query 已从 FILE blocks 中移除
- **算法**：
  1. 对每个 2.7 生成的 query FILE block
  2. 提取 query title + body 的关键词
  3. 在 wiki concepts/entities 中按标题词 Jaccard 匹配（阈值 0.6）找相关页
  4. LLM judge：`closed`（已答→删除） / `kept`（未答/答不全→保留），不确定一律 `kept`
  5. 更新 FILE blocks：`closed` 的 query 删除

### 2.9 · Comparison Auto-Generation ⭐ **新增 2026-06-16**（2.9A/B）

- **作用**：生成对比分析页面，分两种场景：
  - **2.9A 域内消歧义**：新 concept 名称与 wiki 已有 concept 同名但不同 domain → 创建/更新消歧义页（`type: comparison`, `domain: general`）。对齐 NashSU `domains.md` 消歧义规则。
  - **2.9B 源内概念对比**：同一源内两个高度相关的概念天然适合对比（如 CCM vs DCM、EMI vs EMC）→ 生成对比页（对比维度 ≥4，至多 2 页，`concept ≥ 2` 才运行）。
- **跳过条件**：本次 concept **和** entity 都为空（纯 stub source）时整体跳过。
- **产物**：`wiki/comparisons/<slug>.md` 页面（2.9A 消歧义 + 2.9B 源内对比），或子标记 `---COMPARISONS_DISAMBIGUATION: 0---` / `---COMPARISONS_IN_SOURCE: 0---`。
- **go/no-go**：
  - 生成了 comparison FILE block 或对应 `---COMPARISONS_*: 0---` 标记
  - 每个 comparison frontmatter 含 `type: comparison` + `title:` + `domain:` 三必填字段
- **设计说明 / prompt 结构**：见 `references/comparison-generation.md`（真相源为 `_stage_2_9_build_prompt_disambiguation` / `_stage_2_9_build_prompt_in_source`）

### Stage 3.4 · Review ⭐ **永远不能跳**（3.4 review，但低于阈值时自动 skip）

- **作用**：Phase 3 的 LLM 质量审查，分两步：
  1. **生成 review items**：当满足 NashSU 3 条件（≥4 FILE 块 / ≥10K 字符 / 未闭合 REVIEW）时，跑一次 LLM 调用输出 5 类 review items：confirm / suggestion / missing-page / contradiction / duplicate。
  2. **解析并写入**：把 LLM 输出的 review items 解析并写入 `wiki/REVIEW/<type>/<date>-<source>-<short-slug>.md`（按 review type 分子目录，文件名含动作简述，含 frontmatter `resolved: false`），同时写入 `review-suggestions.json` 到 runtime dir。
- **自动跳过条件**：NashSU 3 条件全不满足时跳过。但即使跳过，仍记录"LLM 主动认为无问题"。
- **产物**：`wiki/REVIEW/<type>/` 子目录下的 .md 文件 + `review-suggestions.json`（runtime dir）
- **go/no-go**：review items 数量 ≥ 0（即使 0 也要记）；`wiki/REVIEW/` 目录结构存在且合法

> **高层知识空缺检测（synthesis / finding / thesis / methodology）已移至 lint 系统。** 这些检测扫描的是 wiki 整体健康状态而非单次 ingest 的产物质量，语义上属于 lint 范畴。触发条件和输出格式见 `references/knowledge-gap-lint.md`。

### Stage 3.1 · Write files（含 source page gate）

- **作用**：Phase 3 唯一的磁盘写入入口。分两步：
  1. **Source page gate**（内存）：扫描 Phase 2 产出的所有 FILE blocks，检查是否包含 source 页。如果没有，从 Global Digest 自动生成一个 stub 追加到 blocks 列表。
  2. **原子写盘**：解析所有 FILE 块 → 先 .tmp 再 rename。
- **产物**：所有 wiki/ 下的页面（sources/concepts/entities/queries/comparisons），确保 `wiki/sources/` 与 `raw/` 1:1 对应。
- **go/no-go**：解析出的 page_blocks 数 == 写盘成功数；source page 已落盘

### Stage 3.2 · 图片安全网注入 ⭐ **永远不能跳**（依赖 1.2/1.3）
- **作用**：在 source 页末尾追加 `## Embedded Images` 段，列出所有抽出的图 + caption
- **跳过代价**：图存在但没在 wiki 里被引用 → 用户的 wiki 等于没图
- **产物**：source 页有 `## Embedded Images` 段
- **go/no-go**：source 页包含 `## Embedded Images` 标题 + ≥ 1 行图引用

### Stage 3.3 · Cross-domain Slug Collision Review ⭐ **新增 2026-06-22**
- **作用**：写盘后立即扫描新写的 concept 页 slug，检测与**其它 domain** 已有 concept 的同名碰撞，标记需要消歧义。同 domain 内的重叠是合法合并（由 Stage 2.5 处理），不在此阶段重复。
- **跳过代价**：跨域同名概念静默共存 → 消歧义页缺失，wikilink 指向错误目标
- **产物**：碰撞清单 + warning 段（`stage_3_3_result`：`items`/`collisions`/`warning`），供 Stage 3.4 review 与 Stage 3.6 质量评分消费
- **go/no-go**：跨域碰撞数已统计（可为 0）；有碰撞时消歧义建议已生成

### Stage 3.5 · Save cache + Aggregate Repair ⭐ **永远不能跳**

- **作用**：两步：
  1. **Aggregate repair**：程序化 append index.md / log.md + LLM 重写 overview.md
  2. **Save cache**：写 `<sha256(raw)>` → `[filesWritten...]` 映射到 `ingest-cache.json`
- **跳过代价**：下次跑同一文件会重做所有 stage；aggregate 页面不更新导致 wiki 导航缺失
- **产物**：`ingest-cache.json`（含本次所有 raw 文件 hash）+ index.md / log.md / overview.md 更新
- **go/no-go**：每个本次处理的 raw 文件都有 hash 记录；旧有条目全部保留 + 新条目已追加
- **2026-06-11 重要发现**：app 的 `cache entry ≠ 产物`。cache 里 `filesWritten=[]` 也会出现"已 ingest"假象。**必须用 `scripts/validate_ingest.py` 验产物侧**，不能只看 cache schema

**🚨 2026-06-13 ADL8113 事故**：NashSU 原生让 LLM 同时输出 index/log/overview，但 LLM 不会读到旧的 wiki 文件内容，静默丢失所有历史。improved-wiki 对策：index.md / log.md 纯程序化 append（LLM 不参与）；overview.md LLM 重写但喂入当前全文作上下文。

### Stage 3.6 · Quality Scoring Card ⭐ **新增 2026-06-20（中优先）**

- **作用**：对本次 ingest 的质量进行量化评分，生成质量评分卡。快速识别哪些 ingest 有问题需要人工复审，避免低质量内容进入 wiki。
- **跳过条件**：无（总是执行）
- **产物**：progress checkpoint 中的 `quality_metrics` 字典 + 可选的 `.llm-wiki/lint/audit/<date>-<source>-quality.md` 评分卡
  - 评分卡格式：
    ```markdown
    ---
    type: audit
    source: <source-stem>
    date: <ingest-date>
    overall_score: <0.0-1.0>
    ---
    
    ## 质量评分
    | 维度 | 评分 | 权重 | 说明 |
    |------|------|------|------|
    | 文本覆盖 | ... | 25% | 提取字符数 / 预期字符数 |
    | 图片质量 | ... | 20% | 提取图数 / 预期图数 + caption 长度 |
    | 概念密度 | ... | 25% | 生成页数 / 文本长度 |
    | Review 质量 | ... | 20% | 1 - (review_count / file_blocks) |
    | 去重完整性 | ... | 10% | (原始概念数 - 重复数) / 原始概念数 |
    
    ## 详细诊断
    ...
    ```
- **go/no-go**：
  - `quality_metrics` 已记录
  - 如果 `overall_score < 0.65`，自动标记为 "needs_review"，写入 `wiki/REVIEW/audit/`
- **算法**：
  ```
  text_coverage = len(extracted_text) / len(original_pdf_text)
  image_quality = (len(extracted_images) / expected_images) * (avg_caption_length / 50)
  concept_density = len(unique_concepts) / (len(extracted_text) / 1000)
  review_quality = 1 - min(1.0, review_count / max(1, file_blocks_count))
  dedup_completeness = (concept_count_before - concept_duplicates) / concept_count_before
  
  overall_score = (
    0.25 * text_coverage +
    0.20 * image_quality +
    0.25 * concept_density +
    0.20 * review_quality +
    0.10 * dedup_completeness
  )
  ```

### 3.7 · Embeddings ⭐ **强制尝试（2026-06-21 起）**
- **作用**：把 wiki/ 下的页面 chunk 化 + embed，写到 LanceDB。默认本地 Ollama bge-m3（`http://127.0.0.1:11434/v1`），**不再需要显式 export `EMBEDDING_BASE_URL`**——只要本地 Ollama 跑着且模型已拉取就会自动执行。
- **本地能力缺失时**：不再静默跳过。打印安装提醒（`ollama serve` / `ollama pull bge-m3` / `pip install lancedb`）+ 补跑命令，但**不阻断 ingest**（页面已在 Stage 3.1 落盘）。`validate_ingest.py` 会把这种情况记为 ❌ 而不是 `note skipped`，让缺失可见。
- **跳过代价**：检索只能用纯关键词（wiki < 100 页可接受，> 100 页必须 embeddings）
- **产物**：`lancedb/` 表 + `embed-cache.json`
- **go/no-go**：LanceDB 表存在 + 已写 ≥ N 个 chunk；本地能力不可用时，go/no-go 改为"安装提醒已打印 + 补跑命令已给出"

---

## 强制顺序（不能乱）

```
0.1 → 0.2 → 1.1 → 1.2 → 1.3 → 2.1 → 2.2 → 2.3 → 2.4 → 2.5 → 2.6 → 2.7 → 2.8 → 2.9 → 3.1 → 3.2 → 3.3 → 3.4 → 3.5 → 3.6 → [3.7] → 4.1
```

新增 Stage（2026-06-20）：
- **2.3**：增量学习关联检测（wiki 非空时执行）
- **2.5**：概念去重与合并（多 chunk 书执行）
- **2.8**：跨源查询解析（2.7 产出 query 时执行）
- **3.6**：质量评分卡（总是执行）

执行依赖关系：
- 1.2 **必须先于** 1.3（先有图才能 caption）
- 1.2/1.3 **必须先于** 3.2（3.2 注入图引用）
- Stage 2.1 / 2.2 **永远不能跳过**（短源 1 chunk / 长源 N chunk）
- **2.3 依赖 2.2 完成**（需要 chunk analysis 结果）；wiki 为空时自动跳过
- **2.4 使用 2.3 的增量关联上下文**（优化生成质量）
- **2.5 依赖 2.4 完成**（需要所有 concept FILE blocks）；单 chunk 书自动跳过
- **Phase 2（Generation）全部在内存中完成**：2.4 → 2.5 → 2.6 → 2.7 → 2.8 → 2.9，串行执行。所有产出统一由 Stage 3.1 写盘。
- **2.8 依赖 2.7 完成 + wiki 已有内容**（搜索和匹配）
- **Stage 3.3（slug 碰撞审查）在 3.2 注入图引用后立即运行**，标记跨域同名概念需消歧义
- **Stage 3.4（review）运行在已写盘的文件上**，human reviewer 可直接看页面内容
- **Stage 3.5（aggregate repair）在所有页面写盘后运行**
- **Stage 3.6（质量评分）在 3.5 完成后运行**，基于完整的 ingest 结果
- 2.7 是 conditional（datasheet/standard 自动跳过）
- 2.8 是 conditional（2.7 无 query 或 wiki 为空时自动跳过）
- 2.9 是 conditional（无 concept 产出时自动跳过）
- 3.4 (review) 是 conditional（NashSU 3 条件触发：≥4 FILE 块 / ≥10K 字符 / 未闭合 REVIEW）
- 3.6 是 conditional（overall_score < 0.65 时标记为 needs_review）
- 3.5 程序化 append index/log + LLM 重写 overview（喂入现有内容防丢失）
- 3.5 在所有 stage 之后（写最终缓存）；hard error（磁盘满/权限）阻止 cache save
- 3.7 强制尝试，默认本地 Ollama bge-m3；本地能力不可用时打印安装提醒（不阻断 ingest），手动补跑 `build_embeddings.py`

---

## 验证清单（每次 Ingest 完成后必查）

完成一个文件的 ingest 后，**必须**逐项过这个清单：

- [ ] **Stage 1.1**：源文本已提取（minerU `txt` method OR VLM OCR 后每页 chars >100；PyMuPDF 现在只做类型检测，不做提取）
- [ ] **Stage 1.2：图已抽到 `wiki/media/<type>/<slug>/`（数量 > 0 或确认无嵌入图）**
- [ ] **Stage 1.3：每张图有 .caption.txt（长度 ≥ 20 字符）**
- [ ] Stage 2.1：global-digest.yaml 合法
- [ ] Stage 2.2：所有 chunk analysis 合法
- [ ] **Stage 2.3：incremental_associations 已记录**（wiki 非空时）
- [ ] 2.4：generation_response.txt 的 stop_reason == end_turn（**不是 max_tokens**）
- [ ] **Stage 2.5：concept_merge_rules 已记录**（多 chunk 书）；重复概念已合并
- [ ] **2.6**：源页面已生成并包含 concept 列表
- [ ] **2.7：query 页面已生成或 `---QUERIES: 0---` 已记录**（datasheet/standard 自动跳过）
- [ ] **Stage 2.8：query_resolutions 已记录**；已关闭或改写的 query 已处理
- [ ] **2.9：comparison 页面已生成或 `---COMPARISONS_DISAMBIGUATION: 0---` / `---COMPARISONS_IN_SOURCE: 0---` 已记录**（无 concept/entity 时自动跳过）
- [ ] Stage 3.1：所有 FILE 块写盘成功
- [ ] **Stage 3.2：source 页含 `## Embedded Images` 段**
- [ ] **Stage 3.3：跨域 slug 碰撞已检查**（碰撞数已统计，消歧义建议已生成）
- [ ] **Stage 3.4：review items 已生成并写入 wiki/REVIEW/（即使 0 items）**
- [ ] **Stage 3.5：ingest-cache.json 含本次所有 raw 文件 hash**（且 `validate_ingest.py` 通过；ingest.py 末尾自动运行）
- [ ] **Stage 3.6：quality_metrics 已记录；overall_score 已计算**；如 <0.65 已标记为 needs_review
- [ ] **3.7：lancedb 表已更新**；本地能力不可用时确认安装提醒已打印并记录待补跑

**关键新增 stage**（2026-06-20 优化）：
- **Stage 2.3**（增量学习）—— 避免新源生成孤儿概念
- **Stage 2.5**（概念去重）—— 防止同一本书的重复概念页面
- **Stage 2.8**（跨源查询解析）—— 自动关闭已有答案的 query
- **Stage 3.6**（质量评分）—— 快速识别质量问题的 ingest

**历史上最容易跳过的 stage**：
- Stage 1.2（图提取）— 丢失图知识
- Stage 1.3（图 caption）— 图无法检索
- Stage 2.5（概念去重）— **2026-06-20 新增**；重复页面污染 wiki
- 2.7（query 生成）—— 知识库只有事实没有追问
- Stage 2.8（跨源查询解析）— **2026-06-20 新增**；重复提问浪费资源
- 2.9（comparison 生成）— 跨概念理解和消歧义缺失
- Stage 3.4（review 建议）—— 错误内容永久残留
- Stage 3.2（图注入）—— 图与 wiki 脱节
- Stage 3.5（cache 写入）—— 下次跑会重做所有 stage
- Stage 3.6（质量评分）—— 无法识别问题 ingest

---

## wiki 项目的特定策略（边界明确）

每个 wiki 项目可以在自己的 `wiki/methodology/` 下写"**per-project 决策**"页（如 "本项目用 MiniMax 批量 API"），引用本清单 + 记录该项目的特定选择。

**重要边界（2026-06-11 明确）**：
- `wiki/methodology/` **只放项目特定决策**（VLM 选择、批量大小、嵌入维度等）—— **不放**本清单的复制，也不放"我跳过了哪些 stage + 原因"
- 通用消化策略是本 skill 的责任（在本文件）；项目特定的偏离 = 在 `(removed — validate_ingest.py covers this)` 里说明本项目怎么做，**不**等于把本清单再抄一遍
- 如果项目**真的**跳过了某个 ⭐ stage，**在 `wiki/methodology/` 里加一段说明**，并标注"已知违反 SKILL.md 强制清单，原因：……"——这是**显式记录偏离**而不是静默跳过
- 静默跳过 = 违反规范。显式记录偏离 = 合规（因为人类在下次 lint 时能看到）

---

## 验证清单的执行方式（**清单本身没用，配套脚本才有约束力**）

本清单的 19 项是**人工 check 用的**，但验证已在流水线中自动化：

### 自动验证（ingest.py 内置，2026-06-16+）

**每个 Stage 完成后有实时验证门禁**（`_verify_stage_N()`），失败直接 `RuntimeError` 中止：

| Stage | 门禁检查 | 失败行为 |
|-------|---------|---------|
| Stage 1.1 | 提取文本 ≥ 500 字符；MinerU ≥ 2000 字符 | RuntimeError |
| Stage 2.1 | Global Digest 含 6 个必需 key；≥ 1 个 concept | RuntimeError |
| Stage 2.2 | chunk 分析非空 | RuntimeError |
| 2.4 | ≥ 1 个 FILE block；source page 存在；路径正确 | RuntimeError |
| Stage 3.1 | source page 落盘 | warning（不中止；无 `_verify_stage_3` hard gate） |

**Ingest 末尾自动运行 `validate_ingest.py`**（全阶段验证），结果打印到 stdout。

遵循 superpowers Iron Law：**NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE**。

### 手动补充验证

```bash
# 结构性 lint（覆盖 wikilink 健康）
./scripts/wiki-lint.sh --summary

# 图存在性（覆盖 Stage 1.2 / 1.3 / 3.2）
test -d wiki/media/*/<slug> && \
  find wiki/media/<type>/<slug> \( -name '*.jpeg' -o -name '*.png' \) | \
    while read f; do
      [ -f "$f.caption.txt" ] || echo "MISSING CAPTION: $f"
    done

# cache hash 完整性（覆盖 Stage 5）
python3 -c "
import json, hashlib
from pathlib import Path
cache = json.load(open('.llm-wiki/ingest-cache.json'))
for k, v in cache['entries'].items():
    p = Path('raw') / k
    if p.exists() and hashlib.sha256(p.read_bytes()).hexdigest()[:16] != v['hash'][:16]:
        print(f'HASH DRIFT: {k}')
"
```

## 修订记录

- **2026-06-22**：Stage 2.10（review）重编号为 **3.4** 并移入 Phase 3，原 3.4/3.5/3.6 顺延为 **3.5/3.6/3.7**（aggregate repair / quality / embeddings），4.1 不变。动机：review 实际运行在已写盘文件上（3.3 之后），归入 Phase 3 更贴合执行顺序，消除"2.10 编号在 Phase 2 却在 Stage 3 之后执行"的错位。代码同步重命名：`stage_2_10_review_suggestions`→`stage_3_4_review_suggestions`（`_stage_2_10_review.py`→`_stage_3_4_review.py`）、`stage_3_4_aggregate_repair`→`stage_3_5_aggregate_repair`、`stage_3_5_quality`→`stage_3_6_quality`（`_stage_3_5_quality.py`→`_stage_3_6_quality.py`）、`stage_3_6_embed_new_pages`→`stage_3_7_embed_new_pages`。新增 Stage 3.3（跨域 slug 碰撞审查）章节与表格行；修正强制顺序行（原重复 3.1→3.2、缺 3.3 的 bug）。历史条目中的旧函数名映射保持原样。

- **2026-06-21**：Stage 编号统一为 x.y 形式（消除 x.y.z 后缀）。映射：2.2.1→2.3、2.3→2.4、2.3.1→2.5、2.4→2.6、2.5→2.7、2.5.1→2.8、2.6→2.9、3.4.1→3.5、3.5→3.6。代码同步重命名：`stage_2_4_source_page`→`stage_2_6_source_page`、`stage_2_5_query_generation`→`stage_2_7_query_generation`、`stage_2_6_comparison_generation`→`stage_2_9_comparison_generation`、`stage_3_5_embeddings`→`stage_3_6_embeddings`、`verify_stage_3_5`→`verify_stage_3_6`。接入 Stage 2.5（源内概念去重，多 chunk 书）与 Stage 3.5（质量评分卡，总是执行）；Stage 2.3（增量关联）与 2.8（跨源 query 解析）模块保留，待后续接入。

- **2026-06-21（二）**：接入 Stage 2.3（增量关联，词级 Jaccard + slug 精确匹配）与 Stage 2.8（跨源 query 解析，LLM judge，默认 kept）。Stage 2.5 升级为确定性初筛 + LLM 确认（Jaccard ≥0.6 + 停用词过滤，失败保守不合并）。Stage 3.5 修正：image_quality 改用 caption 覆盖率，单 chunk 书 dedup 维度标 N/A 排除。2.3 完整接入生成 prompt 反馈仍待 barrier-free pipeline 重构。
- **2026-06-20**：**全量重编号为 Phase.Stage 形式，编号=执行顺序**。5 个 Phase：0 前置检查 / 1 原始素材提取 / 2 消化主流程 / 3 材料写入 / 4 验证检查。消除旧编号错位（旧 2.0 在 2 之后、旧 2.5rev/2.6 在 Stage 3 之后）。代码函数/模块/打印 label/validate label/进度 checkpoint 全部同步重命名（`stage_1_global_digest`→`stage_2_1_global_digest` 等；`_stage_0_extract`→`_stage_1_extract`、`_stage_1_analyze`→`_stage_2_analyze`）。Lint 同步采用 Phase 0-4 约定。Graph 命令保留独立编号（Stage 16-18）。
- **2026-06-11**：初版（源于 HardwareWiki 第一次 ingest 漏掉 Stage 1.2/0.6 事故）
- **2026-06-13**：Stage 3.4 从 LLM 重写改为程序化 append（ADL8113 事故教训）；Stage 2.1/1.5 YAML schema 对齐；Stage 2.3.5 触发阈值修正（≥4 FILE 块）
- **2026-06-14**：Stage 0.2 去重检查新增；Stage 1.1 三信号检测升级（Johnson 事故）；NashSU v0.4.23 parity audit 完成
- **2026-06-16**：新增 Stage 2.5 Query + 2.5 Comparison；阶段间实时验证门禁
- **2026-06-19**：全面重编号对齐 `ingest.py` 代码（废弃 Phase.序列），清理所有过时引用
- **2026-06-19**：`validate_ingest.py` 重编号对齐本文（旧 Stage 3.5/5/6 → 2.10/2.6/4，旧 3.7 并入 Stage 3.1）；新增 Stage 2.5 query / 2.5 cmp 两个验证段；`ingest.py` cache `stages` 新增 `queries_generated`/`comparisons_generated` 字段；修正自动验证表 Stage 2.1 "5 个 key"→"6 个"、Stage 3.1 "RuntimeError"→"warning（不中止）"
- **2026-06-17**：高层知识空缺检测移至 lint 系统（`knowledge-gap-lint.md`）；REVIEW 目录分子目录；Phase 4+5 合并；Stage 3.1.1 合并入 3.5；Stage 3.5 合并入 2.5 review
- **2026-06-17**：新增 **Stage 16-18 知识图谱后处理**（Lint 阶段，不在 ingest 管线内）。四信号加权图构建 + Louvain 社区检测 + 图谱洞察输出。脚本：`scripts/build_knowledge_graph.py`。触发时机：批量 ingest 后按需运行，不在单次 ingest 中自动执行。
- **2026-06-20**：知识图谱从 lint 剥离，改为独立 **Graph 命令**（与 Ingest / Lint 并列，对齐 NashSU graph-view 架构——KG 在 NashSU 本就由 `graph-view.tsx` 按需构建，不属于 lint）。脚本重命名 `build_knowledge_graph.py` → `graph.py`。Stage 16-18 框架改为「Graph 命令」段。

- **2026-06-21（三）**：拆分 `_run_chunk_pipeline` 为 `_analyze_all_chunks` → Stage 2.3 → `_generate_all_chunks`。Stage 2.3 的 `incremental_associations` 现回灌进每个 chunk 的生成 prompt（`build_per_chunk_gen_prompt` 新增 `existing_refs` 段，列出已有 wiki 页 wikilink，LLM 不再重复生成）。原 `_chunk_pipeline_serial`/`_chunk_pipeline_parallel` 合并为统一 analyze/generate 两阶段（分析仍分串行/并行，生成统一串行）。
- **2026-06-21（四）**：Stage 3.6（Embeddings）从"`EMBEDDING_BASE_URL` 显式设置才跑"改为**强制尝试**——默认值直接指向本地 Ollama bge-m3（`http://127.0.0.1:11434/v1`），不需要 export 任何环境变量。新增 `_stage_3_6_check_embed_capability()` 探测 lancedb 是否已装 + Ollama 是否可连 + 模型是否已拉取；本地能力不可用时打印安装提醒（ollama serve / ollama pull bge-m3 / pip install lancedb）+ 补跑命令，但不阻断 ingest。`validate_ingest.py` 同步把"未启用 embeddings"从 `note skipped` 升级为 `check` 失败项，使缺失在验证摘要里可见而非被静默忽略。根因：此前默认行为依赖用户记得手动 export 变量，即使本地 Ollama+bge-m3 已就位也会被当作"未配置"而静默跳过。

## Graph 命令：知识图谱（Stage 16-18）

> **定位**：**独立命令**（与 Ingest / Lint 并列，**不属于 lint**）。Ingest 管线不碰图——图建在 Graph 命令，图用在 Ingest（Stage 2.3 可通过 `--mode query` 查询已有图为新页面建议 wikilinks）。触发时机：完成一批 ingest（≥10 本新书）后手动运行，或 cron 定期执行，或 ingest 后由 `AUTO_BUILD_GRAPH=1` 自动触发。NashSU desktop 端这对应 graph-view 组件（`wiki-graph.ts` + `graph-relevance.ts` + `graph-insights.ts`），按需构建，与 lint 完全解耦。

### Stage 16 · 四信号知识图谱构建

- **作用**：解析 `wiki/` 下所有页面的 wikilinks + frontmatter（type/domain/sources），构建 networkx 加权无向图。四个信号权重（NashSU v0.4.24 parity）：
  - **Direct link（×3.0）**：`[[wikilinks]]` 直接连接
  - **Source overlap（×4.0）**：共享同一 raw source（`sources[]` frontmatter）
  - **Adamic-Adar（×1.5）**：共同邻居 / log(邻居度)——只对已有边做 refinemnt，不做 O(n²) 全量发现
  - **Type affinity（×1.0）**：相同 `type`（0.6）+ 相同 `domain`（0.4，general 不计）——只对已有边做 refinemnt
- **跳过代价**：无法发现知识聚类、知识缺口、桥接节点——wiki 永远只是松散页面集合
- **产物**：`graph.json`（nodes + edges + weights + communities）
- **go/no-go**：节点数 == wiki 页面数（排除 index/log/schema/overview + 状态文件）；边数 ≥ N 条（至少 wikilink 网络连接了部分页面）
- **性能考量**：7594 页 HardwareWiki 实测约 30 秒（AA + type affinity 仅 edge-refinement，不做全量 pair 遍历）

### Stage 17 · Louvain 社区检测 + Cohesion 评分

- **作用**：对 Stage 16 构建的加权图运行 Louvain 社区检测，自动发现知识簇。对每个社区计算 cohesion score（簇内实际边数 / 可能边数）。标记 cohesion < 0.15 的低质量社区。
- **跳过代价**：无法识别主题聚类、跨域桥接、孤立页面
- **产物**：`graph.json` 中的 communities 段（含每个社区的 size/cohesion/warning 标记）
- **go/no-go**：社区数 ≤ 节点数（极端：每节点一社区说明图太稀疏）；低 cohesion 社区数合理（< 10% 为健康）

### Stage 18 · 图谱洞察输出

- **作用**：基于 Stage 16-17 结果生成三类实用产物：
  1. **`knowledge-gaps.md`**：知识缺口报告——孤立节点、低 cohesion 社区、桥接节点（跨社区枢纽）、建议的缺失链接
  2. **`clusters/cluster-NNN.md`**：每个 ≥3 节点的社区生成一个 hub 页面（含核心页面、关键主题、建议动作）
  3. **Ingest 联动**：`--mode query --slug <page>` 为 ingest 中的新页面建议 wikilinks（只读查询，不重建图）
- **跳过代价**：图构建了但没人看——等于没建
- **产物**：`knowledge-gaps.md` + `clusters/` 下 N 个 .md 文件
- **go/no-go**：`knowledge-gaps.md` 存在且含 "## Isolated Nodes" / "## Bridge Nodes" / "## Suggested Missing Links" 三段；`clusters/` 不为空
- **门禁**：孤立节点数 < wiki 总页面的 10%（> 10% 说明 wikilink 覆盖率不足，应在 ingest 时补）；HardwareWiki 当前 1613/7594 ≈ 21%，大部分为 `REVIEW/` 和旧 ingest 页面

### Stage 16-18 使用方式

```bash
# 全量分析（Graph 命令，批处理）
IMPROVED_WIKI_ROOT=/path/to/wiki python3 scripts/graph.py

# 仅查看统计（不写文件）
IMPROVED_WIKI_ROOT=/path/to/wiki python3 scripts/graph.py --dry-run

# 大 wiki 先小规模测试
IMPROVED_WIKI_ROOT=/path/to/wiki python3 scripts/graph.py --dry-run --limit 500

# ingest 时查询：给新页面推荐 wikilinks
IMPROVED_WIKI_ROOT=/path/to/wiki python3 scripts/graph.py \
  --mode query --slug "my-new-page"

# 调整 cohesion 告警阈值（默认 0.15）
IMPROVED_WIKI_ROOT=/path/to/wiki python3 scripts/graph.py --min-cohesion 0.10
```

**依赖**：`pip install networkx python-louvain pyyaml`