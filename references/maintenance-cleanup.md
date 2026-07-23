# Wiki Project Maintenance & Residual File Cleanup

Periodic cleanup of stale files in a wiki project (`~/Documents/知识库/<Project>`).
Run after large batch ingests or when disk space / clutter becomes noticeable.

## `.digested` files (LEGACY — safe to delete)

`.digested` files are markers from an older pipeline version. The current
pipeline (Stage 0.2) decides skip/resume from the `ingested` completion marker
in `<hash>.stages.json`（2026-07-08 从 `stage_4_1` 改名）+ `wiki/sources/<raw-rel-path>.md`
existence. The codebase has **zero references** to `.digested`.

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

### 🔒 项目锁冲突诊断

single/batch/watch 仅在当前书的 Stage 2.3+ 活动调用中持有
`.llm-wiki/ingest.lock`。Phase 1 OCR/caption、Stage 2.2、后台等待和 watch
空闲轮询都不持有该锁。因此 `Could not acquire project lock` 表示另一个
wiki-dependent writer 正在运行，不再等同于“后台 OCR 还活着”。

```bash
# 查真实 flock 持有者；锁文件本身长期存在是正常的
lsof /Users/skyfend/Documents/知识库/<Project>/.llm-wiki/ingest.lock

# 查正在运行的协调器/后台 worker/minerU
ps -ax -o pid=,ppid=,etime=,command= | \
  rg 'ingest\.py|mineru\.cli\.fast_api|file_parse'
```

`ProjectLock` 是打开文件描述符上的 `fcntl.flock`：进程退出时内核自动释放。
文件内容和 mtime 只用于诊断，文件仍存在不代表锁仍被持有；不要删除它，也不存在
“没有进程但 kernel 永久残留 flock、必须重启”的正常状态。

conversation handoff 的 exit 101 必然释放这个 kernel flock，所以另有
`.llm-wiki/spine-reservation.json` 保留 source-bound 逻辑 owner。先统一查看：

```bash
python3 "$SKILL_DIR/scripts/ingest.py" --batch-status
```

同一本 source hash 可跨 handoff 重入；不同书不能进入 Stage 2.3+。正常
complete/skip 会清 reservation。失败时保留它是安全闸门，不是死锁：先恢复 owner。
只有明确放弃该书并检查过部分写入后，才运行
`--abandon-spine <status显示的8位hash>`；不要手删 JSON。
该命令还会先取得 `ingest.lock`，有 live writer 时拒绝放弃。

若需要有意暂停批量任务，使用：

```bash
# 全部暂停
python3 "$SKILL_DIR/scripts/ingest.py" --pause-batch

# 只暂停后台 OCR/caption，允许已完成 Phase 1 的书继续
python3 "$SKILL_DIR/scripts/ingest.py" --pause-prefetch
```

这会建立相应 pause marker，并只向 token/source/lease/心跳身份匹配的 detached
进程组发信号。
不要手工按旧 `batch-bg.json` PID 杀进程；PID 可能陈旧或已复用。恢复时用完整、
已确认的文件列表加 `--resume-batch`；只恢复 OCR/caption 可用
`--resume-prefetch`。

`batch.pause` 是项目级 full pause，普通单文件 `ingest.py book.pdf` 也不能绕过；
这是为了拦截把批次拆成单书反复 re-invoke 的旧驱动。若只想停 OCR/caption 而让
已提取书继续主干，必须使用 `--pause-prefetch`，不要使用 full pause。

### Safe to delete (stale after ingest completes)

| Directory | Purpose | When stale |
|-----------|---------|------------|
| `extract-tmp/` | minerU staging dir for PDF extraction | After ingest completes |
| `.extract-tmp/` | Legacy back-compat marker checked by `_paths.py` when detecting old `wiki/`-based runtime layout — not an active temp dir on its own | N/A (detection-only path, not written by current code) |
| `conversation/` | LLM prompt/response handoff files | After ingest completes |
| `ingest-progress/` | Crash-recovery checkpoints | When no ingest is running |

**Do NOT delete** (active state):
- `ingest-cache.json` — dedup hash cache (Stage 3.5)
- `batch.pause` / `batch-prefetch.pause` — explicit pause state
- `batch-bg.json` / `batch-workers/*.json[.lease]` — detached-worker identity
- `spine-reservation.json` — source-bound Stage 2.3+ owner across handoffs
- `batch-coordinator.lock` / `ingest.lock` / `watch.lock` — advisory files whose
  kernel flocks, not file existence, indicate a live holder
- `lint-cache.json` / `lint-lock` — lint state
- `graph.json` — knowledge graph (Graph command output)
- `embed-cache.json` — embedding cache
- `lancedb/` — vector database
- `page-history/` — wiki page version backups (audit/rollback value; 18MB+ typical)
- `review-suggestions.json` — pending review items

（注：graph 产物 `clusters/` 与 `REVIEW/knowledge-gaps.md` 实际由 `graph.py` 写在
**`wiki/` 下**（`wiki/clusters/`、`wiki/REVIEW/knowledge-gaps.md`），不在 `.llm-wiki/`
——同样不要删，但不属于本清单的 runtime 目录范围。）

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
3. 重新跑 `ingest.py <book.pdf>` → 会跳过 Stage 0/1/2.2（OCR 已 cache），直接进 Stage 2.4 generation，从已存的 `phase2-chunk-analyses.json` 续跑

**⚠️ 不要做的事**：
- 不要用 `ingest.py <book> --delete` 试图"重置" — `--delete` 假设书已完成，会删 cache；这里我们要的是**保留 OCR/chunks 重做 2.4**
- 不要批量删 `.stages.json`，要一本一本处理（避免修错书）

**用户偏好（2026-07-04）**：在 `--delete` 不工作的场景下，手动清理 stages.json + re-ingest 是首选路径。
