# Wiki Project Maintenance & Residual File Cleanup

Periodic cleanup of stale files in a wiki project (`~/Documents/知识库/<Project>`).
Run after large batch ingests or when disk space / clutter becomes noticeable.

## `.digested` files (LEGACY — safe to delete)

`.digested` files are markers from an older pipeline version. The current
pipeline (Stage 0.2) uses `wiki/sources/<raw-rel-path>.md` existence +
wikilink integrity as the sole dedup signal. The codebase has **zero
references** to `.digested`.

- Found in: `raw/` subdirectories (one per category folder)
- Typical content: a digest log listing PDFs and their ✓ status
- Action: **delete all**. They are dead weight.

```bash
find ~/Documents/知识库/<Project> -name ".digested" -type f -delete -print | wc -l
```

## `.llm-wiki/` temp directory cleanup

`.llm-wiki/` is the runtime state directory. Some subdirectories accumulate
stale data and are safe to clean **when no ingest is running**.

**Before cleaning, verify no ingest is active:**
```bash
ps aux | grep ingest.py | grep -v grep
```

### 🔒 项目锁冲突诊断（常见卡点，2026-07-04 实战修）

`ingest.py:675` 的报错 `Could not acquire project lock — another ingest may be running` 经常出现，但很少是真正的"并发 ingest"——多数是**后台 OCR 子进程**还活着。improved-wiki 的 minerU Phase 0/1 在前台 batch 持锁时，会用 `subprocess.Popen(start_new_session=True)` 启动后台跑 `--no-project-lock`，但**主对话 LLM 阶段（Stage 2.2+）要求持锁**。所以常见卡法是：

```
Symptom: "ERROR: Could not acquire project lock — another ingest may be running"
原因 1（常见）：另一本书的 minerU/OCR 在跑，~10-20 分钟
原因 2（少数）：真的有死锁（stale lock from crashed run）
```

**诊断三件套**（一行跑完）：

```bash
# 1. 谁在跑？什么 stage？
ps aux | grep "ingest.py" | grep -v grep | awk '{print $2, $13, $14}'

# 2. 锁文件 mtime（最近的 mtime = 当前持锁者）
ls -la /Users/skyfend/Documents/知识库/<Project>/.llm-wiki/ingest*.lock 2>/dev/null

# 3. 删除 stale lock（如果找不到对应 pid）
fuser /Users/skyfend/Documents/知识库/<Project>/.llm-wiki/ingest.lock 2>/dev/null
# 没回显 = 没有人持锁，可以删；in.py 自动 take over
```

**用户策略（胡杨 2026-07-04 拍板）**：看到锁就别抢，**等 OCR 自然跑完再继续**。OCR 完成 `stage_0_done` 后会自动 `lock.release()`，主对话重新跑同一本书的 `ingest.py file.pdf`，即可从 Stage 2.2 续上。

🚫 **禁止**：未经用户许可 `kill <pid>` 后台 OCR——即使它跑了几十分钟。minerU 重启开销大（server 冷启动 60s+），杀掉就要重头 OCR。

⚠️ **真死锁判断**：如果 `ps` 里**根本没有** ingest.py 进程，但 `fuser` 还能看到 lock 引用——那是 macOS 协作锁的 kernel 残留，`fuser -k` 也清不掉。重启 macOS / `kill -9 <pid-of-old-owner-if-recoverable>` 是两个选择，但**确认是这种情况先问用户**。

### Safe to delete (stale after ingest completes)

| Directory | Purpose | When stale |
|-----------|---------|------------|
| `extract-tmp/` | minerU staging dir for PDF extraction | After ingest completes |
| `.extract-tmp/` | Legacy back-compat marker checked by `_paths.py` when detecting old `wiki/`-based runtime layout — not an active temp dir on its own | N/A (detection-only path, not written by current code) |
| `conversation/` | LLM prompt/response handoff files | After ingest completes |
| `ingest-progress/` | Crash-recovery checkpoints | When no ingest is running |

**Do NOT delete** (active state):
- `ingest-cache.json` — dedup hash cache (Stage 3.5)
- `lint-cache.json` / `lint-lock` — lint state
- `graph.json` — knowledge graph (Graph command output)
- `embed-cache.json` — embedding cache
- `lancedb/` — vector database
- `page-history/` — wiki page version backups (audit/rollback value; 18MB+ typical)
- `clusters/` — graph community hub pages
- `REVIEW/knowledge-gaps.md` — graph gap analysis output
- `review-suggestions.json` — pending review items

### Cleanup command

```bash
cd ~/Documents/知识库/<Project>
# Verify no ingest running first!
rm -rf .llm-wiki/extract-tmp/ .llm-wiki/.extract-tmp/ .llm-wiki/conversation/ .llm-wiki/ingest-progress/
```

## `wiki/concepts/.md` empty-slug file (BUG RESIDUAL)

**Symptom**: A file literally named `.md` appears in `wiki/concepts/`.
**Cause**: Pipeline bug where a chunk with zero concepts triggers a FILE
block with an empty slug. The code fix exists (`is_safe_ingest_path` now
rejects empty filenames — see known-issues.md), but
residual files from older ingests persist.

**Action**: Delete the file. It has no content value.

```bash
find ~/Documents/知识库/<Project>/wiki -name ".md" -type f -delete
```

## macOS artifacts

`.DS_Store` files accumulate throughout the directory tree. Safe to delete:

```bash
find ~/Documents/知识库/<Project> -name ".DS_Store" -type f -delete
```

Also check for macOS duplicate files (names with space + number suffix,
e.g. `lint-cache 3.json`) in `.llm-wiki/` — these are Finder copy artifacts,
not real pipeline output.

## `page-history/` decision

`page-history/` (typically 18-50MB, thousands of files) stores wiki page
versions before each overwrite. It has audit/rollback value but grows
unboundedly. Options:

- **Keep** if you want rollback capability
- **Clear** if disk space matters more: `rm -rf .llm-wiki/page-history/`
- **Prune** to recent N days if you want a middle ground (no built-in tool yet)

## "Done-but-missing" 书的诊断与恢复（2026-07-04 实战：HardwareWiki 10 本卡住）

**症状**：`.llm-wiki/<hash>.stages.json` 里 `ingested` / `write_loop_done` 都是 timestamp（看起来"消化完了"），但 `wiki/sources/<slug>.md` 实际**不存在**。

**成因**：之前 ingest 进程在写 FILE blocks 阶段崩了（OOM / context 截断 / agent 重启），但 `write_loop_done` timestamp 已经写进了 stages.json（执行到一半留下的尾巴）。下一次重启后，ingest 看 cache 说"做过"直接 skip，但 wiki/ 树里什么都没有。

**🔴 关键诊断**：cache **不准**——必须实际查文件系统，不能信 `.stages.json`。

**一键诊断脚本**（两个项目都能跑）：

```bash
python3 << 'PYEOF'
import json
from pathlib import Path
from collections import defaultdict

# 改这个变量换项目
PROJECT = Path("/Users/skyfend/Documents/知识库/HardwareWiki")
# PROJECT = Path("/Users/skyfend/Documents/知识库/RadarWiki")

stages_dir = PROJECT / ".llm-wiki"
book = PROJECT / "raw" / "Book"
src_pages = PROJECT / "wiki" / "sources" / "Book"

stuck = []
for sj in stages_dir.glob("*.stages.json"):
    # Hash 是从 raw 文件算 sha256 截断；用 files_written 反查 raw 路径
    try:
        meta = json.loads(sj.read_text())
    except Exception:
        continue
    fw = (meta.get("write_loop_done__payload") or {}).get("files_written") or []
    source_paths = [p for p in fw if p.startswith("wiki/sources/")]
    if not source_paths:
        continue
    # 一个 stages.json 通常对应一本书，files_written 列出 source page 路径
    for sp in source_paths:
        full = PROJECT / sp
        if not full.exists():
            stuck.append({
                "stages_json": sj.name,
                "missing_source_page": sp,
                "source_dir_in_book": sp.replace("wiki/sources/", ""),
            })

if not stuck:
    print("✅ 没找到 done-but-missing 的书")
else:
    print(f"❌ 发现 {len(stuck)} 本 'done-but-missing' 书：")
    for s in stuck:
        print(f"  stages={s['stages_json']}  missing={s['missing_source_page']}")

PYEOF
```

**恢复流程**（每本）：

1. 找到对应 raw 路径（用 `s['source_dir_in_book']` 里的书名去 `raw/Book/` glob）
2. **手动删 stages.json**（**不要**删 `ingest-cache.json`，只删 stages 文件）：
   ```bash
   rm /Users/skyfend/Documents/知识库/<Project>/.llm-wiki/<hash>.stages.json
   ```
3. 重新跑 `ingest.py <book.pdf>` → 会跳过 Stage 0/1/2.1/2.2（OCR 已 cache），直接进 Stage 2.4 generation，从已存的 `phase2-chunk-analyses.json` 续跑

**⚠️ 不要做的事**：
- 不要用 `ingest.py <book> --delete` 试图"重置" — `--delete` 假设书已完成，会删 cache；这里我们要的是**保留 OCR/chunks 重做 2.4**
- 不要批量删 `.stages.json`，要一本一本处理（避免修错书）

**用户偏好（2026-07-04）**：在 `--delete` 不工作的场景下，手动清理 stages.json + re-ingest 是首选路径。
