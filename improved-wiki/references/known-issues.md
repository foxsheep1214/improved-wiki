# Known issues / bugs in `improved-wiki`

## Open issues

### Shell scripts lack `set -euo pipefail`
`run-queue.sh`、`wiki-lint.sh`、`wiki-monitor.sh` 应加严格错误处理。

### Several files exceed the 800-line guideline
`ingest.py`（~2062 行）、`_stage_2_4_generation.py`（~632 行）。

### `batch_size=6` hardcoded on minerU caption path
`_stage_1_extract.py` line 990 硬编码 `batch_size=6`，与 `CAPTION_BATCH_SIZE=8` env 默认不一致——6-图批次可能超 MiniMax token 限制导致 JSON 截断。

### minerU 偶尔把公式区域分类为 `image` 而非 `equation`
~112 公式图被当图片送 VLM，而非用 minerU 已提取的 LaTeX 文本（上游 minerU 版面分析问题）。

## Design decisions (not bugs)

### `ingest.py` 用 `urllib.request` 不用 `httpx`/`requests`
刻意避免 cron 语境下 `pip install`。

### 必须用 venv Python（系统 Python 缺 fitz + 版本太旧）
用 `~/.venv/bin/python3`。fitz（PyMuPDF）仍用于 Stage 1.1 的 garbled 检测采样。**且** stage 模块用 PEP 604 union 语法（`str | None`），需 Python 3.10+；macOS 系统 `/usr/bin/python3` 是 3.9，会抛 `TypeError: unsupported operand type(s) for |`——这是 #1 首次运行失败原因。

### Wikilink enrichment merge loop after Stage 3.1
Stage 3.1 写盘后，pipeline 生成多个 `LLM-task-*.md` merge prompt（`.llm-wiki/conversation/<hash>/`），每个让 agent 把已有 wiki 页与新内容合并。re-run 时会重新发现并 re-merge。高效处理：用 `delegate_task` 批量；wikilink 建议 JSON 任务输出 `{}` 可安全跳过（Stage 2.4 已加内联 wikilink 时无质量损失）。

### Stage 2.1 只喂文本采样不是全文
Global Digest prompt 含书的文本采样（~200K chars），非全文。全文在 `.llm-wiki/extract-tmp/<stem>/p*.txt`，agent 需要时可读更多。

### OCR timeout for 200+ page books
minerU 50 页/chunk 串行。272 页书（6 chunks）可能超 600s 终端超时。**重跑 `ingest.py` 从缓存恢复**——已完成 chunk 跳过。`--stop-after-stage 0` 分离 OCR 与 LLM 阶段。

### `--delete` for re-ingest
`ingest.py --delete "raw/Book/<file>.pdf"` 删 source 页 + 孤儿 concepts/entities + media + cache，再重跑即可干净重摄。

## 已修复（存档）

- **2026-06-24 无回退策略**：caption key 缺失/批次失败、embeddings 缺 stack、LLM page-merge 失败、config.json 解析失败 → 一律 raise 暂停（删占位符/array-merge/静默 env 回退）。详见 `ingest-stages-mandatory.md`。
- **2026-06-24 caption harvest**：`content_list` 是 JSON 字符串，`isinstance(cl, list)` 永远 False → 全量图倾倒。修：`json.loads(cl)` + minerU `image_caption` 写 sidecar。528→340 图，VLM 调用 ↓70%，caption 覆盖 62%→98%，Stage 2.1 输入 4K→200K chars。
- **2026-06-24 wikilink enrichment 嵌套链接 bug**：`_enrich_wikilinks.py` 对已有 `[[...]]` 内的子串二次包装。修：`_replace_first_outside_links` 只在非链接段替换。
- **2026-06-23 per-block 语言误报**：minerU skip-set 不全 + Greek 单字符误判 + Latin 单 token 误判。修：`method.startswith("mineru")` + Greek ≥2-letter word run + Latin ≥2 function words。
- **2026-06-22 `_is_image_too_small` NameError**：`MINERU_IMG_MIN_WIDTH/HEIGHT` 未定义被 try/except 吞。修：定义常量 + size check 移出 try/except。

## Legacy artifacts

### `.digested` files in `raw/` subdirectories
旧 pipeline 标记。当前 pipeline（Stage 0.2）用 `wiki/sources/` 作唯一去重信号。清理见 `maintenance-cleanup.md`。

## Batch digest patterns
批量摄入 pitfalls 见 `batch-digest-patterns.md`。一句话：直接从 Python 循环调 `ingest.py`（不走 `claude -p`），按 `wiki/sources/<stem>.md` 存在性去重，串行跑。
