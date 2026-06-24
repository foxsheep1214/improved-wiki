# Known issues / bugs in `improved-wiki`

## Open issues

### Several files exceed the 800-line guideline
`_core.py`（~1013 行）。暂无明显自然切分点，未拆。
`ingest.py` 已于 2026-06-24 拆分（→ `_ingest_skip.py` / `_ingest_chunks.py` / `_ingest_prepare.py` / `_ingest_write.py`，主文件 1407→462 行）。
`_stage_1_extract.py` 已于 2026-06-24 按子阶段拆为 facade + 三个兄弟模块（`_stage_1_1_scanned.py` 780 / `_stage_1_2_images.py` 478 / `_stage_1_3_caption.py` 416，facade 主文件 1904→403 行）。采用 facade 模式：外部导入者（ingest.py / _ingest_prepare.py / _stage_3_2_inject_images.py / _stage_validators.py / _stage_2_base.py）无需改动，facade re-export 全部原有公开名。
`_stage_2_4_generation.py`（~658 行）已低于阈值。

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

- **2026-06-24 wiki-monitor.sh 队列文件被状态文本覆盖（数据损坏）**：merge 阶段的 heredoc 把 stdout 重定向到 `$QUEUE_PATH.tmp` 再 `mv` 覆盖真实队列文件，但 Python 内部又直接 `open(queue_path, 'w')` 写了一遍 JSON——外层 mv 随后用只含 `print()` 状态行的文本覆盖掉刚写好的 JSON，每次 merge 后队列文件就被损坏成纯文本，导致 `run-queue.sh` 下次读队列时 JSONDecodeError。修：Python 内部改为 tmp+`os.replace` 原子写，去掉外层重定向。
- **2026-06-24 wiki-monitor.sh 队列去重 key 不匹配**：合并新文件时用裸 `rel`（如 `test.txt`）去匹配队列里的 `sourcePath`（如 `raw/test.txt`），永远不命中，导致同一文件在被 ingest.py 处理前每次 monitor 运行都会被重复加入队列。修：比较 `f"raw/{rel}"`。
- **2026-06-24 wiki-monitor.sh 首次运行 lock 文件目录不存在**：默认 runtime 目录（`.llm-wiki/`）只在 `.iwiki-runtime` 迁移分支里被 `mkdir -p`；全新项目第一次跑（无历史 runtime 目录）时 `touch "$LOCK_PATH"` 直接报错退出。修：sanity check 后统一 `mkdir -p "$RUNTIME"`。
- **2026-06-24 wiki-monitor.sh 死代码清理**：移除从未被调用的 `sha256_of`/`cached_hash` bash 函数（哈希实际在内嵌 Python 里算）、未使用的 `QUEUE_CONTENT`/`ADDED`/`SKIPPED`/`FAILED`/`TO_ADD` 占位变量、以及 `existing = {Path-like: i for i, Path-like in enumerate([])}` 这行语法上能跑但完全是垂悬占位符的字典推导式。
- **2026-06-24 `batch_size=6` 硬编码**：`_stage_1_extract.py` 的 mineru-extracted caption 调用改用 `CAPTION_BATCH_SIZE`（env 可配，默认 8），不再与模块默认值脱节。同时删除了同文件里重复定义的第二个 `CAPTION_BATCH_SIZE = int(os.environ.get(...))`（与第一个定义完全冗余）。
- **2026-06-24 shell 脚本 `set -euo pipefail` 复查**：`run-queue.sh`、`wiki-monitor.sh` 实际已具备严格错误处理（之前记录已过期）；`wiki-lint.sh` 仍只有 `set -uo pipefail`（缺 `-e`），但该脚本大量依赖"子步骤失败不应中断整体扫描"的语义（如 `--semantic` 子脚本失败时打印警告并继续），加 `-e` 风险大于收益，故保留现状，不强行加。
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
