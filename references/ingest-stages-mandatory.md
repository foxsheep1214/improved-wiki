---
name: improved-wiki
description: "强制 Ingest Stage 清单——基于 NashSU v0.4.25 autoIngestImpl() 流水线的 ~15 ingest + lint + graph Stage 规范，每个 Stage 含作用/跳过代价/产物/go-no-go 判断。用于约束任何 wiki 项目执行 ingest 时不漏步。"
tags: [ingest, mandatory, nashsu, pipeline]
related: [SKILL.md §7, known-issues, multimodal-vlm-pitfalls]
---

# 强制 Ingest Stage 清单

## 为什么需要"强制"？

Karpathy LLM-Wiki 模式 + NashSU LLM Wiki app (v0.4.25) 的 `autoIngestImpl()` 流水线包含 **~15 个 ingest Stage + lint + graph**（编号与 `ingest.py` 代码一致）。**任何一个 Stage 都不能跳过**——即使后续 Stage 看起来成功了，也不能"先跑再说"。

**跳过的代价**：
1. **raw 是 sacred**（Layer 1 原则）—— PDF 里的图也是 raw 的一部分，跳过图片提取 = 丢了一半知识
2. **审计可追溯性**（三层模型）—— 缺了 stage 产物，审计时无法回溯"为什么这个页面这么写"
3. **增量缓存前提**（ingest cache）—— 不写 hash cache，下一次 ingest 不知道哪些文件已处理
4. **错误累积**（NashSU 实测）—— 跳过的 stage 永远不会被补做，错误会一直留在 wiki 里

**违反此清单的代价**已在 2026-06-11 HardwareWiki 第一次 ingest 中真实发生：漏掉 Stage 0.5/0.6 后，source 页面里没有任何图片引用——因为没强制流程就没人会回头补。

## 阶段编号说明

本文件所有 Stage 编号已与 `ingest.py` 代码对齐。对应关系如下：
- **本文编号**：各章节 `### Stage X.Y` 使用的编号（与代码一致）
- **代码函数名**：代码中对应的实现函数

| 本文编号 | 代码函数名 | 说明 |
|---------|-----------|------|
| 0 | `extract_text` / `detect_pdf_type` | 文本提取 + PDF 类型检测 |
| 0.5 | `stage_0_5_extract_images` | 图片提取 |
| 0.6 | `stage_0_6_caption_images` | 图片 caption |
| 1 | `stage_1_global_digest` | 全局摘要 |
| 1.5 | `stage_1_5_chunk_analysis` | 逐 chunk 分析（NashSU 顺序递进） |
| 2.0 | `stage_2_0_source_page` | 源页面生成 |
| 2 | `stage_2_per_chunk_generation` | 概念/实体逐 chunk 生成 |
| 2.3 | `stage_2_3_query_generation` | 问题生成 |
| 2.5 | `stage_2_5_comparison_generation` / `stage_2_5_review_suggestions` | 对比生成 + 审查建议 |
| 3 | `write_wiki_file` (主写入循环) | 文件写盘 |
| 3.5 | `stage_3_5_inject_images` | 图片注入 |
| 2.6 | `stage_2_6_aggregate_repair` | 聚合修复 + 缓存 |
| 4 | `_auto_embed_new_pages` | 嵌入向量化 |

> **注意**：`2.5`（审查建议）和 `2.6`（聚合修复）在代码中属于 Phase 2 的编号，但实际在 Stage 3（写盘）之后执行。编号反映的是概念归属（生成阶段），而非严格执行顺序。执行顺序见下方强制顺序。

## 强制 Stage 清单（~15 步）

每个 Stage 都标了：
- **作用**：该 Stage 做什么
- **跳过代价**：跳过的具体后果
- **产物**：Stage 完成后必须存在的文件
- **go/no-go 判断**：怎么知道这个 Stage 算"真的完成"了

## Pre-Ingest Gate：Raw 文件命名规范检查 ⭐ **Stage 0.1 之前强制执行**

> **定位**：此 gate 不属于 ingest pipeline 的阶段编号。它检查的是 raw/ 目录的**管理状态**（命名规则是否存在、新文件是否合规），而非单个文件的消化进度。有独立的检查逻辑，不应与 Stage 0.1 的去重耦合。

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
  4. 全部符合 → ✅ 进入 Stage 0.1
- **go/no-go 判断**：
  - `raw/NAMING.md` 存在 **且** 候选文件全部合规 → 进入 Stage 0.1
  - 否则 → 🛑 阻止 ingest

### Stage 0.1 · 源页去重检查 ⭐ **任何文件选取前强制执行**

- **作用**：检查候选文件是否已消化。**唯一判断依据：`wiki/sources/<raw-rel-path>.md` 是否存在。** `<raw-rel-path>` = raw 文件相对于 `raw/` 的路径（去掉 `.pdf` 后缀），镜像 `raw/` 的目录结构。源页是 Stage 3.5 写入的不可变记录，永远不会被 pipeline 删除或覆盖。源页存在 = 消化完成 → 跳过。不存在 → 进入 Stage 0.3。
- **跳过代价**：重复消化已完成的书籍，浪费 LLM token、OCR 时间，且并行场景下可能导致 index.md / log.md 竞态覆盖。
- **为什么只用 `wiki/sources/`，不查 `ingest-cache.json`**：
  - **`wiki/sources/` 是不可变记录**：每个成功的 ingest 在 Stage 3.5 写入一个源页，pipeline 永不删除或覆盖它。
  - **`ingest-cache.json` 不可靠**：2026-06-14 HardwareWiki 两次事故——(a) agent 忽略缓存选了已消化的书；(b) 10 本书源页存在但缓存缺失。缓存可以被手动删除、跨对话丢失、runtime 目录切换后找不到、并发写入损坏。它只适合作为 ingest.py 内部的性能优化（跳过哈希计算），**绝不用于去重判断**。
- **产物**：过滤后的待消化文件列表（过滤残缺 ingest：源页存在但引用的 concepts/entities >80% 丢失的会重新消化）。
- **go/no-go 判断**（2026-06-17 改为完整性校验，不只是检查源页存在）：
  - `wiki/sources/<raw-rel-path>.md` 存在 **且** 解析 `[[wikilinks]]` 验证 >80% 的 concepts/entities 页面存在 → 跳过。
  - 源页存在但引用的 concepts/entities 丢失 >80%（或源页无任何 wikilinks）→ 不跳过，重新消化。**防止上次 ingest 中途崩溃后留下残缺源页。**
  - `wiki/sources/<raw-rel-path>.md` 不存在 → 未消化，进入 Stage 0.3。
  - **不依赖对话历史、agent 记忆、`ingest-cache.json`、或文件名猜测。**

### Stage 0 · PDF 文本提取（按 PDF 类型分两路径）

**先判断 PDF 类型**（2026-06-18 改为跳过首尾随机采样 + 四信号检测），采样方式：跳过首页（封面/扉页）和末页（索引/封底），从剩余中间页中随机挑 5 页。不足 5 页的短 PDF 全量采样中间页，不足 3 页的采全部页面。空白页（<10 chars）自动跳过不计入统计。按结果走不同路径：

- **信号 ①**：`get_text()` 平均 chars/page
- **信号 ②**：渲染页面低分 Pixmap，检查非白像素占比（>80% 即视为全页扫描图）。这是 **2026-06-14 Johnson《High-Speed Signal Propagation》教训**引入的补充检测——OCR 处理的扫描版 PDF，其背景扫描图可能以 PyMuPDF `get_images()` 无法枚举的形式存储（form XObject / masked image / inline image），导致信号 ③ 漏检。仅靠 `get_images()` 是不够的。
- **信号 ③**：`get_images()` 返回的嵌入图数量。**⭐ OCR处理扫描版的最可靠检测信号**。如果每页只有一个大图（>50% 页面面积），说明 PDF 本质是扫描版——OCR 文字层是后来嵌入的。2026-06-15 童诗白《模拟电子技术基础》：信号①=609c/p（文字层达标），信号②=7%（漏判），信号③=100%（每页一个嵌入大图）→ 判定为扫描版。**信号③ 必须作为三信号检测的首要判断依据。**

#### 路径 A：文本层 PDF（chars/page >500 且全页大图占比 <60%）

- **作用**：PyMuPDF `page.get_text()` 直接抽文本层
- **跳过代价**：无；这是最快路径
- **产物**：`full.txt`（合并所有页）
- **go/no-go**：平均 chars/page >500 且抽样页中全页大图占比 <60%。**但如果书籍内容以图表为核心（信号完整性、眼图、波形图、电路图等），即使字符数达标，也优先选路径 B——图表丢失的代价远大于 OCR 的时间成本。**

#### 路径 B：扫描版 PDF（chars/page <50，或抽样页中 >60% 有全页大图，或图表密集型书籍）

- **作用**：强制走本地 minerU VLM OCR，同时提取文字和图片。**⚠️ OCR 处理的扫描版 PDF 会有高质量文字层（chars/page 可达数百甚至上千），2026-06-17 新增第四信号"隐藏 OCR 层检测"：即使 chars/page >500，如果 >30% 抽样页有全页大图，判定为 `mixed` 走 OCR 路径，防止 Johnson 事故（文本达标但图表全丢）再次发生。**
- **跳过代价**：扫描版 PDF 仅走 PyMuPDF → 全页波形图/眼图/示意图丢失 → 对于信号完整性、电路设计等图表密集型书籍，丢失了一半以上的知识价值。**2026-06-14 Johnson《High-Speed Signal Propagation》实际发生**：100% 页面有全页大图，但 OCR 文字层字符数达标，最终走了路径 A，全本图表丢失。
- **产物**：每页一个 `p<NNN>.txt`（与页号 1:1 对应）+ minerU 自动提取的图片
- **go/no-go**：每页 chars >100；无幻觉（chars<100 且无中文字符 → 重跑）；确认 minerU 输出的 `images/` 目录包含图表
- **关键实操**：扫描版 PDF 全本 OCR 使用本地 minerU（`~/.venv/bin/mineru -b vlm-engine`），免费、自动提取图片、无需 API key。可通过 `MINERU_BACKEND` 环境变量切换后端（vlm-engine / hybrid-engine / pipeline）。**并发限制**：系统级最多 1 个 minerU OCR 任务串行执行（`MINERU_MAX_CONCURRENT=1`）。`ingest.py` 在每次 minerU 调用前通过 `_wait_for_mineru_slot()` 自动排队，等待时显示当前占用文件名和累计等待时间（如 `[mineru] ⏳ 并发槽已满 (1/1)「图解传热学」— 已等待 X 分钟，30s 后重试...`），无需人工协调。

**Stage 0.3 Pilot：OCR 质量验证（2026-06-17 改为 auto-fallback，不再阻塞）**
- **交互模式**（`--pilot-confirmed`）：先本地 minerU 切 5-10 页 → OCR → 人工看输出质量 → 确认后全本。适用于调试/单本消化时希望先看质量。
- **批处理/默认模式**（无 `--pilot-confirmed`）：自动 OCR 降级，不阻塞。OCR 输出 <2000 chars 时标记 `low-quality` 警告但不中断。**避免批处理被 pilot 阻塞数小时无人看管。**
- **仍会运行**：`scanned` 和 `mixed` PDF 始终走 minerU OCR 路径，只是不再需要人工确认 gate。

### Stage 0.5 · 图片提取 ⭐ **永远不能跳**
- **作用**：用 PyMuPDF `get_images()` 抽取 PDF 每页的嵌入图，存到 `wiki/media/<type>/<pdf-stem>/`。**`<pdf-stem>` = PDF 文件名去 `.pdf` 后缀，与 `wiki/sources/<pdf-stem>.md` 共用同一个 stem。2026-06-15: 出现同一 PDF 被两次 Stage 0.5 用不同 slug 命名产生两个 media 目录的 bug，根因是 `source-slug` 未强制等于 PDF stem。**
- **跳过代价**：图全部丢失，wiki 文字描述无法引用图，故障排查价值砍半
- **产物**：`wiki/media/<type>/<pdf-stem>/p<N>-fig<K>.<ext>` + manifest.json
- **go/no-go**：扫描完所有页，统计抽出的图总数 > 0；如确实没有图，在 source 页 `## Embedded Images` 段写"无嵌入图"
- **必须含**：
  - 文件命名带页号（`p123-fig4.png`）便于回溯
  - sha256 去重（一图复用多页只存一份）
  - 尺寸过滤（< 100×100 像素的装饰/logo 剔除）
  - manifest.json 记录：图路径 / 来源页 / 尺寸 / sha256
  - **方向修正**（2026-06-15）：使用 `fitz.Pixmap(doc, xref)` 而非 `doc.extract_image(xref).raw_bytes`。Pixmap 会应用 PDF 图像变换矩阵，自动纠正旋转/翻转——`extract_image()` 只给原始字节，不处理 PDF 层对图像施加的旋转。CMYK 色彩空间自动转 RGB。如果 Pixmap 对 JBIG2/JPEG2000 等特殊编码失败，fallback 回原始字节。
- **扫描版 PDF 特殊说明**：扫描版的"图"是整页 PNG（不是嵌入 raster），Stage 0.5 在扫描版路径下不走 PyMuPDF `get_images()`，由 Stage 0.5 的页图天然承担。Stage 3.5 注入 source 页时直接引用 page-level PNG 即可

### Stage 0.6 · 图片 captioning ⭐ **永远不能跳**
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
- **HardwareWiki 实测选择**（2026-06-11 无源器件篇扫描版）：`anthropic/v1/messages` 多图批量 caption（minimax M3，5 张/请求约 3.5 秒/张，比 OCR 任务更快因为输出短）。Stage 0.6 走跟 Stage 0.5 相同的 endpoint 即可。

### Stage 1 · Analysis（Global Digest）
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

### Stage 1.5 · Chunk Analysis
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

### Stage 2 · Source/Concept/Entity Generation（统一 barrier-free pipeline）

- **作用**：与 Stage 1.5 合并为 **barrier-free pipeline**：analyze chunk → generate pages → next chunk。对所有 chunk 数统一——1 chunk = 单次循环，N chunks = N 次循环。每个 chunk 分析完立即生成概念/实体页，不等全部分析完成。**仅生成 source / concept / entity 三种 page type**。
- **为什么统一**：NashSU 对单 chunk 书用 legacy synthesis（多轮追问），但单次 synthesis LLM 调用经常因 token 超限或超时失败。barrier-free 每 chunk 一次小调用，稳定且可恢复。
- **产物**：FILE blocks → `parse_file_blocks()` → 写入 `wiki/` 目录
- **输出格式**：`---FILE:wiki/<path>---\n<markdown content>\n---END FILE---`
- **go/no-go**：
  - `stages.file_blocks_generated ≥ 1`
  - source page FILE block 存在
  - 概念页路径在 `wiki/concepts/` 下（不在 bare `wiki/` 或 `wiki/sources/`）
  - 至少 1 个 chunk 产出 ≥ 1 个 block
- **fallback**：barrier-free 产出 0 个 concept → 自动降级为 per-concept 生成（每个 concept 一次 LLM 调用）

### Stage 2.3 · Query Auto-Generation ⭐ **新增 2026-06-16**

- **作用**：基于 Stage 2 已生成的 concept/entity 列表，识别书中**提出但未完全解答**的开放问题，生成 `wiki/queries/<slug>.md` 页面。query 是知识演化链中"从已知到未知"的第一跳——把书中隐含的认知边界显式化为可追问的问题。这是纯知识产出（内存中的 FILE blocks），由 Stage 3.5 统一写盘。
- **跳过条件**：source 类型为 `datasheet` 或 `standard` 时自动跳过（纯事实罗列，不产生有意义的开放问题）。
- **产物**：0-5 个 `wiki/queries/<slug>.md` 页面，或 `---QUERIES: 0---` 标记。
- **go/no-go**：
  - 生成了 0-5 个 query FILE block 或 `---QUERIES: 0---` 标记
  - 每个 query frontmatter 含 `type: query` + `title:` + `sources:` 三必填字段
  - 每个 query body ≥200 字符（不含 frontmatter）
- **prompt 模板**：见 `references/query-generation.md`

### Stage 2.5 · Comparison Auto-Generation ⭐ **新增 2026-06-16**（2.5A/B/C）

- **作用**：生成对比分析页面，分三种场景：
  - **2.5A 域内消歧义**：新 concept 名称与 wiki 已有 concept 同名但不同 domain → 创建/更新消歧义页（`type: comparison`, `domain: general`）。对齐 NashSU `domains.md` 消歧义规则。
  - **2.5B 源内概念对比**：同一源内两个高度相关的概念天然适合对比（如 CCM vs DCM、EMI vs EMC）→ 生成对比页（对比维度 ≥4）。
  - **2.5C 跨源对比**：新 concept 与已有 wiki concept 有可比性 → **仅标记 suggestion** 到 Stage 2.5 review，不自动生成（需人工触发，因跨源对比需读取双方完整 concept 页面，token 消耗大）。
- **跳过条件**：本次无 concept 产出（纯 stub source）时自动跳过。
- **产物**：0-2 个 `wiki/comparisons/<slug>.md` 页面（消歧义 + 源内对比），或 `---COMPARISONS: 0---` 标记。
- **go/no-go**：
  - 生成了 0-2 个 comparison FILE block 或 `---COMPARISONS: 0---` 标记
  - 每个 comparison frontmatter 含 `type: comparison` + `title:` + `domain:` 三必填字段
- **prompt 模板**：见 `references/comparison-generation.md`

### Stage 2.5 · Review ⭐ **永远不能跳**（2.5 review，但低于阈值时自动 skip）

- **作用**：Phase 4 的 LLM 质量审查，分两步：
  1. **生成 review items**：当满足 NashSU 3 条件（≥4 FILE 块 / ≥10K 字符 / 未闭合 REVIEW）时，跑一次 LLM 调用输出 5 类 review items：confirm / suggestion / missing-page / contradiction / duplicate。
  2. **解析并写入**：把 LLM 输出的 review items 解析并写入 `wiki/REVIEW/<type>/<date>-<source>-<short-slug>.md`（按 review type 分子目录，文件名含动作简述，含 frontmatter `resolved: false`），同时写入 `review-suggestions.json` 到 runtime dir。
- **自动跳过条件**：NashSU 3 条件全不满足时跳过。但即使跳过，仍记录"LLM 主动认为无问题"。
- **产物**：`wiki/REVIEW/<type>/` 子目录下的 .md 文件 + `review-suggestions.json`（runtime dir）
- **go/no-go**：review items 数量 ≥ 0（即使 0 也要记）；`wiki/REVIEW/` 目录结构存在且合法

> **高层知识空缺检测（synthesis / finding / thesis / methodology）已移至 lint 系统。** 这些检测扫描的是 wiki 整体健康状态而非单次 ingest 的产物质量，语义上属于 lint 范畴。触发条件和输出格式见 `references/knowledge-gap-lint.md`。

### Stage 3 · Write files（含 source page gate）

- **作用**：Phase 3 唯一的磁盘写入入口。分两步：
  1. **Source page gate**（内存）：扫描 Phase 2 产出的所有 FILE blocks，检查是否包含 source 页。如果没有，从 Global Digest 自动生成一个 stub 追加到 blocks 列表。
  2. **原子写盘**：解析所有 FILE 块 → 先 .tmp 再 rename。
- **产物**：所有 wiki/ 下的页面（sources/concepts/entities/queries/comparisons），确保 `wiki/sources/` 与 `raw/` 1:1 对应。
- **go/no-go**：解析出的 page_blocks 数 == 写盘成功数；source page 已落盘

### Stage 3.5 · 图片安全网注入 ⭐ **永远不能跳**（依赖 0.5/0.6）
- **作用**：在 source 页末尾追加 `## Embedded Images` 段，列出所有抽出的图 + caption
- **跳过代价**：图存在但没在 wiki 里被引用 → 用户的 wiki 等于没图
- **产物**：source 页有 `## Embedded Images` 段
- **go/no-go**：source 页包含 `## Embedded Images` 标题 + ≥ 1 行图引用

### Stage 2.6 · Save cache + Aggregate Repair ⭐ **永远不能跳**

- **作用**：两步：
  1. **Aggregate repair**：程序化 append index.md / log.md + LLM 重写 overview.md
  2. **Save cache**：写 `<sha256(raw)>` → `[filesWritten...]` 映射到 `ingest-cache.json`
- **跳过代价**：下次跑同一文件会重做所有 stage；aggregate 页面不更新导致 wiki 导航缺失
- **产物**：`ingest-cache.json`（含本次所有 raw 文件 hash）+ index.md / log.md / overview.md 更新
- **go/no-go**：每个本次处理的 raw 文件都有 hash 记录；旧有条目全部保留 + 新条目已追加
- **2026-06-11 重要发现**：app 的 `cache entry ≠ 产物`。cache 里 `filesWritten=[]` 也会出现"已 ingest"假象。**必须用 `scripts/validate_ingest.py` 验产物侧**，不能只看 cache schema

**🚨 2026-06-13 ADL8113 事故**：NashSU 原生让 LLM 同时输出 index/log/overview，但 LLM 不会读到旧的 wiki 文件内容，静默丢失所有历史。improved-wiki 对策：index.md / log.md 纯程序化 append（LLM 不参与）；overview.md LLM 重写但喂入当前全文作上下文。

### Stage 4 · Embeddings
- **作用**：把 wiki/ 下的页面 chunk 化 + embed，写到 LanceDB
- **跳过代价**：检索只能用纯关键词（wiki < 100 页可接受，> 100 页必须 embeddings）
- **产物**：`lancedb/` 表 + `embed-cache.json`
- **go/no-go**：LanceDB 表存在 + 已写 ≥ N 个 chunk

---

## 强制顺序（不能乱）

```
0.1 → 0.3 → 0 → 0.5 → 0.6 → 1 → 1.5 → 2.0 → 2 → 2.3 → 2.5 → 3 → 3.5 → 2.5(review) → 2.6 → [4]
```

- **Stage 0.3 Pilot 是新强制前置**（2026-06-11）：任何 PDF 走 Stage 0 之前必须先 5-10 页 pilot 验证
- 0.5 **必须先于** 0.6（先有图才能 caption）
- 0.5/0.6 **必须先于** 3.5（3.5 注入图引用）
- Stage 1 / 1.5 **永远不能跳过**（短源 1 chunk / 长源 N chunk，都是 1.5 内部逻辑，不是 skip）
（2026-06-16 新增）：
  
  
  
- **Phase 3 内部顺序**（2026-06-17 订正）：3（写盘，含 source page gate 前置检查）→ 3.5（图片注入，程序化追加）
- **Phase 2（Generation）全部在内存中完成**——2.0（source）→ 2（concept/entity）→ 2.3（query）→ 2.5（comparison）。串行执行：2.3 依赖 2 的 concept 列表，2.5 依赖 2 的 concept 列表 + wiki 已有页面列表。所有产出统一由 Stage 3 写盘。
  - **Stage 2.5（review）运行在已写盘的文件上**，这样 human reviewer 可以直接看到实际页面内容（包括 2.3/2.5 产出的 query/comparison 页面）
  - **Stage 2.6（aggregate repair）在所有页面写盘后运行**，确保 index/log/overview 基于完整的磁盘状态
- 2.3 是 conditional（datasheet/standard 自动跳过）
- 2.5 是 conditional（无 concept 产出时自动跳过）
- 2.5 (review) 是 conditional（NashSU 3 条件触发：≥4 FILE 块 / ≥10K 字符 / 未闭合 REVIEW）
- 2.6 程序化 append index/log + LLM 重写 overview（喂入现有内容防丢失）
- 2.6 在所有 stage 之后（写最终缓存）；hard error（磁盘满/权限）阻止 cache save
- 4 auto-run 当 `EMBEDDING_BASE_URL` 已设置时；否则手动 `build_embeddings.py`

---

## 验证清单（每次 Ingest 完成后必查）

完成一个文件的 ingest 后，**必须**逐项过这个清单：

- [ ] **Stage 0.3 Pilot 已跑**：5-10 页 OCR 输出质量 OK
- [ ] **Stage 0**：源文本已提取（PyMuPDF 文本层 OR mmx vision OCR 后每页 chars >100）
- [ ] **Stage 0.5：图已抽到 `wiki/media/<type>/<slug>/`（数量 > 0 或确认无嵌入图）**
- [ ] **Stage 0.6：每张图有 .caption.txt（长度 ≥ 20 字符）**
- [ ] Stage 1：global-digest.yaml 合法
- [ ] Stage 1.5：所有 chunk analysis 合法
- [ ] Stage 2：generation_response.txt 的 stop_reason == end_turn（**不是 max_tokens**）
- [ ] **Stage 2.3：query 页面已生成或 `---QUERIES: 0---` 已记录**（datasheet/standard 自动跳过）
- [ ] **Stage 2.5：comparison 页面已生成或 `---COMPARISONS: 0---` 已记录**（无 concept 时自动跳过）
- [ ] **Stage 2.5：review items 已生成并写入 wiki/REVIEW/（即使 0 items）**
- [ ] Stage 3：所有 FILE 块写盘成功
- [ ] **Stage 3.5：source 页含 `## Embedded Images` 段**
- [ ] **Stage 2.6：ingest-cache.json 含本次所有 raw 文件 hash**（且 `validate_ingest.py` 通过；ingest.py 末尾自动运行）
- [ ] Stage 4：lancedb 表已更新（如启用 embeddings）

**加粗的 9 个 stage 是最容易跳过的**（也是历史上最容易出事的）：
- Stage 0.3 Pilot（2026-06-11 新增）—— 没 pilot 直接全本 = 浪费数小时
- 0.5（图提取）— 看似 optional，实际丢一半知识
- 0.6（图 caption）— 看似 optional，实际让图无法检索
- 2.3（query 生成）— **2026-06-16 新增**；看似 optional，实际让知识库只有事实没有追问
- 2.5（comparison 生成）— **2026-06-16 新增**；看似 optional，实际让跨概念理解和消歧义缺失
- 2.5（review suggestions）— 看似 optional，实际让错误内容永久残留
- 3.5（图注入 source 页）— 看似 optional，实际让图与 wiki 脱节
- 2.6（cache 写入）— 看似 optional，实际下次跑会重做所有 stage

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

本清单的 17 项是**人工 check 用的**，但验证已在流水线中自动化：

### 自动验证（ingest.py 内置，2026-06-16+）

**每个 Stage 完成后有实时验证门禁**（`_verify_stage_N()`），失败直接 `RuntimeError` 中止：

| Stage | 门禁检查 | 失败行为 |
|-------|---------|---------|
| Stage 0 | 提取文本 ≥ 500 字符；MinerU ≥ 2000 字符 | RuntimeError |
| Stage 1 | Global Digest 含 5 个必需 key；≥ 1 个 concept | RuntimeError |
| Stage 1.5 | chunk 分析非空 | RuntimeError |
| Stage 2 | ≥ 1 个 FILE block；source page 存在；路径正确 | RuntimeError |
| Stage 3 | source page 落盘 | RuntimeError |

**Ingest 末尾自动运行 `validate_ingest.py`**（全阶段验证），结果打印到 stdout。

遵循 superpowers Iron Law：**NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE**。

### 手动补充验证

```bash
# 结构性 lint（覆盖 wikilink 健康）
./scripts/wiki-lint.sh --summary

# 图存在性（覆盖 Stage 0.5 / 0.6 / 3.5）
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

- **2026-06-11**：初版（源于 HardwareWiki 第一次 ingest 漏掉 Stage 0.5/0.6 事故）
- **2026-06-13**：Stage 2.6 从 LLM 重写改为程序化 append（ADL8113 事故教训）；Stage 1/1.5 YAML schema 对齐；Stage 2.5 触发阈值修正（≥4 FILE 块）
- **2026-06-14**：Stage 0.1 去重检查新增；Stage 0 三信号检测升级（Johnson 事故）；NashSU v0.4.23 parity audit 完成
- **2026-06-16**：新增 Stage 2.3 Query + 2.5 Comparison；阶段间实时验证门禁
- **2026-06-19**：全面重编号对齐 `ingest.py` 代码（废弃 Phase.序列），清理所有过时引用
- **2026-06-17**：高层知识空缺检测移至 lint 系统（`knowledge-gap-lint.md`）；REVIEW 目录分子目录；Phase 4+5 合并；Stage 3.1 合并入 3.5；Stage 4 合并入 2.5 review
- **2026-06-17**：新增 **Stage 16-18 知识图谱后处理**（Lint 阶段，不在 ingest 管线内）。四信号加权图构建 + Louvain 社区检测 + 图谱洞察输出。脚本：`scripts/build_knowledge_graph.py`。触发时机：批量 ingest 后按需运行，不在单次 ingest 中自动执行。


## Lint 阶段：知识图谱（Stage 16-18）

> **定位**：Lint 阶段（不在单次 ingest 管线中）。Ingest 管线的 16 Stage 不碰图——图建在 lint，图用在 ingest（Stage 2 可通过 `--mode query` 查询已有图为新页面建议 wikilinks）。触发时机：完成一批 ingest（≥10 本新书）后手动运行，或 cron 定期执行。

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
# 全量分析（lint 阶段，批处理）
IMPROVED_WIKI_ROOT=/path/to/wiki python3 scripts/build_knowledge_graph.py

# 仅查看统计（不写文件）
IMPROVED_WIKI_ROOT=/path/to/wiki python3 scripts/build_knowledge_graph.py --dry-run

# 大 wiki 先小规模测试
IMPROVED_WIKI_ROOT=/path/to/wiki python3 scripts/build_knowledge_graph.py --dry-run --limit 500

# ingest 时查询：给新页面推荐 wikilinks
IMPROVED_WIKI_ROOT=/path/to/wiki python3 scripts/build_knowledge_graph.py \
  --mode query --slug "my-new-page"

# 调整 cohesion 告警阈值（默认 0.15）
IMPROVED_WIKI_ROOT=/path/to/wiki python3 scripts/build_knowledge_graph.py --min-cohesion 0.10
```

**依赖**：`pip install networkx python-louvain pyyaml`