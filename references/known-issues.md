# Known issues / bugs in `improved-wiki`

## Open issues

### Several files exceed the 800-line guideline
`_core.py`（~1032 行）。暂无明显自然切分点，未拆。
`ingest.py`（~462 行）协同 `_ingest_skip.py` / `_ingest_chunks.py` / `_ingest_prepare.py` / `_ingest_write.py` 四个编排子模块。
`_stage_1_extract.py` 是 facade，re-export 兄弟模块 `_stage_1_1_scanned.py` / `_stage_1_2_images.py` / `_stage_1_3_caption.py` 的公开名，外部导入者无需改动。
`_stage_2_4_generation.py`（~658 行）已低于阈值。

### minerU 偶尔把公式区域分类为 `image` 而非 `equation`
~112 公式图被当图片送 VLM，而非用 minerU 已提取的 LaTeX 文本（上游 minerU 版面分析问题）。

## Design decisions (not bugs)

### `ingest.py` 用 `urllib.request` 不用 `httpx`/`requests`
刻意避免 cron 语境下 `pip install`。

### 必须用 venv Python（系统 Python 缺 fitz + 版本太旧）
用 `~/.venv/bin/python3`。fitz（PyMuPDF）仍用于 Stage 1.1 的 garbled 检测采样。**且** stage 模块用 PEP 604 union 语法（`str | None`），需 Python 3.10+；macOS 系统 `/usr/bin/python3` 是 3.9，会抛 `TypeError: unsupported operand type(s) for |`——这是 #1 首次运行失败原因。

### 删除页面后 LanceDB 留残留向量，需重 embed
`build_embeddings.py embed` 是 `mode="overwrite"` 全量重建，全 skill 无增量删向量的 API。任何删除——`--delete`（源生命周期）和 lint `--delete-orphans`——都不清向量块，被删页可能在向量搜索里短暂命中（链接已失效），直到下次 `build_embeddings.py embed` 重建即清除。NashSU 用增量 `removePageEmbedding`；CLI 用整表重建达成同一端状态（无残留向量），刻意不移植增量删（YAGNI，且与重建式索引不符）。删除后想立即干净就重跑 embed。

### Wikilink enrichment merge loop after Stage 3.1
Stage 3.1 写盘后，pipeline 生成多个 `LLM-task-*.md` merge prompt（`.llm-wiki/conversation/<hash>/`），每个让 agent 把已有 wiki 页与新内容合并。re-run 时会重新发现并 re-merge。高效处理：用 `delegate_task` 批量；wikilink 建议 JSON 任务输出 `{}` 可安全跳过（Stage 2.4 已加内联 wikilink 时无质量损失）。

### Stage 2.1 只喂文本采样不是全文
Global Digest prompt 含书的文本采样（~200K chars），非全文。全文在 `.llm-wiki/extract-tmp/<stem>/p*.txt`，agent 需要时可读更多。

### OCR timeout for 200+ page books
minerU 50 页/chunk 串行。272 页书（6 chunks）可能超 600s 终端超时。**重跑 `ingest.py` 从缓存恢复**——已完成 chunk 跳过。`--stop-after-stage 0` 分离 OCR 与 LLM 阶段。

### `--delete` for re-ingest
`ingest.py --delete "raw/Book/<file>.pdf"` 删 source 页 + 孤儿 concepts/entities + media + cache，再重跑即可干净重摄。

## Legacy artifacts

### `.digested` files in `raw/` subdirectories
旧 pipeline 标记。当前 pipeline（Stage 0.2）用 `wiki/sources/` 作唯一去重信号。清理见 `maintenance-cleanup.md`。

## Batch digest patterns
批量摄入 pitfalls 见 `batch-digest-patterns.md`。一句话：直接从 Python 循环调 `ingest.py`（不走 `claude -p`），按 `wiki/sources/<stem>.md` 存在性去重，串行跑。
