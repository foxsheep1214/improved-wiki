# Known issues / bugs in `improved-wiki`

## Open issues

### Several files exceed the 800-line guideline
`_core.py`（~1480 行）。暂无明显自然切分点，未拆。
`ingest.py`（~680 行）协同 `_ingest_skip.py` / `_ingest_chunks.py` / `_ingest_prepare.py` / `_ingest_write.py` 四个编排子模块。
`_stage_1_extract.py` 是 facade，re-export 兄弟模块 `_stage_1_1_scanned.py` / `_stage_1_2_images.py` / `_stage_1_3_caption.py` 的公开名，外部导入者无需改动。
`_stage_2_4_generation.py`（~1326 行）仍超阈值（source-anchored 逐 chunk 生成 + 源内去重收尾逻辑集中，暂未找到干净切分点）。

### minerU 偶尔把公式区域分类为 `image` 而非 `equation`
~112 公式图被当图片送 VLM，而非用 minerU 已提取的 LaTeX 文本（上游 minerU 版面分析问题）。

### 跨目录同名 basename 的 slug 碰撞（dedup/merge 侧未彻底解决，2026-07-10）
`cross_source_dedup.py` 全链路用 `_slug_from_path()`（只取文件名 stem）作页面 id：`queries/skolnik-m-i.md` 和 `entities/skolnik-m-i.md` 映射到同一个 slug，slug 键的 dict 会静默塌缩成一条（后者覆盖前者）。实测（RadarWiki 2026-07-10）：`matched-filter`、`skolnik-m-i` 在 detector 候选清单里各出现两次。**已修的部分**：embedding 预筛的 `emb_pages` 现在按 slug 去重，不再产生重复 id。**未修的部分**：若一个合并组恰好包含碰撞 slug，merge 可能读到/删掉错误目录下的同名文件——根治需要全链路改用路径作 id（较大改动）。**规避**：合并前先核对组内 slug 是否存在跨目录同名文件（`find wiki -name "<slug>.md"` 多于一个结果即碰撞）；这类组先人工处理。

### `detect_language()` 非拉丁文字阈值过低，几个杂散字符就能误判全书语言
`_language.py::detect_language()` 的非拉丁脚本判定阈值只是 `max_count >= 2`，而英文本身纯 ASCII、不计入对照基准。实测：某书扉页的外文图书馆公章（OCR 出十几个非拉丁字符）导致全书正文被误判成该语言，各生成 stage 收到错误的 "MANDATORY OUTPUT LANGUAGE" 指令，而全书 99%+ 是英文——与已记录的"São Paulo 陷阱"（`improved-wiki-language-detect-false-positive` 内存条目）同一类假阳性，但触发方式更直接。Greek 分支已有"孤立单字符不算希腊语"的保护（`_has_greek_word_run`），其他非拉丁脚本分支没有。**当前规避**：生成阶段人工判断源文本主体语言、忽略错误的语言指令；项目级可用 `IMPROVED_WIKI_OUTPUT_LANGUAGE=English` 强制覆盖整本书。**未修复**：给非拉丁脚本分支加类似 Greek 的保护（改动前需先补测试用例，避免影响现有中文等双语页面的检测）。

### Stage 2.6 source 页偶发整体丢失 section 结构（单次事件 2026-07-07，根因未锁定）
现象：一本书首次摄入的 source 页落盘后不走模板（自创标题、必需 section 大面积缺失），但归档的 Stage 2.6 conversation 响应本身完全合规——损坏发生在生成"之后"、写盘"之前/期间"，日志粒度不足以锁定具体代码，此后未复现。现有防线：`_stage_2_6_validate_required_sections()` 硬门禁（doctype-aware 必需 H2 清单，缺失即 raise），覆盖"LLM 自创结构"这一失败模式；写盘窗口本身仍无写盘后校验。**行动项：如再复现，保留当次 `.llm-wiki/conversation/<hash>/` 目录不清理，为根因排查留证据。**

## Design decisions (not bugs)

### `ingest.py` 用 `urllib.request` 不用 `httpx`/`requests`
刻意避免 cron 语境下 `pip install`。

### 必须用 venv Python（系统 Python 缺 fitz + 版本太旧）
用 `~/.venv/bin/python3`（需 3.10+，系统 3.9 不支持 PEP 604）。完整说明见 `references/scripting-pitfalls.md` Pitfall 4——这是 #1 首次运行失败原因。

### 删除页面后 LanceDB 留残留向量，需重 embed
`build_embeddings.py embed` 是 `mode="overwrite"` 全量重建，全 skill 无增量删向量的 API。任何删除——`--delete`（源生命周期）和 lint `--delete-orphans`——都不清向量块，被删页可能在向量搜索里短暂命中（链接已失效），直到下次 `build_embeddings.py embed` 重建即清除。NashSU 用增量 `removePageEmbedding`；CLI 用整表重建达成同一端状态，刻意不移植增量删（YAGNI，且与重建式索引不符）。删除后想立即干净就重跑 embed。

### Wikilink enrichment merge loop after Stage 3.1
Stage 3.1 写盘后，pipeline 生成多个 `LLM-task-*.md` merge prompt（`.llm-wiki/conversation/<hash>/`），每个让 agent 把已有 wiki 页与新内容合并。re-run 时会重新发现并 re-merge。高效处理：用 `delegate_task` 批量；wikilink 建议 JSON 任务输出 `{}` 可安全跳过（Stage 2.4 已加内联 wikilink 时无质量损失）。

### OCR timeout for 200+ page books
minerU 32 页/chunk 串行。272 页书（9 chunks）可能超 600s 终端超时。**重跑 `ingest.py` 从缓存恢复**——已完成 chunk 跳过。`--stop-after-stage 0` 分离 OCR 与 LLM 阶段。

### `--delete` for re-ingest
`ingest.py --delete` 删 source 页 + 孤儿 concepts/entities + media + cache，再重跑即可干净重摄；先问用户 full-redo 还是 analysis-only（`--keep-media`），权威流程见 `re-ingest-comparison.md`。

### Bash 工具 cwd 不在调用间持久
`ingest.py` 靠 `Config.from_env`（`IMPROVED_WIKI_ROOT` env 或 `os.getcwd()`）解析项目根；没有 `--project` 参数。每次调用前必须显式 `cd <project> && ...`——不能指望上一次 `cd` 还生效。cwd 错了会直接 file-not-found。

## Fixed bugs（回归意识——已修但值得记录症状）

### Stage 2.2 prompt 的 YAML/LaTeX 转义坑（已修，2026-06-27）
Prompt 曾用双引号包公式（`formula: "LaTeX"`），未强制 YAML 单引号；含 `\`/`$` 的字符串在双引号 YAML 里会被静默改写或让 `yaml.safe_load` 抛错，fallback parser 拿不到 `concepts_found` → 该 chunk **静默生成 0 个页面**（无报错）。已修：要求含 `\`/`$` 的字段用单引号。**操作陷阱**：改这个 prompt 模板会变更每个 chunk 的 prompt hash，在飞 chunk 结果全部作废（文件名不匹配）；若旧结果内容本身没变，`cp old-hash.txt new-hash.txt` 可免重跑。

### Stage 3.7 embedding 因路径双重前缀被静默跳过（已修，2026-06-30）
`files_written` 条目已带 `wiki/` 前缀，旧代码又拼一次 `config.wiki_dir`（已是 `wiki_root/wiki`），产出永不存在的 `wiki/wiki/...` 路径 → `new_files` 恒空 → Stage 3.7 无日志无报错直接返回，`_finalize_book` 却照常打完成标记——**新页面从未被 embed，书标"完成"，lancedb 停留旧状态**。已修：改为先按 `wiki_root` 解析。**诊断信号**：每次 ingest 应看到 `[stage 3.7] Embedding N new pages...`，缺失即说明被跳过（此修复前摄入的项目需手动 `build_embeddings.py --project <root> embed` 补嵌入）。

### 大型 wiki 首次批量补嵌入：超时 + 缓存不收敛（已修，2026-06-30）
上条修复后首次真实批量嵌入暴露两个叠加 bug：(1) 硬编码 `timeout=300`，大 wiki（6000+ chunk）实测必超时；(2) 缓存只在整批结束后写一次，超时/kill 会丢光本次已算的全部向量——两者叠加导致**永远无法收敛**（反复重做又反复丢失）。已修：超时按页数缩放 `max(600, page_count*2)`；缓存改为每 `SAVE_EVERY=512` chunk 增量存盘。**恢复法**：大型 wiki 首次批量嵌入，先单独跑 `build_embeddings.py --project <root> embed`（无超时上限）清空积压，再跑 `ingest.py`，Stage 3.7 命中热缓存秒级完成。

### snap_out 在表格密集书上曾产出异常极小 chunk（已修，2026-06-30）
Chunk 窗口末端落在受保护 block（表格/代码块）内部时曾无条件整体回退到该 block 起点；表格密集书中一张早早开始的巨表会把 chunk 收缩成表格前的极小片段——不丢数据（下一 chunk 靠 overlap 重新覆盖），但浪费一次 LLM 往返分析近空 chunk。已修：只有回退后仍留下有意义内容（`r[0]-start >= attempted//2`）才回退，否则跳过整个 block。**操作纪律：chunker 边界逻辑只能在书与书之间改，绝不能在书摄入中途改**——chunk 分析按内容 hash 缓存，改边界会废掉当前在飞书的已完成 Stage 2.2/2.4 缓存。

### Stage 2.3 标题 Jaccard 去重漏判重音/标点变体（已修，2026-06-30）
既有页关联/去重只按 `[\s/]+` 切分+小写+去停用词，不折叠重音、不去标点。已存在页 "Thévenin's Theorem" 因此漏配新生成的 "Thevenin's Theorem"（词集合交集只有 `{theorem}`，Jaccard 0.33 < 0.5 阈值，精确 slug 匹配也因撇号差异失败）——**结果是生成了一个重复页**。已修：每个 token 先过 `unicodedata.normalize("NFKD", ...)` 折叠重音再去标点，才做 Jaccard 比较。**范围**：只防未来新重复，不回溯清理已存在的跨书历史重复 slug 变体（更大的独立课题，见 `dedup-design.md`）。

### Stage 2.4 生成概念数上限按行数算，曾静默丢尾部概念（已修，2026-06-30）
`_stage_2_4_generation.py` 原按**行数**截断 GENERATE 列表（每概念约 4 行，实际只放得下约 25-34 个），而可链接 slug 列表本身不受限。chunk 分析密度提升后，密集 chunk 的尾部概念被静默从生成列表剔除、但仍留在可链接列表里——产生指向"从未生成"页面的死链。已修：上限大幅提高（分chunk 480/160，单发 800/200）。**教训**：这类上限必须按概念数算（或留出 ≫ 密度×4 的余量），绝不能是纯行数截断。

### Stage 2.6 源页偶发缺失 authors/year/url/venue（已缓解，非彻底修复）
生成 agent 有时自由发挥自己的格式（如加粗行内署名、metadata 表格）而非照抄 Stage 2.6 模板，pipeline 又原样写盘不校验 frontmatter，字段就此缺失——**这是 agent 未遵循问题，不是代码 bug**。缓解：`_normalize_source_frontmatter()` 在 agent 响应之后跑一遍，从已算出的 `*_meta` YAML 回填缺失的 authors/year/url/venue，并从该 chunk 刚生成的 concept/entity slug 回填空的 `related: []`。**排查手法**：对比归档的 `Stage-2-6-SourcePage-*.md`（提示词）与对应 `.txt`（响应），区分"模板本身缺字段"（真 bug）还是"agent 没照做"（此类问题）。

### `_stage_1_2_extract_from_mineru()` 两处硬编码 width/height=0（已修，2026-07-06）
两个分支（img_source_dir 存在时的正常复制、OCR 缓存续跑的 media_dir 恢复）在构造 manifest 图片条目时把 `"width": 0, "height": 0` 写死，不像 `_stage_1_2_harvest_images()` 那样用 PIL 读真实尺寸——图片文件本身正常，只是元数据没填，caption 失败占位符统一显示"尺寸 0×0"。已修：抽出共享辅助 `_stage_1_2_image_size()`（PIL 读取，读失败兜底 (0,0)），两处硬编码分支改用它；受影响的存量 manifest 已用现存图片文件回填尺寸，无需重跑 VLM。

### Stage 1.3「[待重试]」占位符从未被自动重试（已修，2026-07-06）
`_stage_1_3_is_caption_failed()` 只认几个失败关键词子串（"解析失败"/"sorry"/"unable to" 等），没检查占位符自己的 `[待重试]` 前缀（`{err}` 部分是任意异常信息，通常不含那几个关键词）——占位符被当成正常缓存永久跳过，用户必须手动删 `.caption.txt` 才能强制重跑。已修：加 `text.startswith("[待重试]")` 直接判定为失败。

### `sweep_reviews.py` 规则阶段子串匹配假阳性（已修）
实测 ~15/197 误 auto-resolve。已修：`pageExists` 改为 EXACT 匹配（文件名 id / kebab 归一化 id / frontmatter title 三种精确等值，无子串匹配，对齐 NashSU）。保留 dry-run 先行的习惯即可。

### deep-research query 桥接文件误生成 `wiki/sources/queries/` 源页 + 会无限重摄（已修，2026-07-15）
`references/deep-research.md` 早就明确 query 桥接（`raw/queries/*.md`）只该跑 2.2/2.4/2.9/3.4/3.5/3.7，**不含 Stage 2.6**——但代码从未真正跳过 2.6，一直无条件生成 `wiki/sources/queries/<slug>.md`，跟 `wiki/queries/<slug>.md`（人类可读研究页）内容重复。更严重的是 `_stage_0_2_should_skip` 的完整性检查依赖"source 页是否存在"判断 `ingested` 标记是否过期——一旦改成不生成 source 页，每次调用都会误判为"标记过期"并清掉，导致**无限重摄、每次都重新生成一遍重复的 concept/entity 页**。已修：新增 `is_query_bridge_source()`（`_core.py`），在 `_ingest_prepare.py` 跳过 2.6 调用、`_verify_stage_2_4_file_blocks` 跳过"必须有 source 块"的硬校验、`_stage_0_2_should_skip` 对桥接文件只信 `ingested` 标记本身，不再检查 source 页是否存在。**存量清理**：此修复前已生成的 `wiki/sources/queries/*.md` 需手动删除（连带 index.md/log.md 里的引用行、ingest-cache.json 对应条目的 `filesWritten` 项），已在 HardwareWiki、RadarWiki 各清理 1 个。

### `.digested` files in `raw/` subdirectories
旧 pipeline 标记。当前 pipeline（Stage 0.2）用 `wiki/sources/` 作唯一去重信号。清理见 `maintenance-cleanup.md`。

## Batch digest patterns
批量摄入 pitfalls 见 `batch-digest-loop.md`。一句话：把确认过的完整文件列表一次交给
`ingest.py`；Phase 1 自动做受控 OCR/caption 流水，Stage 2.2 串行，Stage 2.4
按 `--parallel` 分波并行，跨书 Stage 2.3+ 由 durable spine reservation 串行。
