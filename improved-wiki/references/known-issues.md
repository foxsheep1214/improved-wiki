# Known issues / bugs in `improved-wiki` (as of v0.1)

This file tracks known issues with the skill's scripts. Each entry is small enough to fix in a one-liner, but no one has gotten around to it yet. Fix via `patch` and update this file when you do.

---

## `ingest.py` — `detect_template_type` error message is too terse

**Status (2026-06-10)**: ✅ **PARTIALLY FIXED**. The detection logic now handles all 3 supported layouts:
- Layout A: `raw/<type>/<topic>/<file>` (improved-wiki default)
- Layout B: `raw/sources/<type>/<file>` (LLM Wiki app legacy)
- Layout C: `raw/sources/<file>` (flat — defaults to `book` with a warning)

But the **error message** still doesn't list recognized folders or suggest overrides. If the user puts a file in a non-standard folder (e.g. `raw/notes/`), the error is still cryptic.

**Original text** (kept for historical record):

> **Symptom**: When a file is in a folder the script doesn't recognize, the error is `ValueError: Unknown raw folder 'sources'. Add to FOLDER_TO_TEMPLATE or set IMPROVED_WIKI_TYPE.`
> 
> **Why this matters**: the LLM Wiki app (NashSU) uses a different layout — `raw/sources/book/X.pdf` instead of `raw/book/X.pdf`. If the user has both kinds of projects, this error fires constantly and the user has to google the FOLDER_TO_TEMPLATE mapping.
> 
> **One-line fix** (in `scripts/ingest.py`, the `detect_template_type` function): replace the `raise ValueError` block with one that:
> 1. Lists all recognized first-level folders
> 2. Suggests the closest match by substring
> 3. Suggests `--type` as an override
> 
> The actual replacement is in the conversation history (search for "patch ... `ingest.py` `detect_template_type`" — that patch was attempted but denied because the user has restricted write tools to `skill_manage` + `memory` only).
> 
> Workaround: pass `--type=book` (or whatever) explicitly when invoking the script on a file in an unrecognized folder.

---

## `ingest.py` — Generation step writes a placeholder, not the LLM's actual output

**Status (2026-06-10)**: ⚠️ **PARTIAL**. The parsing infrastructure works (`parse_file_blocks` extracts `### File N:` blocks correctly), but **in practice the LLM (MiniMax-M3, 16K max_tokens) does NOT emit `### File N:` blocks** — it produces only the YAML analysis. With 32K max_tokens, the API times out (600s HTTP timeout) before completing the generation.

**What actually happens**:
- Step 1 (Analysis): ✅ Works great. 1740-line YAML with book meta, 16-chapter outline, 31 concept slugs, 5 entity slugs, reading notes.
- Step 2 (Generation): ❌ LLM does not emit `### File N:` blocks. `parse_file_blocks` returns 0 blocks. Placeholder source page is written with the raw analysis YAML dump.

**Root cause**: 251K input chars + full analysis + source page + 31 concept pages + 5 entity pages exceeds what a single API call can produce within 600s (or within 16K output tokens).

**Workaround** (two options):
1. Accept Analysis-only: the placeholder source page already contains a high-quality YAML analysis. Generate concept/entity pages separately from that data.
2. Split Generation: after the Analysis call, make a separate LLM call that takes the analysis YAML and generates only the wiki source page body (without concept pages). Concept pages can be generated lazily on first query.

**Historical note**: this entry previously said "FIXED" because the parsing code was written, but the 2026-06-10 end-to-end test on HardwareWiki's 电源篇 (778 pages, 251K chars) revealed that the LLM simply doesn't execute the Generation step.

**Original text** (kept for historical record):

> **Symptom**: After a successful ingest, the source page contains a YAML dump of the analysis and a comment "Ingested but Generation step not yet implemented". The real wiki pages (concepts, entities) are NOT written.
> 
> **Why this matters**: the skill is half-finished. The Analysis step works (LLM produces structured YAML), but the Generation step (LLM producing the actual markdown file contents) is not wired up.
> 
> **What's missing**:
> - Parse the LLM's response for the `### File N: <path>` blocks
> - Each block's content should be written to the corresponding wiki path
> - The current `ingest_one` only writes a placeholder for the source page
> 
> **How to finish**: in the `call_llm_analysis` response, after parsing the YAML analysis, extract the markdown blocks (regex: `### File (\\d+): (.+?)\\n(.*?)(?=### File |\\Z)` with DOTALL). For each block, write the content to the path on the `### File N: <path>` line.
> 
> Until this is done, the skill is useful for the *hashing + cache + queue* plumbing, but not for actually generating wiki content.

---

## `scripts/wiki-monitor.sh` and `scripts/run-queue.sh` — referenced in SKILL.md but not yet written

**Status (2026-06-10)**: ✅ **FIXED**. Both scripts exist now. `wiki-monitor.sh` (cron entry, hash-diff monitor) and `run-queue.sh` (manual entry, queue processor).

**Implementation notes** (for future reference):
- `wiki-monitor.sh` uses `python3` to walk `raw/` safely (handles CJK paths), then `python3` again to merge new entries into the existing queue (atomic write via `tmp` + `rename`).
- `run-queue.sh` supports 3 modes: `--file <path>` (one-off), `--priority <path>` (prepend to queue), and default (process the queue in order). Stops on first failure unless `--continue-on-error` is set.
- Both scripts respect `IMPROVED_WIKI_ROOT` env var (default: `cwd`).
- Both scripts acquire a `wiki/.ingest-lock` file to prevent concurrent runs.

Original text (kept for historical record):

> **Symptom**: SKILL.md §14.1, §14.2, §14.6 reference these scripts, but they don't exist on disk.
> 
> **Why this matters**: the cron entry points to `wiki-monitor.sh` which doesn't exist, so the cron will fail silently.
> 
> **Workaround**: write them when you actually go to install the cron. The two scripts are ~50 lines each, in pure bash, and follow the pattern in SKILL.md §14.2.
> 
> The basic shape:
> 
> ```bash
> # wiki-monitor.sh
> WIKI_ROOT="${IMPROVED_WIKI_ROOT:-$PWD}"
> CACHE="$WIKI_ROOT/wiki/.ingest-cache.json"
> QUEUE="$WIKI_ROOT/wiki/.ingest-queue.json"
> RAW="$WIKI_ROOT/raw"
> 
> # For each file in raw/, check if hash is in cache
> # If not, add to queue
> # Then call run-queue.sh
> ```
> 
> ```bash
> # run-queue.sh
> # For each entry in queue:
> #   Call ingest.py
> #   Mark entry done in queue (or delete)
> ```

---

## `references/templates/` — the 4 anchor files were referenced before they were created

**Status**: ✅ Fixed in this session. The 4 anchor files (schema.md, index.md, log.md, overview.md) are now in `references/templates/` and can be copied to a new project's `wiki/` directory per `references/initial-setup.md` Scenario A step 2.

---

## Dry-run gives a wrong impression of what the LLM call will cost

**Symptom**: `--dry-run` prints the file's hash, template, and a "DRY RUN" line, but does NOT print the LLM prompt length or estimated token cost. So the user can't tell whether the upcoming call will be 30s or 30min.

**Fix**: in `main()`, after detecting the template, count the chars in the extracted text (need to call `extract_text` in dry-run mode) and print "Estimated LLM call: ~{N} chars in {N} chunks" or similar. This requires a `--dry-run-extract` mode that does the OCR but skips the LLM call.

---

## `scripts/ingest.py` — uses `urllib.request` not `httpx` / `requests`

**Not a bug, but a note**: the script deliberately uses stdlib `urllib.request` to avoid `pip install` in the cron context. This means no streaming, no fancy retry logic, no async. If you need to upgrade to `httpx` for any reason, that's a deliberate choice to revisit, not a "missing import" to fix.

---

## `ingest.py` — `update_index_log_overview` ValueError (FIXED 2026-06-10)

**Status**: ✅ **FIXED**. The function received `source_path` (wiki-side, e.g. `wiki/sources/X.md`) but called `source_path.relative_to(config.raw_root)` (raw is `raw/`). Crash: paths have different roots.

**Fix**: Added `raw_file: Path` parameter. Now computes `raw_rel` and `source_rel` independently.

---

## `ingest.py` — `wiki_path_for_source` wrong path with type subfolder (FIXED 2026-06-10)

**Status**: ✅ **FIXED** (2026-06-16: re-nested to mirror raw/). `wiki/sources/`, `wiki/media/`, `wiki/REVIEW/` now all mirror the `raw/` directory structure. Source-block matching updated to handle nested paths.

**Fix**: Changed to `config.wiki_dir / "sources" / raw_file.with_suffix(".md").name` — flat, filename stem only.

---

## `ingest.py` — `parse_yaml_block` crash on unescaped CJK quotes (FIXED 2026-06-10)

**Status**: ✅ **FIXED**. `yaml.safe_load` crashed on `"9.2 "正激"和"反激""` — Chinese curly quotes inside YAML strings. Fallback was only on `ImportError`, not parse failures.

**Fix**: Added `except Exception` catch, falling back to `parse_simple_yaml`.

---

## `ingest.py` — Must run with venv Python (system Python lacks PyMuPDF)

**Status**: **DOCUMENTED**. macOS `/usr/bin/python3` has no `fitz`. Use the skill venv (`~/.venv/bin/python3`) which has PyMuPDF + other deps installed.

## `ingest.py` — PyMuPDF extracted text leaks into `wiki/.extract-tmp/` (DESIGN GAP, 2026-06-10)

**Status (2026-06-10)**: ⚠️ **DESIGN HOLE**. SKILL.md §1 says `wiki/` should be "git-friendly, human-readable .md files" and `wiki/` is the "LLM-generated derivative" layer. But §14.1's table lists `wiki/.ingest-cache.json`, `wiki/.ingest-queue.json`, `wiki/.ingest-progress/` as if it's fine to put runtime state under `wiki/`. The actual script (PyMuPDF `extract_text()`) writes `<source-stem>-extracted.txt` into `wiki/.extract-tmp/`. **This contradicts §1's stated intent** and is inconsistent across the skill — some runtime state is documented, some isn't.

**Evidence (HardwareWiki 电源篇 2026-06-10)**:
```
wiki/.extract-tmp/电源篇-2024-王玉皞-extracted.txt  (636 KB, 11172 lines, 778-page PDF text dump)
```
- LLM Wiki App does **not** produce this file. App uses `media/<slug>/mineru/` for OCR artifacts, never writes a `-extracted.txt` next to wiki. **This file is improved-wiki's own runtime artifact, placed in the wrong directory.**
- A 636 KB raw text dump in `wiki/` is not "LLM-generated derivative content" — it's an OCR cache.
- A future script that walks `wiki/**` for md files will accidentally include the .txt (or have to filter it).
- If user runs `git add wiki/`, they'll commit 600+ KB of raw PDF text per book.

**Why this matters beyond the dir name**:
- The naming `<source-stem>-extracted.txt` is **specific to improved-wiki's `ingest.py`** — no App, no Lint tool, no LLM Wiki skill knows about it.
- The user noticed because they ran `ls wiki/` and saw an unexpected dir.

**Status (2026-06-16)**: ✅ **FIXED**. Runtime state unified to `.llm-wiki/` (NashSU-aligned). `.iwiki-runtime/` backward compat removed — `_paths.detect_runtime_dir()` auto-migrates any lingering `.iwiki-runtime/` contents to `.llm-wiki/` on first access. Detection order: `.llm-wiki/` → auto-migrate `.iwiki-runtime/` → `wiki/` (legacy).
**Status (2026-06-14)**: ✅ **FIXED**. `validate_ingest.py` now reads from cache `stages` summary instead of individual files; uses `_paths.detect_runtime_dir()` for runtime detection. Stage keys aligned with ingest.py actual output.

**Trigger for action**: any time `validate_ingest.py` is touched for another reason, fold in the env-var override.

## `extract_text()` (PyMuPDF) does NOT auto-fallback to OCR for scanned PDFs

**Status**: **DOCUMENTED AS PITFALL** (LLM Wiki App user will recognize this — same root cause, same symptom).

**Symptom**: A scanned PDF (no embedded text layer) gets ingested, but `wiki/sources/<name>.md` is empty or has only image refs. No useful content. This is **NOT a chunking problem** (chunker never even sees content) — it's an extraction problem.

**Root cause** (cross-checked against LLM Wiki App's `ingest.ts` 6/10):
1. App: PDFium local fallback → renders to PNGs but never OCRs them; `sourceContent` is nearly empty, LLM hallucinates or refuses. This is what produces "352 无说明 PNG + 6.5 KB .cache/.txt" (verified 6/10 on RadarWiki).
2. improved-wiki's `extract_text()`: same shape — uses PyMuPDF, no OCR fallback path. `sourceContent` empty, LLM gets nothing to digest.

**Fix**: in `extract_text()`, after PyMuPDF returns text, check `len(text) < <threshold>`. If so, fall back to minerU VLM (`mineru.ts` cloud API per `llm-wiki` skill). This is the same integration the App has.

**Threshold tuning**: a flat `len(text) < 1000` check will mis-fire on legitimate short docs (datasheets can be 500 chars). Better: `len(text) < pages × 50` (50 chars per page is bare minimum for a real text doc). Or check PyMuPDF's `page.get_text()` return per-page and count pages with empty text — if >50% of pages have empty text, it's a scanned PDF.

## macOS 文件名字符限制（`/` 无法在文件名中创建）⚠️ 已知限制

**Status**: **FIXED (2026-06-16)** — `_auto_correct_wiki_path()` in `ingest.py` now includes `stem = stem.replace('/', '_')` to handle macOS `/` in filenames.

**Symptom**: Stage 2 LLM 生成的概念页 slug 中包含 `/`（例如 `[[热仿真(Cauer/Foster模型)]]`），`_auto_correct_wiki_path()` 无法纠正，`os.rename()` / `open(path, "w")` 报 `FileNotFoundError`。macOS（及 Linux）文件系统不允许文件名中出现 `/`（它被解释为目录分隔符）。

**触发条件** (HardwareWiki 实测)：
- 源页 wikilink 引用格式 `[[名称(子分类/子名称)]]`，其中 `/` 在括号内也被 LLM 输出为 slug
- 中文书籍中较常见（如 `[[NPT/PT尺度法]]`、`[[热仿真(Cauer/Foster模型)]]`）

**影响范围**：1 个 wikilink 无法创建对应文件（HardwareWiki Lutz 2013）。其他书用 `_` 替换 `/` 后均可创建，但严格匹配的 wikilink 解析取决于 LLM Wiki app 的 slug 匹配策略（不区分大小写、`-`/`_`/空格 互换）。

**Workaround**：Stage 2 的 prompt 中已知会提示 `# Constraints - Use _ instead of / in slugs`，但 LLM 仍可能忽略。最可靠的修复是 `_auto_correct_wiki_path()` 新增一条替换规则：

```python
# 在 _auto_correct_wiki_path() 的 slug 变量赋值后添加
slug = slug.replace("/", "_")
```

但此修复仍有局限：`_` 变体创建的 wikilink 与源页中的 `[[热仿真(Cauer/Foster模型)]]`（含 `/`）并不完全一致，lint 可能报告 broken link。彻底解决需要在 lint 层面将 `/` 和 `_` 视为等价（类似于 lint.ts 已有的不区分大小写处理）。

**建议方案**：
1. **Short-term**：在 `_auto_correct_wiki_path()` 中将文件名中的 `/` 替换为 `_`（5 行 patch）
2. **Long-term**：lint 层将 `_`、`-`、`/` 在 slug 匹配中视为等价（需改 lint.ts / lint.py）

**相关**：`naming-conventions.md` §2.2 slug 规则未提及 macOS 的 `/` 限制，应补充。

---

## `wiki-lint.sh` 将 `lint-cache.json`/`lint-lock` 写入 `wiki/` 污染内容目录

**Symptom**: `wiki/` 根目录下出现 `lint-cache.json`、`lint-lock` 以及带空格名的过期副本（`lint-cache 2.json`、`lint-cache 3.json` 等）。运行时文件不应该出现在内容目录中。

**Root cause**: `wiki-lint.sh` 第 44-45 行硬编码 `LINT_CACHE="$WIKI_DIR/lint-cache.json"` 和 `LINT_LOCK="$WIKI_DIR/lint-lock"`，未使用 `$RUNTIME_DIR`。并发运行或 `mv` 原子写入中断时产生编号副本。与 `_paths.py` 形成鸡生蛋循环：lint-cache.json 在 wiki/ → `detect_runtime_dir()` 判定为 legacy 模式 → 所有运行时文件都往 wiki/ 写。

**Status (2026-06-16)**: ✅ **FIXED**.
- `wiki-lint.sh`: `LINT_CACHE`/`LINT_LOCK` 改为 `$RUNTIME_DIR/lint-cache.json`（`mkdir -p "$RUNTIME_DIR"` 确保目录存在）
- `_paths.py`: 新增 `_migrate_lint_cache_out_of_wiki()` — 检测到 wiki/ 下有 lint-cache.json/lint-lock 时自动迁移到 `.llm-wiki/`，同时清理带空格名的过期副本
- HardwareWiki 实际运行验证：2 个文件成功从 wiki/ → `.llm-wiki/`
