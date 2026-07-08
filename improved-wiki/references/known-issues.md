# Known issues / bugs in `improved-wiki`

## Open issues

### Several files exceed the 800-line guideline
`_core.py`（~1434 行）。暂无明显自然切分点，未拆。
`ingest.py`（~692 行）协同 `_ingest_skip.py` / `_ingest_chunks.py` / `_ingest_prepare.py` / `_ingest_write.py` 四个编排子模块。
`_stage_1_extract.py` 是 facade，re-export 兄弟模块 `_stage_1_1_scanned.py` / `_stage_1_2_images.py` / `_stage_1_3_caption.py` 的公开名，外部导入者无需改动。
`_stage_2_4_generation.py`（~1326 行）仍超阈值（source-anchored 逐 chunk 生成 + 源内去重收尾逻辑集中，暂未找到干净切分点）。

### minerU 偶尔把公式区域分类为 `image` 而非 `equation`
~112 公式图被当图片送 VLM，而非用 minerU 已提取的 LaTeX 文本（上游 minerU 版面分析问题）。

### `detect_language()` 非拉丁文字阈值过低，几个杂散字符就能误判全书语言
`_language.py::detect_language()` 的非拉丁脚本判定阈值只是 `max_count >= 2`——只要样本文本里出现 ≥2 个某非拉丁文字的字符，就判定整份文档是那个语言，而英文本身是纯 ASCII、完全不计入 `counts`，等于没有对照基准。实测：《Fundamentals of Radar Signal Processing - 2005 - Richard》扉页有两张伊朗大学图书馆的波斯语/阿拉伯语公章（OCR 出十几个阿拉伯字符），导致各分析/生成 stage（当时含 2.1，现为 2.2/2.4/2.6）全部注入"MANDATORY OUTPUT LANGUAGE: Arabic"，而全书正文 99%+ 是英文——这是和已记录的"São Paulo 陷阱"（`improved-wiki-language-detect-false-positive` 内存条目）同一类假阳性，但触发方式更直接（真实非拉丁文字，不是地名误判）。现有 Greek 分支已有"孤立单字符不算希腊语，需要多字符连续词"的保护（`_has_greek_word_run`），但阿拉伯语等其他非拉丁脚本分支没有对应保护。**当前规避**：生成阶段人工判断源文本主体语言、忽略错误的语言指令即可（本次已验证可行）；项目级也可用 `IMPROVED_WIKI_OUTPUT_LANGUAGE=English` 强制覆盖整本书。**未修复**：给非拉丁脚本分支加类似 Greek 的"需要有意义占比/连续词"保护，风险是可能影响现有中文等双语页面的检测结果，未做（改动前应先补测试用例）。

### `_stage_1_2_extract_from_mineru()` 两处硬编码 width/height=0（已修，2026-07-06）
`_stage_1_2_images.py::_stage_1_2_extract_from_mineru()` 有两个分支（img_source_dir 存在时的正常复制分支、OCR 缓存续跑的 media_dir 恢复分支）在构造 manifest 图片条目时把 `"width": 0, "height": 0` 写死，而不像同文件里的 `_stage_1_2_harvest_images()` 那样用 PIL 读真实尺寸。后果：凡是走这个函数生成 manifest 的书，`_manifest.json` 里全部图片尺寸恒为 0×0——图片文件本身完全正常，只是元数据没填。表征：caption 失败时的占位符统一显示"尺寸 0×0"，无论实际图片多大（发现于《High Resolution Radar 2nd - 1995 - Wehner》，同一天摄入的《Fundamentals of Radar Signal Processing》走了另一条会算真实尺寸的路径，manifest 正常）。**已修复**：抽出共享辅助 `_stage_1_2_image_size()`（PIL 读取，读失败兜底 (0,0)，跟 `_stage_1_2_harvest_images()` 一致的防御写法），两处硬编码分支都改用它。**已回填**：Wehner 现有 `_manifest.json` 332 张图的尺寸已用现存图片文件补齐，无需重跑 VLM。

### Stage 2.6 source 页偶发整体丢失 section 结构，根因未 100% 锁定（检测网已升级为硬门禁，2026-07-07）
《Fundamentals of Radar Signal Processing - 2005 - Richard》今早首次摄入的 source 页完全没有走模板——标题是自创的 Bibliographic Information/Overview/Chapter Outline/Key Concepts，Main Arguments & Findings / Key Entities / Connections / Contradictions / Recommendations 全部缺失。排查过程：archived 的 Stage 2.6 conversation 产物（`.llm-wiki/conversation/47e0adf0/Stage-2-6-SourcePage-532d2243.txt`，生成时间 11:13:16，仅比 log.md 记录的摄入完成时间 11:17:17 早 4 分钟）本身内容完全合规（7 个 section 齐全、英文、43 条 claim）；`_normalize_source_frontmatter()` 只碰 frontmatter 不碰 body，排除了它改坏内容的可能；也没有可复用的旧 source 页触发 merge（首次摄入）；代码里也搜不到硬编码的 fallback 模板匹配这个坏结构。但一份 18 秒后生成的 wikilink-enrichment 提示词（`LLM-task-07027825.md`，11:13:34）里，`## PAGE: sources/Book/Fundamentals of Radar Signal Processing - 2005 - Richard.md` 下面已经是坏结构——**说明损坏发生在 Stage 2.6 生成"之后"、写盘"之前/期间"的 18 秒窗口内**，但受限于现有日志/缓存粒度，未能锁定到具体是哪一行代码/哪一次写入把好内容换成了坏内容。**检测网已升级为硬门禁**（2026-07-07，audit M5）：`_stage_2_6_validate_required_sections(response, source_kind)`（A10）在 `_stage_2_6_source_page.py` 对比 doctype-aware 的 7 个必需 H2 标题（paper 用 Paper Summary/Methodology & Results，其余用 Book Summary/Table of Contents & Key Concepts），缺失即 **raise RuntimeError** 暂停 ingest（不再是 warn-only，与 no-silent-fallback 政策一致）。这覆盖"LLM 自创结构"的失败模式；写盘窗口的损坏仍需写盘后校验（未做，因根因未锁且为单次事件）。**后续如再复现，应保留当次的 `.llm-wiki/conversation/<hash>/` 目录不要清理，为根因排查留证据**。
`sweep_reviews.py` 规则阶段的 title/path 匹配用子串（partial）匹配，短词（`to`/`ul`/`none`/`DC` 等 2-3 字符片段）会误命中无关页面 slug。实测 ~15/197 review items 被误 auto-resolve。**缓解**：先 dry-run（不加 `--apply`），检查可疑 auto-resolve；规则阶段应要求最小匹配长度 ≥4 字符或全 slug 等值。LLM judge 阶段不受影响。详见 `references/review-sweep.md`。

## Design decisions (not bugs)

### `ingest.py` 用 `urllib.request` 不用 `httpx`/`requests`
刻意避免 cron 语境下 `pip install`。

### 必须用 venv Python（系统 Python 缺 fitz + 版本太旧）
用 `~/.venv/bin/python3`（需 3.10+，系统 3.9 不支持 PEP 604）。完整说明见 `references/scripting-pitfalls.md` Pitfall 4——这是 #1 首次运行失败原因。

### 删除页面后 LanceDB 留残留向量，需重 embed
`build_embeddings.py embed` 是 `mode="overwrite"` 全量重建，全 skill 无增量删向量的 API。任何删除——`--delete`（源生命周期）和 lint `--delete-orphans`——都不清向量块，被删页可能在向量搜索里短暂命中（链接已失效），直到下次 `build_embeddings.py embed` 重建即清除。NashSU 用增量 `removePageEmbedding`；CLI 用整表重建达成同一端状态（无残留向量），刻意不移植增量删（YAGNI，且与重建式索引不符）。删除后想立即干净就重跑 embed。

### Wikilink enrichment merge loop after Stage 3.1
Stage 3.1 写盘后，pipeline 生成多个 `LLM-task-*.md` merge prompt（`.llm-wiki/conversation/<hash>/`），每个让 agent 把已有 wiki 页与新内容合并。re-run 时会重新发现并 re-merge。高效处理：用 `delegate_task` 批量；wikilink 建议 JSON 任务输出 `{}` 可安全跳过（Stage 2.4 已加内联 wikilink 时无质量损失）。

### OCR timeout for 200+ page books
minerU 32 页/chunk 串行。272 页书（9 chunks）可能超 600s 终端超时。**重跑 `ingest.py` 从缓存恢复**——已完成 chunk 跳过。`--stop-after-stage 0` 分离 OCR 与 LLM 阶段。

### `--delete` for re-ingest
`ingest.py --delete "raw/Book/<file>.pdf"` 删 source 页 + 孤儿 concepts/entities + media + cache，再重跑即可干净重摄。

### Bash 工具 cwd 不在调用间持久
`ingest.py` 靠 `Config.from_env`（`IMPROVED_WIKI_ROOT` env 或 `os.getcwd()`）解析项目根；没有 `--project` 参数。每次调用前必须显式 `cd <project> && ...`——不能指望上一次 `cd` 还生效。cwd 错了会直接 file-not-found（2026-06-28 起：先校验 raw 文件存在再进 context probe，报错更直白，但 cwd 问题本身不会自动修）。

## Fixed bugs（回归意识——已修但值得记录症状）

### Stage 2.2 prompt 的 YAML/LaTeX 转义坑（已修，2026-06-27）
`_stage_2_analyze.py` 曾教 LLM 用双引号包 `formula: "LaTeX"`，未强制 YAML 单引号规则。双引号 YAML 字符串里 `\t`/`\f`/`\n`/`\b`/`\r` 是合法转义（静默改写公式），`\p`/`\$`/`\x` 等非法转义则让 `yaml.safe_load` 抛错，回退的简易 parser 拿不到按名字索引的 `concepts_found` dict → Stage 2.4 生成提示词看到"（无）"→ 该 chunk **静默生成 0 个页面**（无报错，看起来像正常完成）。修复：prompt 现在要求含 `\` 或 `$` 的字段用单引号（因 prompt 本身是 Python f-string，字面反斜杠要写 `\\`，改 prompt 模板后务必 `inspect` 验证）。**操作陷阱**：中途改这个 prompt 模板会变更每个 chunk 的 prompt hash，所有在飞 chunk 结果变成孤儿（文件名不匹配），需要全部重新分析；若旧结果内容本身没变，`cp old-hash.txt new-hash.txt` 可免重跑（pipeline 只按 hash 文件名匹配，不校验内容）。

### Stage 3.7 embedding 因路径双重前缀被静默跳过（已修，2026-06-30）
`files_written` 条目是相对 wiki_root **且带 `wiki/` 前缀**（如 `wiki/concepts/foo.md`），旧代码却拼到 `config.wiki_dir`（已是 `wiki_root/wiki`），产出 `.../wiki/wiki/concepts/foo.md`，永不存在 → `new_files` 恒为空 → Stage 3.7 无日志无报错地直接返回，`_finalize_book` 却照常打完成标记——**新页面从未被 embed，书标"完成"，lancedb 停留旧状态**。修复：改为先按 `config.wiki_root` 解析（回退 `wiki_dir`）。**诊断信号**：每次 ingest 应能看到 `[stage 3.7] Embedding N new pages...` 日志，缺失即说明 3.7 被跳过。此修复前摄入的项目需手动 `build_embeddings.py --project <root> embed` 补嵌入。

### 大型 wiki 首次批量补嵌入：超时 + 缓存不收敛（已修，2026-06-30）
上一条修好后首次真实批量嵌入暴露两个叠加 bug：(1) `build_embeddings.py embed` 是全量增量扫描（非只扫 new_files），`_stage_3_7_embed.py` 硬编码 `timeout=300`，6301 个未缓存 chunk 实测耗时 368.1s 超时被 no-fallback 策略中断——已改按页数缩放 `max(600, page_count*2)`；(2) 更严重：缓存只在整批结束后写一次，超时/kill 会丢光本次已算的全部向量，叠加 (1) 的 300s 上限意味着**永远无法收敛**（反复重做同一段又反复丢失）——已改每 `SAVE_EVERY=512` chunk 增量存盘。**操作恢复法**：大型 wiki 首次批量嵌入，先直接跑 `build_embeddings.py --project <root> embed` 清空积压（无超时上限），再跑 `ingest.py`，Stage 3.7 命中热缓存秒级完成。

### snap_out 在表格密集书上曾产出异常极小 chunk（已修，2026-06-30）
`_stage_2_analyze.py::_stage_2_1_snap_out` 在窗口末端落在受保护 block（表格/代码块）内部时，把 chunk 结束点回退到该 block 起点。表格密集书（如控制系统教材的大 Laplace/Routh 表）中，一张早早开始的巨表会把 chunk 收缩成表格前的极小片段——**不丢数据**（下一 chunk 靠 overlap 重新覆盖该表格），但浪费一次 LLM 往返分析一个近空 chunk。修复：只有回退后仍留下有意义的 chunk（`r[0]-start >= attempted//2`）才回退，否则改为向前跳过整个 block。**操作纪律：chunker 边界逻辑只能在书与书之间改，绝不能在书摄入中途改**——chunk 分析按内容 hash 缓存，改边界会废掉当前在飞书的已完成 Stage 2.2/2.4 缓存。

### Stage 2.3 标题 Jaccard 去重漏判重音/标点变体（已修，2026-06-30）
Stage 2.3 的既有页关联/去重（`_stage_2_base.py::_stage_2_title_words`）只按 `[\s/]+` 切分+小写+去停用词，不折叠重音、不去标点。已存在页 "Thévenin's Theorem"（slug `Thevenins-Theorem`）因此漏配新生成的 "Thevenin's Theorem"：词集合 `{thévenin's, theorem}` vs `{thevenin's, theorem}` 交集只有 `{theorem}`（Jaccard 0.33，< 0.5 阈值），精确 slug 匹配也因撇号差异失败——**结果是生成了一个重复页**（该 wiki 此前已有 3 个历史重复变体，这次变成第 4 个）。修复：每个 token 先过 `unicodedata.normalize("NFKD", ...)` 折叠重音再去标点，才做 Jaccard 比较。**范围提醒**：此修复只防止未来的新重复；已存在的跨书历史重复 slug 变体是更大的、独立的内容去重课题，此修复不回溯清理。

### Stage 2.4 生成概念数上限按行数算，曾静默丢尾部概念（已修，2026-06-30）
`_stage_2_4_generation.py` 原按**行数**截断 GENERATE 列表（`concept_lines[:100]` 分chunk / `[:200]` 单发），但每个概念占约 4 行（标题+最多3条 key_detail），实际只放得下约 25-34 个概念；而可链接 slug 列表本身不受限。chunk 分析密度提升后（见 `conversation-mode-agent-workflow.md` 的 density_hint 技巧），密集 chunk 的尾部概念被静默从生成列表剔除、但仍留在可链接列表里——产生指向"从未生成"页面的死链。修复：上限大幅提高到远超密度目标（分chunk 480/160，单发 800/200）。**未来调密度的教训**：这类上限必须按概念数算（或留出 ≫ 密度×4 的余量），绝不能是纯行数截断。

### Stage 2.6 源页偶发缺失 authors/year/url/venue（已缓解，非彻底修复）
Stage 2.6 提示词预填了 NashSU parity 模板（frontmatter authors/year/url/venue + `## Book Summary`），但生成 agent 有时会自由发挥自己的格式（如加粗行内署名、metadata 表格）而非照抄模板，pipeline 又原样写盘不校验 source 页 frontmatter，字段就此缺失——**这是 agent 未遵循问题，不是代码 bug**。缓解：`_stage_2_6_source_page.py::_normalize_source_frontmatter()` 在 agent 响应之后、返回之前跑一遍：从已算出的 `*_meta` YAML 回填缺失的 authors/year/url/venue，并从该 chunk 刚生成的 concept/entity slug（取前 5 个）回填空的 `related: []`；源页已完整则空操作。**排查同类问题的手法**：对比归档的 `Stage-2-6-SourcePage-*.md`（提示词）与对应 `.txt`（响应），能区分"模板本身缺字段"（真 bug）还是"agent 没照做"（此类问题）。

### Stage 1.3「[待重试]」占位符从未被自动重试（已修，2026-07-06）
`_stage_1_3_pending_images()` 靠 `_stage_1_3_is_caption_failed()` 判断已有 `.caption.txt` 是否需要重跑；后者只认几个失败关键词子串（"解析失败"/"sorry"/"unable to" 等），完全没检查占位符自己的 `[待重试]` 前缀。占位符文本形如 `[待重试] 图片 X，尺寸 W×H — TimeoutError: timed out`——`{err}` 部分是任意异常信息，通常不含那几个关键词——于是占位符被当成一份正常缓存的 caption 永久跳过，`_stage_1_3_pending_images` 文档字符串里"失败会在下次运行重试"的承诺从未真正生效，用户必须手动删 `.caption.txt` 才能强制重跑。修复：`_stage_1_3_is_caption_failed()` 开头加 `text.startswith("[待重试]")` 直接判定为失败。

### `.digested` files in `raw/` subdirectories
旧 pipeline 标记。当前 pipeline（Stage 0.2）用 `wiki/sources/` 作唯一去重信号。清理见 `maintenance-cleanup.md`。

## Batch digest patterns
批量摄入 pitfalls 见 `batch-digest-loop.md`。一句话：直接从 Python 循环调 `ingest.py`（不走 `claude -p`），按 `wiki/sources/<stem>.md` 存在性去重，串行跑。
