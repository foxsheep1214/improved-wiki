# Session lessons — improved-wiki ↔ NashSU LLM Wiki app cross-tool pitfalls

Accumulated 2026-06-11 from HardwareWiki + RadarWiki live debugging sessions. Each entry is something that bit us in this session and is not yet covered by other references/ files.

---

## 0. Stage 1.2 图片提取实现 (2026-06-11)

**背景**: improved-wiki 之前缺少 Stage 1.2 的实现。

**解决**: 新增 `scripts/extract_images.py`

**功能**:
- 用 PyMuPDF `get_images()` 提取嵌入图
- 保存到 `wiki/media/<slug>/p<N>-fig<K>.<ext>`
- 生成 `_manifest.json`（含页号/尺寸/SHA256）
- SHA-256 去重（同一图复用多次只存一份）

**使用**:
```bash
~/.hermes/skills/improved-wiki/scripts/extract_images.py raw/book/Book.pdf --project .
```

**产物**:
```
wiki/media/<slug>/
├── p0-fig1.png
├── p0-fig2.png
├── ...
└── _manifest.json
```

---

## 1. Delegate 模式实现 (2026-06-11)

**背景**: improved-wiki 的 ingest.py 需要调用 LLM API（MiniMax），但 agent 调用时希望用自己的 model 和 API key。

**解决**: 新增 `--delegate` 模式 + `--continue-from` 恢复

**关键改动**:
- Config 添加 `delegate_mode` 字段
- `call_llm_delegate()` 在 delegate 模式下返回任务描述
- 新增 `write_delegate_output()` 输出 JSON 任务到 stdout + `.delegate.json`
- 新增 `continue_from_result()` 处理 delegate 结果
- checkpoint_dir (默认 `.ingest-checkpoints/`) 替代 `.llm-wiki/`

**流程**:
```bash
# 1. 启动（返回任务 + checkpoint）
ingest.py raw/book/Book.pdf --delegate
# → exits 101, 输出 JSON + checkpoint.json

# 2. Agent 执行 LLM，保存结果
result.json = {"response": "..."}

# 3. 继续
ingest.py --continue-from <checkpoint> --result result.json
# → 循环直到完成（exit 0）
```

**退出码**:
- `0`: 完成
- `101`: 等待 delegate 执行
- `1+`: 错误

---

## 2. Conversation Mode 文档 (2026-06-11)

**背景**: 当前对话可以直接执行 LLM 任务，无需脚本/API key，需要文档说明如何执行。

**解决**: 新增 `references/conversation-mode.md`

**内容**:
- 与 script/delegate 模式的对比
- 各 Stage 的 prompt 模板
- 完整流程说明
- 分批处理 Chunk Analysis 的策略

**适用场景**:
- ✅ 单次消化一本书
- ✅ 需要人工调整每个阶段
- ✅ 需要验证 LLM 输出质量
- ❌ 批量消化多本书
- ❌ 需要无人值守的自动化流程

---

## 3. Cache Schema Drift 支持 (2026-06-11)

**背景**: improved-wiki 和 LLM Wiki app 使用不同的 cache 格式。

**改进**: continue_from_result 支持两种 cache 格式的读取：
- `filesWritten` (improved-wiki 格式)
- `filesWritten` (app 格式，驼峰)

**位置**: `continue_from_result()` 中读取 cache

---

## 4. Stage 3.2 Image Injection 支持 (2026-06-11)

**新增**: `scripts/inject_images_to_source.py`

**功能**: 读取 `wiki/media/<slug>/_manifest.json`，将图片信息注入到 source 页面的 `## Embedded Images` 段。

**用法**:
```bash
~/.hermes/skills/improved-wiki/scripts/inject_images_to_source.py \
  --project /path/to/wiki \
  --source-slug <slug>
```

---

## 0. Stage 1.2 图片提取实现 (2026-06-11)

**背景**: improved-wiki 之前缺少 Stage 1.2 的实现，只有 `validate-ingest.sh` 检查，但没有提取脚本。

**解决**: 新增 `scripts/extract_images.py`

**功能**:
- 用 PyMuPDF `page.get_images()` 提取嵌入图
- 保存到 `wiki/media/<slug>/p<N>-fig<K>.<ext>`
- 生成 `_manifest.json`（含页号、尺寸、SHA256）
- SHA-256 去重（同一图复用多次只存一份）
- 过滤小图（<100px，默认可配）

**使用**:
```bash
~/.hermes/skills/improved-wiki/scripts/extract_images.py raw/book/Book.pdf --project .
```

**产物**:
```
wiki/media/<slug>/
├── p0-fig1.png
├── p0-fig2.png
├── ...
└── _manifest.json
```

---

---

## 1. `ingest-cache.json` schema drift — `filesWritten: []` does NOT mean "no files written"

**Symptom**: After ingesting a book, the cache shows:
```json
{
  "book/<file>.pdf": {
    "sha256": "...",
    "ingested_at": "2026-06-11",
    "files_written": [],            ← empty list
    "stages_completed": [...],      ← looks plausible
    "stages_skipped_or_pending": [...6 items]   ← alarmingly long
  }
}
```
A naive auditor concludes the book was "ingested but no files were written." This is **wrong** — the real filesystem has 738 captioned images, 38 concept pages, 52 entity pages. The book fully worked.

**Root cause**: improved-wiki's `ingest.py` writes one cache shape; the NashSU LLM Wiki app reads a different shape. Both shapes use the key `filesWritten` (or `files_written`) but improved-wiki writes the JSON in its own format that the app may not parse back into the same field name.

**Diagnostic recipe** (run all three — never trust the cache alone):

```bash
PROJ="/Users/skyfend/Documents/知识库/<project>"

# 1. Count actual products on disk
echo "sources:  $(ls $PROJ/wiki/sources/*.md 2>/dev/null | wc -l)"
echo "concepts: $(ls $PROJ/wiki/concepts/*.md 2>/dev/null | wc -l)"
echo "entities: $(ls $PROJ/wiki/entities/*.md 2>/dev/null | wc -l)"

# 2. Count images + check caption coverage
img_count=$(find $PROJ/wiki/media/<slug> -maxdepth 1 -type f \( -name '*.jpg' -o -name '*.jpeg' -o -name '*.png' \) 2>/dev/null | wc -l)
cap_count=$(find $PROJ/wiki/media/<slug> -maxdepth 1 -name '*.caption.txt' 2>/dev/null | wc -l)
echo "media/<slug>: images=$img_count, captions=$cap_count"
# caption coverage should be 100% for completed ingests

# 3. Cross-check the per-page source list against raw/books
ls $PROJ/raw/book/ | wc -l    # raw count
ls $PROJ/wiki/sources/*.md 2>/dev/null | wc -l   # sources count
# mismatch ≠ "missing" — could be hash drift, could be app-side cache confusion
```

**Lesson**: Treat `ingest-cache.json` as **one of four** state files to cross-check (the others being `ingest-queue.json`, `file-snapshot.json`, and `lancedb/`). Never trust a single file's claim of completion.

---

## 2. The "skill owns scripts" principle

**Established**: 2026-06-11, in response to agent suggesting RadarWiki needed its own `scripts/`. The user pushed back: "radarwiki 里面当然没有项目级的 scripts/，这些都应该写在 improved wiki 技能里面."

**The principle** (encoded in SKILL.md):
- `scripts/validate-ingest.sh`, `scripts/wiki-lint.sh`, `scripts/caption_*.py`, `scripts/ingest.py` all ship with this skill.
- Wiki projects **invoke** these via `~/.hermes/skills/improved-wiki/scripts/<name>` paths.
- Projects **do not fork** these scripts into their own tree. If a project's wiki/methodology/ records per-project VLM choice or batch size, that's a **decision**, not a script.
- A project that lacks `scripts/validate-ingest.sh` is not "missing infrastructure" — it's expected; the script lives in the skill.

**Anti-pattern to avoid**: agent reads a project's bare `wiki/` layout and proposes to scaffold `scripts/`. Wrong direction. Instead, point at `~/.hermes/skills/improved-wiki/scripts/validate-ingest.sh` and run it from the project root.

---

## 3. `validate-ingest.sh <slug>` smoke-test pattern with a non-existent slug

**Use case**: Want a fast read on whether a project's ingestion is healthy, without knowing which slug the project uses.

**Trick**: Run with a slug that **doesn't exist** (e.g. `validate-ingest.sh "电源篇"` on RadarWiki). Output gives you 4 ✅ / 10 ❌ where the failures split into two categories:

| Failure class | Interpretation |
|---|---|
| Stage 1.1 / 0.5 / 0.6 / 1 / 1.5 / 2 missing artifacts | **Slug doesn't exist** — these stages are per-slug by definition |
| Stage 2.3.5 / 3.5 / 3.7 / 4 / 5 fails on real slug-named state | **Project-level problem** — affects every slug in the project |

This is a useful first probe because it surfaces project-wide gaps (e.g. `review.json` schema invalid, `review-suggestions.json` missing, sources 1:1 coverage gap) without needing to know which books the project has.

**Caveat**: A slug that exists but didn't digest will produce indistinguishable output from a non-existent slug. Always grep `wiki/sources/` and `raw/` to confirm what slugs are real before deciding what the failures mean.

The mineru skill gotcha #35 documents the same pattern from the mineru side.

---

## 4. Per-source-slug validation vs project-wide audit

Two distinct uses of `validate-ingest.sh`:

**Per-source-slug validation** (most common):
```bash
cd <project> && validate-ingest.sh "硬件十万个为什么 电源是怎样炼成的 - 2024 - 王玉皞"
# All 15 stages checked against artifacts under .extract-tmp/<slug>/ and wiki/media/<slug>/
```

**Project-wide audit** — `validate-ingest.sh` is **not** designed for this. It takes one slug at a time. For a project-wide audit, write a wrapper that:
1. Lists all `<slug>` under `wiki/sources/`
2. Runs validate-ingest.sh per slug, captures exit code
3. Reports aggregate pass/fail + per-stage failure count across all slugs
4. Separately checks project-level state (`ingest-cache.json` schema, `review.json` validity, `lancedb/` existence, `wiki/{index,log,overview}.md` presence)

The wrapper is **project-specific by design** because the project knows its own slug list and aggregation policy. It still invokes `validate-ingest.sh` from this skill — does not redeploy it.

---

## 5. The "9 path" project: when sources < raw

Observed in RadarWiki: `raw_count=105, sources_count=46`. Two natural explanations:

| Cause | Diagnostic |
|---|---|
| Cache schema confusion (see §1) | Check `ingest-cache.json` schema variant; verify on-disk products are real |
| App-level pending/retry backlog | Check `ingest-queue.json` and `ingest-progress/` for in-flight books; some raw files may be mid-digest or queued for retry |
| macOS metadata drift | `autoIngest: false` in `app-state.json` sourceWatchConfig — disable to stop the watcher re-queueing on FinderInfo changes |

Don't assume the gap means "the digest failed." Cross-check the queue + progress + cache before declaring a book missing.

---

## 6. Quick reference: where state lives

For any debugging session on a wiki project, the four files you will check most often:

| File | Role |
|---|---|
| `.llm-wiki/ingest-cache.json` | "What was completed" — but see §1 |
| `.llm-wiki/ingest-queue.json` | "What's pending / failed / processing" |
| `.llm-wiki/ingest-progress/<book>.json` | "What's mid-flight" (real liveness signal) |
| `.llm-wiki/file-snapshot.json` | "What the watcher last saw on disk" |

When the queue file itself is corrupt (truncated write → `Extra data: line 92 column 2`), the recovery is to recover valid `{...}` objects via brace-depth scanning and write back, but **only after backing up** to `~/.hermes/backups/llm-wiki-queue/<ts>/`. The LLM Wiki app will overwrite your repair on shutdown if the in-memory state disagrees — restart-then-write is safer than write-while-running.

---

## 7. When `caption_md.py` reports a path it can't load

If `caption_md.py --md X.md --images-dir Y/ ...` fails with `HFValidationError: Repo id must be in the form 'repo_name'`, the cause is **almost always**: the script's default model-path lookup points at a modelscope cache directory that doesn't exist on this machine. Specifically `~/.cache/modelscope/hub/models/OpenDataLab/MinerU2___5-Pro-2605-1___2B/` (triple-underscore variant) — but the actual model is in `~/.cache/mineru-vlm/MinerU2.5-Pro-2605-1.2B/` per `~/mineru.json`.

**Fix**: pass `--model-path /Users/skyfend/.cache/mineru-vlm/MinerU2.5-Pro-2605-1.2B` explicitly. This is also the path `MINERU_MODEL_SOURCE=local` mode uses. See `references/scripting-pitfalls.md` (mineru skill) for the full env-var matrix.

---

## 8. HardwareWiki 2026-06-11 ingest state snapshot (baseline for future audits)

Reference numbers captured during this session, useful for "did something regress?" comparisons:

- 3 books in `raw/book/`: 电源是怎样炼成的 (15MB), 开发流程篇 (26MB), 无源器件篇 (123MB)
- All 3 marked `ingested_at=2026-06-11` in `ingest-cache.json`
- Wiki products: 1 source page (only 电源篇), 38 concept pages, 52 entity pages, 1477 files in `media/电源篇/` (738 images + 738 captions + 1 manifest)
- 0 entries in `ingest-progress/` (no in-flight work at snapshot time)
- `review-suggestions.json` present, generated at 2026-06-11 16:57:38 by MiniMax-M3, stop_reason=end_turn
- `wiki/methodology/ingest-decisions.md` (4.3KB) present — per-project VLM/batch decisions exist
- Validate script smoke test (slug="电源篇"): 4 ✅ / 10 ❌. Real project gaps surfaced: Stage 3.3 review-suggestions missing per-book, Stage 3.2 source pages lack `## Embedded Images` section, Stage 3.1.7 raw=105 > sources=46, Stage 3.5 review.json schema invalid.

---

## 9. Long-running ingest pipelines need stage-by-stage authorization, not fire-and-forget

The user's explicit preference (captured 2026-06-10 bjz part1/part2 pattern, restated 2026-06-11 during RadarWiki ingest check): multi-stage background tasks must pause at each natural resync point, report progress, and wait for an explicit "go" before starting the next stage. **Do not chain stage N+1 automatically when stage N completes via `notify_on_complete`.**

Practical implications when invoking `ingest.py` or `caption_batch.py` on a long book:
- The LLM Wiki app's automatic in-memory queue is fine for within-stage retries (one chunk failing → next chunk). It is **not** fine for cross-stage transitions. Don't write orchestrators that fire `validate-ingest.sh` automatically when `ingest.py` returns.
- For a 25-Part / 8.7-hour batch, treat each Part (24 min of VLM work) as a synchronization point. The agent should report Part N complete → wait for user "go" → start Part N+1.
- The LLM Wiki app's own ingestion is **not** subject to this constraint — it manages its own retry. The constraint applies to **agent-orchestrated** multi-stage workflows (split → OCR per part → re-ingest per part).

This is the same preference recorded in user-profile memory; encoding it here so the next agent who reads this skill picks it up before reading memory.

---

## 10. Status-reporting discipline for ingest tasks

When reporting "ingest done" or "validate passed", report **both** dimensions:
1. **Engineering progress**: did the stages execute? did validate-ingest.sh print ✅?
2. **Goal achievement**: does the wiki now actually contain useful, retrievable content for the user?

❌ Anti-pattern: "Stage 5 cache written, 15/15 ✅" — that says nothing about whether the user can find anything. HardwareWiki 2026-06-11 state was exactly this: 15-stage validation would print mostly green while the source pages had no `## Embedded Images` injection (Stage 3.2) and the actual digest text didn't help users find anything.

✅ Pattern: "3 books fully digested (738 captioned images, 38 concepts, 52 entities in 电源篇), all with caption coverage 100%; 2 books in raw/ (开发流程篇 / 无源器件篇) lack source pages — Stage 3.1 fallback didn't trigger for those." — engineering + goal in one breath.

When validating an ingest, always cross-check **at least one** user-visible artifact (a concept page contains expected terms, a source page links to embedded images, a search returns the book). validate-ingest.sh's per-stage checks are necessary but not sufficient.

---

## 11. Read the skill before reflecting — schema drift is documented, agent often forgets

**Established**: 2026-06-11, in response to a session that walked straight into the §1 cache-schema pitfall despite it being documented in this very file's SKILL.md ("Known cross-tool schema drift" section). The agent treated `ingest-cache.json` `stages_completed: [...6 items]` + `files_written: []` as evidence that 3 HardwareWiki books had "no real output," then walked back when product-side audit showed 1 of 3 actually had 738 captioned images.

**The principle** (extends §1 from "documented but ignored" to "before reflection, read"):
- **Before claiming a system is X, read the skill's documented position on X first.** This file + `references/ingest-stages-mandatory.md` + `references/known-issues.md` cover the common failure modes. If the skill says "cache is unreliable, cross-check filesystem" and the agent is about to conclude from cache alone → stop, run the cross-check.
- **Skill-loaded cache invalidation**: `skill_view` sometimes returns `__SKIP__` for SKILL.md content (observed at session start 2026-06-11 on HardwareWiki check). When this happens, **don't trust it as "the skill is empty"** — re-fetch with a specific `file_path` (e.g. `references/session-lessons.md`) or read the directory listing of `references/` to see what's there. A `__SKIP__` SKILL.md does not mean "no content."
- **Memory hygiene before action**: when an agent is about to make a decision based on a piece of memory (e.g. "use MinerU local first because memory says 'HardwareWiki caption 优先本地 MinerU VLM'"), **check whether that memory is still on the active list** by reading `~/.hermes/memories/USER.md` and `~/.hermes/memories/MEMORY.md` directly. If the memory was deleted or amended in a prior turn (as the user did 2026-06-11 with the HardwareWiki caption entry), the system prompt's snapshot is stale and the agent will make decisions on outdated facts. The user explicitly framed this as "**这3 条 memory 影响工作了**" — i.e. trust-but-verify the active memory set, not just the most-recent user message.

**Anti-pattern to avoid**: agent reads `ingest-cache.json`, sees plausible-looking entries, drafts a 3-paragraph conclusion based on cache alone, ships it. User has to push back "**cache entry ≠ 产物**" — this is the same lesson as §1, but stated as a *behavioral* rule (read first, conclude second), not just a diagnostic recipe.

**Practical pre-flight checklist before any wiki-state claim**:
1. `skill_view` this skill (or its relevant `references/` file) — check if §1 / §5 / §10 already address the question.
2. `ls ~/.hermes/memories/` — check memory is still the version you think it is.
3. Cross-check filesystem (sources/, concepts/, entities/, media/) before trusting cache.
4. Only then draft the conclusion.

This pattern extends the "audit actual state before launching" rule from `references/known-issues.md` gotcha #30 (RadarWiki 5-book recovery) into the agent's reasoning layer, not just the pipeline-launch layer.

---

## 12. Scanned-PDF branch: when `PyMuPDF.get_text()` returns 0, the default Stage 1.1/0.5 don't apply

**Symptom (2026-06-11, HardwareWiki 无源器件篇 123 MB)**: Standard pre-flight returns:
```
总页数: 312
每页 get_text() 长度: 全 0
每页 get_images(): 恰好 1 张嵌入图 (== 整页扫描图)
```
The default Stage 1.1 path (`PyMuPDF.get_text()` → text-layer extraction) produces nothing useful. The default Stage 1.2 path (`PyMuPDF.get_images()` → embedded image extraction) returns exactly 1 image per page — that's the entire page scanned as an image, not a captioned figure.

**Diagnostic recipe** (run BEFORE deciding Stage 1.1 backend):
```python
import fitz
doc = fitz.open(pdf_path)
zero_pages = sum(1 for i in range(len(doc)) if len(doc[i].get_text()) == 0)
img_per_page = [len(doc[i].get_images(full=True)) for i in range(len(doc))]
print(f"zero-text pages: {zero_pages}/{len(doc)}")
print(f"avg images per page: {sum(img_per_page)/len(img_per_page):.1f}")
# If zero_pages / total > 0.5 AND avg images per page ≈ 1 → scanned PDF, switch branches
```

**The scanned-PDF branch** (apply if diagnostic above triggers):
| Default stage | Scanned-PDF substitute |
|---|---|
| Stage 1.1: `PyMuPDF.get_text()` → text file | PyMuPDF render page as 150 DPI PNG → OCR via chosen backend |
| Stage 1.2: `PyMuPDF.get_images()` → per-figure extract | Whole-page PNG IS the figure unit. Captioning covers page-level content (mixes formula/figure/text/table). |
| Stage 1.3: caption each extracted figure | Caption each rendered page (full-page "figure") |

**Lesson**: `improved-wiki/scripts/ingest.py` and `caption_sample_test.py` assume the default text-layer + per-figure branch. They will silently produce empty `wiki/sources/` on scanned PDFs. The scan-detect step above must run first; if it triggers, switch to the scanned-PDF pipeline (manual orchestration, no ingest.py shortcut).

The `references/scanned-pdf-to-llm-wiki-recovery.md` and `references/scanned-pdf-batch-recipe.md` cover the bulk-OCR workflow on scanned books (80-page Parts, 5-page pilot gate). They were built for RadarWiki 5-book recovery; the same workflow applies here, just with a different OCR backend — the user's preference on this Mac is MiniMax CN via mmx CLI, not local MinerU VLM (see §15).

---

## 13. Blank-page detection BEFORE diagnosing "VLM hallucination"

**Symptom (2026-06-11, HardwareWiki 无源器件篇 pilot p4)**: mmx vision takes **73.7 seconds** on page 4 and produces 2018 chars of hallucinated Japanese (`言いあう` repeated 30+ times). Initial diagnosis: "VLM collapsed into a foreign-language fallback loop." **Wrong** — p4 was a blank page (avg=254.7, 100% pixels in 200-255 luminance range, 0 dark pixels). The model wasn't hallucinating; it had no real content to extract, and the OCR prompt's "extract ALL text verbatim" constraint forced it into the cheapest fallback: non-Latin token emission.

**Diagnostic recipe** (run on suspicious low-quality OCR output):
```python
import fitz
from PIL import Image
doc = fitz.open(pdf_path)
for i in range(len(doc)):
    pix = doc[i].get_pixmap(dpi=72)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    gray = img.convert("L")
    hist = gray.histogram()
    avg = sum(v*c for v,c in enumerate(hist)) / sum(hist)
    # avg > 250 → blank page (verified bimodal distribution: 4 blank, 308 content, no overlap)
    if avg > 250:
        print(f"p{i+1}: BLANK (avg={avg:.1f})")
```

**Why threshold 250 works** (verified on 312-page hardware book, 2026-06-11):
| avg range | count | interpretation |
|---|---|---|
| `>=250` | 4 pages | Blank (min 250.2, max 254.7) |
| `245-250` | 55 pages | Sparse content (mostly table-of-contents, section dividers) |
| `240-245` | 167 pages | Normal text pages |
| `<240` | 86 pages | High-density content (figures, tables, formulas) |

There is a **5.1-unit gap** between the lowest blank page (250.2) and the highest sparse page (249.7). The threshold is naturally bimodal — no risk of misclassifying.

**Skip rule**: any page with `avg >= 250` should be excluded from OCR. Do NOT feed blank pages to mmx vision / MinerU VLM — they will burn 60-90 seconds generating junk tokens.

**Lesson**: Before diagnosing "the model hallucinated," check whether the input had anything to hallucinate about. PyMuPDF brightness is cheap (6.5 sec for 312 pages at 72 DPI) and answers the question definitively. The 5-second check belongs in any scanned-PDF pre-flight.

---

## 14. mmx CLI vs `anthropic/v1/messages` direct HTTP — two backends, different capabilities

**Pitfall (refined 2026-06-12)**: Earlier versions of this entry claimed "mmx CLI does NOT batch — use HTTP direct for batching." That's half right and missed the actual gotcha: the **endpoint URL** is what differs, not the calling style.

| Backend | Endpoint | Single-image | Multi-image batch | Auth header |
|---|---|---|---|---|
| `mmx vision describe` (CLI) | `v1/coding_plan/vlm` | ✅ | ❌ (one per call) | mmx adds internally |
| `requests.post` direct HTTP | `anthropic/v1/messages` | ✅ | ✅ (N images in one content block) | `Authorization: Bearer <key>` |

**mmx CLI quirk**: `mmx --verbose` displays `Auth: sk-cp-9f...` but this is **not the literal header name** — it's mmx's truncated display. The actual header that works for both endpoints is `Authorization: Bearer <key>`. Do NOT pass `Auth:` directly when calling HTTP — use `Authorization: Bearer`.

**Common error mode**: agent runs `curl` with `X-Api-Key: <key>` (the Anthropic-standard header) and gets `{"status_code":1004,"status_msg":"login fail: Please carry the API secret key in the 'Authorization' field"}`. The endpoint requires `Authorization: Bearer`, not `X-Api-Key`. The error message mentions "Authorization" but the verbose output of mmx shows `Auth:` — easy to misinterpret.

**Verified (2026-06-11)**: 5-image batch OCR via `anthropic/v1/messages` = 23.5s; same 5 images sequential via `mmx vision describe` = 85s. 3.6× speedup. Output: `stop_reason: end_turn`, input_tokens=10505, output_tokens=2727, prompt cache auto-reuses common prefix (`cache_read_input_tokens: 114`).

**Working payload shape** for multi-image batch:
```python
content = [{"type": "text", "text": prompt}]
for img_path in pages:
    b64 = base64.standard_b64encode(img_path.read_bytes()).decode()
    content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}})
payload = {
    "model": "MiniMax-M3",
    "max_tokens": 12000,  # OCR needs higher than caption (1500)
    "messages": [{"role": "user", "content": content}],
    "temperature": 0.1,
}
r = requests.post(
    "https://api.minimaxi.com/anthropic/v1/messages",
    json=payload,
    headers={"Authorization": f"Bearer {api_key}", "anthropic-version": "2023-06-01"},
    timeout=(30, 240),  # (connect, read) — read timeout is the one that bites on hung streams
)
```

**Pilot/measurement rule**: A 5-page pilot that runs on **one** backend doesn't generalize to the other. State which backend the pilot ran on when reporting numbers.

**Decision matrix** (which backend for which workload):
| Workload | Use |
|---|---|
| `< 30` images, ad-hoc | `mmx vision describe` (CLI is simpler; no script needed) |
| `30+` images, repeatable | `requests.post` to `anthropic/v1/messages` with multi-image content blocks |
| 1-5 pages for "5-20 sample test" pilot | CLI is fine — sample size is small, pilot focus is quality not speed |

---

## 15. User-mandated backend override — record the deviation, don't argue the default

**Established**: 2026-06-11, HardwareWiki 无源器件篇 OCR. Skill default for Stage 1.1 OCR is "MinerU VLM (local vlm-auto-engine)" per `references/scanned-pdf-to-llm-wiki-recovery.md`. User explicitly said: "**不同意，stage0 ocr也走minimax**". The agent offered the default; the user overrode.

**The principle** (generalizes beyond OCR):
- **Skill defaults are recommendations, not contracts.** They exist to spare the user from having to re-explain a 15-stage pipeline every session. They do not exist to override user choice.
- **When the user mandates a specific backend/tool/path**, the agent's job is to:
  1. **Honor the override immediately** — don't ask "are you sure?" or re-pitch the default
  2. **Record the deviation** in `wiki/methodology/ingest-decisions.md` (per-project) so the next auditor sees it
  3. **Note the override in this skill's session-lessons** so the next session doesn't waste a turn re-suggesting the default
- **Don't conflate "skill default" with "what the skill expects."** The skill's `ingest-stages-mandatory.md` §0 says "走 MinerU VLM OCR" but this is a default recommendation that the user can override per-project. The contract is the 15-Stage list, not the per-stage backend choice.
- **When the user's override contradicts a memory entry**, check whether the memory is still active (see §11). On 2026-06-11, USER.md had `"caption 优先本地 MinerU VLM（零 API），难图 MiniMax"` and `"多模态长任务先问'有无批量 API'（Anthropic messages 单请求多图, Message Batches 50% 折扣）"` — both contradicted the user's live directive. The user explicitly framed this as `"这3 条 memory 影响工作了"`, meaning: don't trust memory that contradicts current context.

**Anti-pattern to avoid**: agent loads the skill, sees Stage 1.1 default = MinerU, starts planning around MinerU. User says "use MiniMax." Agent responds with "but MinerU is faster locally, are you sure?" — that's arguing with the user about their own infrastructure. Wrong. Honor override, note deviation, move on.

**Decision recording template** for `wiki/methodology/<source>-decisions.md`:
```markdown
## Stage 1.1 (OCR) backend: mmx CLI (user override of skill default)
**User directive**: "不同意，stage0 ocr也走minimax" (2026-06-11)
**Skill default was**: MinerU vlm-auto-engine (local)
**Reason for override**: (user's reason if given; otherwise "user preference, no further justification needed")
**Impact**: +N hours of API latency vs local VLM; -1 dependency on local MPS memory budget
```

The impact line matters for the next agent who sees the deviation and wonders whether to "correct" it.

---

## 16. mmx CLI on the user's Mac — auth + region + base_url canonical invocation

**Verified working invocation** (2026-06-11, HardwareWiki 无源器件篇 pilot):
```bash
mmx vision describe \
  --image /path/to/page.png \
  --prompt "OCR prompt text here" \
  --region cn \
  --output text \
  --timeout 180
```

| Flag | Why |
|---|---|
| `--region cn` | Routes to `api.minimaxi.com` (Chinese endpoint); without it, falls back to global which has higher latency + different rate limits on this Mac |
| `--output text` | Suppresses JSON wrapper; easier to pipe into `.txt` files. Default is JSON |
| `--timeout 180` | 180s ceiling for individual image. Without it, default is 300s which masks network hangs. Set to 3× expected per-image time |

**Auth**: stored in `~/.config/mmx/config.json` (api-key from `mmx auth status`). No env var needed; the CLI handles it.

**Output cleanup**: raw `mmx` output contains `--output text` plain text + newlines; strip leading/trailing whitespace before writing to `.txt`. Don't try to parse JSON without `--output text` (default JSON wrapper adds noise).

**Rate limits on CN endpoint**: empirically observed ~24 sec/image on this Mac. Concurrent `mmx` calls hit 429 quickly; stay sequential unless user explicitly authorizes parallelism.

**Lesson**: when invoking mmx CLI from a Python loop or shell script, pin `--region cn` and `--output text`. The defaults work but add avoidable latency + parsing complexity.

---

## 17. Heuristic thresholds need data-driven justification, not "trust me"

**Symptom (2026-06-11, HardwareWiki 无源器件篇)**: I introduced `avg > 250` as a blank-page threshold without justifying the number. User asked: "avg>250 是什么意思？" — the right reaction was to immediately validate the threshold against the actual page distribution.

**Anti-pattern**: Pick a threshold that "feels right" (e.g. 240, 245, 250) and start using it. User has to ask why, agent has to scramble to verify.

**Pattern**: When introducing any heuristic threshold — brightness, page count, batch size, time budget — **justify it with one of**:
1. **Data distribution**: "I scanned 312 pages; 4 are blank (avg≥250), 308 have content (avg≤249.7). The 5.1-unit gap means threshold 250 has zero misclassification risk."
2. **Documented standard**: "PDF spec defines blank page as X" / "Industry convention for OCR confidence is Y"
3. **Empirical benchmark**: "Tested on 100 sample images, threshold X catches 99% of cases with 1% false positive"

If you can't do any of those three, **you don't yet have a threshold; you have a guess.** Mark it explicitly: "this is a guess pending validation" — and run the validation.

**Lesson for skill authoring**: this rule applies equally to skill defaults. The Stage 1.3 sample-test gate of "5-20 images" in `ingest-stages-mandatory.md` is a guess that worked; the script doesn't justify the range. If you want a tighter guideline, sample empirically — don't paper over the guess with confident prose.

---

## 18. PyYAML `safe_load` rejects `chunk_plan:` with mixed map-key + list-items

**Symptom (2026-06-12, HardwareWiki 无源器件篇 Phase 1)**: After the LLM returns a global-digest YAML, parsing fails with:
```
yaml.parser.ParserError: while parsing a block mapping
  in "<unicode string>", line 298, column 3:
      estimated_total_chunks: 4
      ^
expected <block end>, but found '-'
  in "<unicode string>", line 299, column 3:
      - chunk: 1
      ^
```

The LLM naturally emits this shape:
```yaml
chunk_plan:
  estimated_total_chunks: 4
  - chunk: 1
    parts: [1]
    chapters: ["电阻篇"]
    ...
  - chunk: 2
    ...
```

This is **invalid YAML 1.1 strict**. A mapping node cannot have both scalar keys (`estimated_total_chunks: 4`) and list items (`- chunk: 1`) as direct children — they have to be in distinct sub-keys.

**Fix (regex, ~10 lines)**: nest the list under a `chunks:` sub-key:
```python
import re, yaml
fixed = re.sub(
    r"(estimated_total_chunks:\s*\d+)\n((?:  - chunk:.*\n(?:    .*\n)*)+)",
    r"\1\n  chunks:\n\2",
    yaml_text,
)
data = yaml.safe_load(fixed)
```

Result parses cleanly:
```python
{'chunk_plan': {
    'estimated_total_chunks': 4,
    'chunks': [
        {'chunk': 1, 'parts': [1], 'chapters': ['电阻篇'], ...},
        {'chunk': 2, ...},
    ]
}}
```

**Why LLMs emit this shape**: when given the prompt template `chunk_plan: { estimated_total_chunks: N, - chunk: 1, ... }`, the LLM doesn't notice the structural mismatch and produces natural prose-as-structure. The chunked field needs to be wrapped.

**Two ways to prevent at the prompt level**:
1. Wrap explicitly in the prompt: `chunk_plan: { estimated_total_chunks: N, chunks: [ { chunk: 1, ... }, ... ] }` — but LLMs still sometimes flatten.
2. Defensively fix in `parse_yaml_block` (in `scripts/ingest.py` line 263) — the same regex above. This is the durable fix because (1) doesn't always work.

**Lesson**: When ingesting Phase 1 output via `parse_yaml_block`, always check for `chunk_plan` shape and normalize. Otherwise the failure happens **after** the LLM call succeeded — wasted ~60s API time + ambiguous error pointing at line 298 with no hint about the actual cause.

## 19. retry pattern that survives minimax's transient 5xx storm

**Symptom (2026-06-12, HardwareWiki 无源器件篇 OCR batch run)**: First ingest script only retried on HTTP 529 (`if "529" in err_msg or "overload" in err_msg.lower()`). Result: 22/30 batches failed because the cluster was returning HTTP 500 / 520 in addition to 529. The single retry on the only-529 path failed; the remaining22 batches were marked `status: fail` and 22 pages permanently lost from the OCR set.

**Root cause**: minimax CN cluster on this Mac during 2026-06-11 evening returned a mix of:
- `HTTP 500` with `{"type":"api_error","message":"unknown error, 500 (1000)"}` — transient
- `HTTP 520` with same shape — transient
- `HTTP 529` with `overloaded_error` — transient
- `ReadTimeoutError` after 240s — transient
- `ConnectError` / `ChunkedEncodingError` — transient

All are transient but the original script only retried 529. The retry condition was **too narrow**.

**The durable retry pattern** (proven 62/62 batch success after applying it):
```python
class BatchError(Exception):
    def __init__(self, kind, detail):
        self.kind = kind  # 'timeout' | 'network' | 'api_529' | 'http_500' | 'http_429' | 'unexpected'
        self.detail = detail

# In the API caller:
try:
    r = requests.post(ENDPOINT, json=payload, headers={...}, timeout=(30, 240))
except requests.exceptions.ReadTimeout:
    raise BatchError("timeout", f"ReadTimeout after 240s")
except (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError,
         requests.exceptions.ChunkedEncodingError) as e:
    raise BatchError("network", f"{type(e).__name__}: {e}")
except requests.exceptions.RequestException as e:
    raise BatchError("network", f"{type(e).__name__}: {e}")
if r.status_code == 429:
    raise BatchError("http_429", ...)
if 500 <= r.status_code < 600:
    try:
        body = r.json()
        err_type = body.get("error", {}).get("type", "")
        msg = body.get("error", {}).get("message", "")[:150]
        if "overload" in err_type.lower() or "overload" in msg.lower():
            kind = "api_529"
        else:
            kind = f"http_{r.status_code}"
    except ValueError:
        kind = f"http_{r.status_code}"
    raise BatchError(kind, msg)

# In the retry loop:
MAX_RETRIES = 8
BACKOFF_BASE = 8  # seconds
BACKOFF_CAP = 90
for attempt in range(MAX_RETRIES):
    try:
        result = call_api(...)
        break
    except BatchError as e:
        if attempt < MAX_RETRIES - 1:
            wait = min(BACKOFF_BASE * (2 ** attempt), BACKOFF_CAP)
            wait *= (0.7 + 0.6 * (hash((batch_id, attempt)) % 100) / 100)  # ±30% jitter
            time.sleep(wait)
    except Exception as e:
        # Catch-all for "shouldn't happen" exceptions — also retry
        if attempt < MAX_RETRIES - 1:
            wait = min(BACKOFF_BASE * (2 ** attempt), BACKOFF_CAP)
            time.sleep(wait)
```

**Why this pattern**:
- All 5xx + 429 + timeouts + network errors → BatchError → retry. No error class is "terminal."
- MAX_RETRIES=8 + exponential backoff caps total wait at ~5 minutes per batch under sustained failure
- Jitter (±30%) prevents thundering-herd when many scripts retry simultaneously
- Bare `except Exception` catches unknown failures (e.g. `requests.exceptions.JSONDecodeError`, custom client bugs) instead of crashing the script
- **Important**: don't `break` on `BatchError` outside the `attempt < MAX_RETRIES - 1` check — let the loop run to completion so the last attempt's failure is captured in stats

**Gaps in `scripts/ingest.py`**: the current retry logic in `phase2_chunk_analysis` (line 656-659) only catches `RuntimeError` and doesn't re-attempt. When `call_anthropic_protocol` raises on transient 5xx, ingest.py aborts Phase 2 entirely. **TODO**: port this BatchError pattern into `call_anthropic_protocol` or add a retry wrapper in `phase2_chunk_analysis`. Until then, `ingest.py` will silently lose progress on transient cluster failures.

**Lesson**: When writing ANY wrapper around an external API, the retry condition should be "any error that's plausibly transient" — not "specific magic strings." Magic-string matching breaks silently the moment the API changes its error format (e.g. 520 instead of 529). Range-based matching (`500 <= status < 600`) + exception-type matching is more durable.

## 20. Skill routing: LLM Wiki content ≠ Hermes runtime metadata (added 2026-06-12)

**Symptom**: While verifying a MiniMax-M3 context-window claim, I almost stored the "≤512K SLA / ~800K silent degradation / ≥1000K HTTP 400" finding into LLM Wiki (HardwareWiki / RadarWiki), because the HardwareWiki wiki pages already contain "512K / 524288" mentions (chip Flash sizes, completely unrelated). User pushed back: "这个 context 为什么要和 llm wiki 相关". They were right.

**The principle** (generalizes §2 from "scripts live in skill, not projects" to "all Hermes-vs-domain knowledge lives in skill, not projects"):

- **LLM Wiki (any wiki project) is the user's domain knowledge base** — datasheets, selection guides, project status, PCB designs, RF analyses. Things the user owns and wants retrievable.
- **Hermes runtime metadata is how the agent behaves** — model context lengths, picker catalog, API call retries, prompt-cache behavior, base_url semantics. Things the agent needs to operate correctly.
- **A keyword match is not a topic match**. HardwareWiki containing "512K" (chip Flash) is no reason to store "MiniMax-M3 524288-token context" in HardwareWiki — they're topically unrelated, even though the numbers rhyme.

**Decision rule** (apply before storing anything derived from a session):

| Question | If yes → | If no → |
|----------|----------|---------|
| Does this teach the agent how to do something in the user's domain? | LLM Wiki project page | Skill |
| Does this teach the agent (or future sessions) how Hermes behaves? | Skill (hermes-*, improved-wiki, vendor-datasheet-pipeline, etc.) | Memory (if a stable cross-session fact) |
| Does this describe the user's own preferences or environment? | Memory or user-profile | Skill (if a class-level how-to) |

**Anti-pattern**: Search `wiki/` for a keyword. Find matches. Conclude "this is related." Store there. User pushes back. Move to skill. The keyword search is a red herring — match on *what kind of knowledge this is*, not on whether a string appears in the corpus.

**Concrete example** (from this session): I had M3 context-window step-probe data. The probe data was generated by a Python script against the live API. The findings are about *how the M3 model behaves at the API surface* — that is runtime metadata. It belongs in `hermes-model-metadata` skill (under "Pitfalls" / "Probing the actual context limit"), not in any LLM Wiki project. The LLM Wiki would only contain M3 information if the user themselves had written analysis of M3 as a *product* worth tracking (model release notes, benchmark comparisons, integration patterns) — which is a different topic from "how does the API actually respond".

**Related**:
- §2 ("the skill owns scripts") — same principle applied to code, not knowledge
- §11 ("read the skill before reflecting") — extend: read the skill to *find the right home*, not just to verify a claim

## See also

- `references/ingest-stages-mandatory.md` — the 14-Stage contract (the authoritative source this lesson list cites into)
- `references/known-issues.md` — pipeline-stage debugging recipes; includes gotcha #30 (audit-before-launch pattern this skill extends into the agent reasoning layer)
- `references/scripting-pitfalls.md` — Python script gotchas (caption_md.py path resolution, etc.)
- `references/cron-installation.md` — running this skill from cron
- `references/nashsu-lint-source-analysis.md` — why lint complains about a page
- `references/raw-layout-compat.md` — adapting raw layout to NashSU wiki layout
- `references/scanned-pdf-to-llm-wiki-recovery.md` — class-level recipe for batch recovery of 5+ scanned-image-only PDFs (split → OCR → re-ingest into LLM Wiki). 80-page Part sizing, 5-page test gate, per-Part timing, output dir quirk, quality checks, re-ingest handoff. Verified end-to-end 2026-06-10 on RadarWiki 5-book recovery (1792 pages → 25 Parts). Run §30 audit-before-launch BEFORE invoking on resumed pipelines.
- `references/scanned-pdf-batch-recipe.md` — end-to-end 5+ scanned-PDF workflow with worked example
- `references/pre-launch-audit-decision-matrix.md` — decision matrix when §30 audit reveals state divergence
- `references/image-analysis-mechanism.md` — VLM image_analysis architecture (block types → structured extraction, sub_type taxonomy, two-step workflow + CLI `--image-analysis` flag)
- `references/caption-vs-details-comparison.md` — A/B comparison on Chinese block diagrams (caption vs details output side-by-side)
- `references/scanned-book-benchmark.md` — pipeline vs vlm-auto-engine side-by-side on 245-page `图解传热学`
- `references/llm-wiki-multimodal-pattern.md` — LLM Wiki v0.4.22 multimodal image ingestion (cloud MinerU API, SHA-256 byte-hash cache, pinned factual prompt, context-aware 150-char window, alt-text rewriter) — reference design when building local caption post-processors
- `references/arch-pipeline.md` — pipeline call chain detail
- `references/arch-vlm.md` — VLM engine auto-selection + mlx_compat quirks
- `references/network-download-methodology.md` — how to measure download speed, monitor progress, decide when to switch network
- `references/hf-proxy-download.md` — proven Python requests + proxy + xet bridge download method (2026-06-09)