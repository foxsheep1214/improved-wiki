# Delegate Mode — Agent Orchestration

Invoking improved-wiki from an agent (Claude Code, Hermes, etc.) always uses **conversation mode** — there is no flag, and no direct-API alternative. Every text-generation LLM step (including wikilink enrichment, batched once per ingest) is handled by the calling agent with the current conversation's model. Two external dependencies outside text generation: **image captioning** (Stage 1.3, configurable VLM provider — vision content can't flow through the prompt-file handoff) and **embeddings** (Stage 3.7, local Ollama bge-m3). Both are **no-fallback**: if the caption key is missing/batch fails or the Ollama stack is down, `ingest.py` raises `RuntimeError` and pauses — the calling agent must surface this to the user rather than silently continuing. Extraction/page writes are cached, so re-running after the user fixes the dependency resumes from the failed stage.

---

## Conversation Mode

| Who calls LLM? | API key needed? |
|----------------|-----------------|
| Calling agent, via prompt files (current model) | No for text gen (agent uses its own model). Caption provider key only for image captioning. |

---

## Conversation Mode Workflow

### Step 1: Start the ingest

```bash
cd /path/to/wiki/project
scripts/ingest.py raw/Book/Book.pdf
```

At each LLM call point, `ingest.py` writes a prompt file and raises `ConversationPending` (exit code `101`).

### Step 2: Agent reads prompt and generates response

Prompt files are written to:
```
<llm-wiki>/conversation/<sha256_prefix>/<stage-slug>.md
```

The agent reads the `.md` file, executes the LLM task, and writes the result to:
```
<llm-wiki>/conversation/<sha256_prefix>/<stage-slug>.txt
```

### Step 3: Re-invoke to continue

```bash
scripts/ingest.py raw/Book/Book.pdf
```

`ingest.py` finds the result file, reads it, continues to the next stage, and repeats until completion.

### Task manifest

Pipelines with multiple LLM calls (chunk analysis, per-chunk generation) use a `tasks.json` manifest in the conversation directory to track pending/completed tasks.

---

## Agent Integration Pattern

```python
def ingest_via_conversation(pdf_path, project_path):
    while True:
        proc = subprocess.run(
            ["scripts/ingest.py", pdf_path],
            cwd=project_path,
            env={**os.environ, "IMPROVED_WIKI_ROOT": project_path},
        )

        if proc.returncode == 0:
            return  # Done

        if proc.returncode == 101:
            # Read the pending prompt
            conv_dir = find_conversation_dir(project_path)
            prompt_file = find_pending_prompt(conv_dir)
            prompt = prompt_file.read_text()

            # Execute with agent's own LLM
            result = call_llm(prompt)

            # Write result back
            result_file = prompt_file.with_suffix(".txt")
            result_file.write_text(result)
            continue

        raise RuntimeError(f"Ingest failed: {proc.returncode}")
```

---

## Implementation Notes

- `conversation_prefix` = last 8 hex chars of the raw file's SHA-256 hash (per-source isolation)
- Multiple simultaneous ingests are safe — each has a unique conversation directory
- Task files use simple markdown (no JSON serialization needed)
- `ConversationPending` exception is defined in `_core.py`

## Operational pitfalls

### Must use venv Python — system Python 3.9 will crash

**Always** invoke with `~/.venv/bin/python3` — system `/usr/bin/python3` (3.9)
fails on PEP 604 union syntax. Full explanation: `references/scripting-pitfalls.md` Pitfall 4.

```bash
IMPROVED_WIKI_ROOT="$(pwd)" ~/.venv/bin/python3 ~/.agents/skills/improved-wiki/scripts/ingest.py "raw/Book/Book.pdf"
```

### minerU OCR can take 10+ minutes — use `--stop-after-stage 0`

A 272-page book takes ~10 min for minerU OCR (6 chunks × ~100s/chunk). This will
exceed foreground terminal timeouts. Split the run:

```bash
# Phase 1: OCR only (may timeout, re-run resumes from cache)
~/.venv/bin/python3 scripts/ingest.py "raw/Book/Book.pdf" --stop-after-stage 0

# Phase 2: LLM stages (conversation mode, multiple exit-101 cycles)
~/.venv/bin/python3 scripts/ingest.py "raw/Book/Book.pdf"
```

`--stop-after-stage 0` halts **cleanly (exit 0, `{"status":"ok","stopped_after":"0"}`)**
after Stage 1.1–1.3 (text + image + caption) complete and **before** Stage 2.1 —
the 2.1 global digest is not even submitted. Re-running without the flag resumes
from the cached extraction (stage_1_x_done markers). If the OCR phase times out
mid-chunk, re-running the same command resumes from the last completed chunk
(minerU caches per-chunk results in `.llm-wiki/extract-tmp/`).

> **Behavior note (2026-06-25 fix):** previously `--stop-after-stage 0` was
> effectively dead on a fresh run — the stop check sat *after* `_do_prepare`,
> which runs all of Stage 0-2 (pausing at the 2.1/2.2/2.4 LLM handoffs) before
> that check, so the process entered 2.1 and exited 101 instead of halting after
> OCR. The check now raises `PrepareStopAfter` at the in-prepare boundary. The
> same fix makes `1` (after global digest) and `2` (after generation) halt
> cleanly. `1.5`/`2.3` (inside the chunk pipeline, no clean resume marker) remain
> best-effort — they are not intercepted on a fresh run.

### 🔒 项目锁冲突（看 ps 别抢锁，2026-07-04 实战修）

`ingest.py:675` 抛出 `Could not acquire project lock — another ingest may be running` 时，
**绝大多数情况下另一本书的 minerU/OCR 后台还在跑**（`start_new_session=True` 的 detached 子进程，
batch 起来时用 `--no-project-lock` 让 minerU 持锁跑 Phase 0/1，不影响主对话但慢 10-20 分钟）。

**操作纪律**：
1. **先 `ps aux | grep ingest.py | grep -v grep`** — 看到 `Python3 ... ingest.py raw/Book/<另一本>.pdf --stop-after-stage 0` 之类的进程就是 OCR 在跑。
2. **不要 `kill`**，等 OCR 自然完成（`stage_1_1_done` 写盘后会自动 `lock.release()`）。
3. 重跑同一本书：`ingest.py "raw/Book/this.pdf"` — 会跳过 Stage 0/1/2.1/2.2（OCR/缓存都在），从 Stage 2.4 续上。
4. 等不下去或确认是真死锁（`ps` 看不到任何 ingest.py）→ 参考 `maintenance-cleanup.md` "🔒 项目锁冲突诊断" 段。

**为什么不能抢锁**：improved-wiki 的 minerU 的锁是 `fcntl.flock`，后台 OCR 进程在另一端持着，你抢走会逼它退出、丢失正在跑的 chunk 结果，下次跑要重头 OCR（书越大越痛，500 页可能要 30 分钟）。

### Wikilink enrichment generates many merge tasks

After Stage 3.1 (write files), the pipeline enters wikilink enrichment which generates
multiple `LLM-task-*.md` merge prompts in the conversation directory. Each prompt asks
to merge an existing wiki page with a new version from the current ingest. There can be
5-15 such tasks for a single book ingest.

**Pattern for handling merge tasks efficiently:**
1. Check for pending `LLM-task-*.md` files (no corresponding `.txt`)
2. For source-page merges that appear identical to a previous merge (same existing +
   new content), copy the previous `.txt` result instead of re-generating
3. For concept/entity page merges where the "existing" page was just written by this
   ingest (no prior version), the new content IS the merged content — output it as-is
4. Use `delegate_task` to batch-process merge tasks in the background while you continue
   other work

**Merge task identification:**
```bash
# List pending merge tasks
for f in .llm-wiki/conversation/<conv_prefix>/LLM-task-*.md; do
  t="${f%.md}.txt"
  [ ! -f "$t" ] && echo "PENDING: $f"
done
```

### Re-ingest pattern: `--delete` first

`--delete` removes the source page, orphaned concept/entity pages (those whose
only source was the deleted book), media directory, and cache entry, then a
fresh run re-ingests cleanly. **Authoritative flow (backup → delete → re-ingest
→ compare): `references/re-ingest-comparison.md`.**

### Source page may be merged multiple times

The pipeline can generate 2-3 redundant source-page merge `LLM-task` prompts during
a single ingest (Stage 2.6 writes it, then enrichment re-merges it). If you see the
same source page appearing in multiple merge tasks, reuse your first merge result —
the content doesn't change between merges.

## 链式作答 → 每 chunk 独立 subagent（L4 修订，2026-07-08）

**旧政策（2026-07-02）：** 链式作答上限 2 个 handoff，用于压缩交接死区（≈30%
墙钟），叠加预取后单书 ≈ -15%。

**为什么废除：** 上限=2 只是**减缓**上下文累积，不**消除**它。2026-07-08 的
EW and Radar Systems Handbook 事故证明：即使不连答（在主对话里逐个答），
主对话自身的 context 也会随每个 chunk 的 250K 字符 prompt + 响应单调累积，
到后面的 chunk 时模型注意力被稀释到全书广度上，退化成"凭记忆答题"而非读原文。
C1/C3 硬门禁（source_quotes / key_details≤5）是在**输出端**拦症状，但**输入端**
的根因——上下文累积导致注意力分散——只有结构隔离能治。

**新政策（2026-07-08）：Stage 2.2 / 2.4 每个 chunk 派一个全新 subagent，
上限 1 handoff，答完即销毁。**

- subagent 的上下文里**只有**这一个 chunk 的 prompt（源文 + schema + 前序 digest），
  没有其他 chunk 的干扰——等价于 NashSU 子进程的无状态 `streamChat`。
- 主对话的 context 保持干净，只做编排（派发、re-invoke、进度跟踪），不承载 chunk 原文。
- **1 handoff 是硬上限，不是"看着还行就多答一个"**：答完这一个 chunk 必须退出，
  即使书还没摄入完。下一个 chunk 由主对话重新派发新 subagent。
- 交接死区重新成为代价（≈30% 墙钟），但这是用速度换质量——放弃旧 L4 的 -15% 效率。
- 不同 stage 类型的 handoff 一律留给主对话重新派发（保证 prompt 规则正确）。
- 仅限单书串行；跨书 2.3+ 并行仍然禁止（不变量不变）。
- 每次 re-invoke 前仍跑 `scripts/qc_stage22.py` 质量检查防止退化响应蒙混过关。
- **Skolnik 事故（2026-07-07）的教训仍然适用**：那次根因是连答 14 个 chunk
  不退出；新政策下不可能发生（上限=1），但主对话如果跳过 subagent 直接自己答
  多个 chunk，效果等价于"连答"，同样退化——所以"主对话直接答"也被禁止。
