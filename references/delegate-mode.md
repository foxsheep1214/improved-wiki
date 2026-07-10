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

### Reporting stage progress to the user (2026-07-10)

When narrating progress to the user (e.g. "advancing to Stage 2.9"), always pair the
numeric stage with its Chinese keyword — a bare stage number isn't readable at a
glance. Fixed mapping, reuse verbatim rather than re-wording each time:

| Stage | 关键词 |
|---|---|
| 1.1–1.3 | 提取/OCR/配图 |
| 2.2 | 分块分析 |
| 2.3 | 关联检测 |
| 2.4 | 页面生成 |
| 2.6 | 源页生成 |
| 2.7 | 问题生成 |
| 2.9 | 对比生成 |
| 3.1/3.2 | 写入 |
| 3.4 | 质量审查 |
| 3.5 | 聚合修复 |
| 3.7 | 嵌入 |

This is a skill-level convention (applies to any agent orchestrating improved-wiki,
not just one session's personal preference) — user-requested 2026-07-10.

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

### Read tool spuriously fails on large prompt files (2.2/2.4 handoffs)

A 2.2/2.4 chunk prompt is routinely 300-460KB with at least one very long
single line (the capped "Existing wiki pages" slug list, or the raw
`<extracted_text>` block). The Read tool sometimes fails/over-estimates token
count on certain (offset, limit) combinations against files this shape — seen
live on both a Hansen chunk-1 retry and a Wiley chunk-4 prompt — even though
the file itself is intact (valid UTF-8, no corruption). This is a Read-tool
quirk, not a skill bug and not a sign the prompt file is broken.

**Workaround for the answering subagent**: narrow the `limit` until reads
succeed (binary-search down if a full-file or large-limit read fails), or
fall back to `sed -n 'START,ENDp'`/`grep -n`/`awk` via Bash to inspect the
problem region directly — both are already in every subagent's toolset. Don't
conclude the prompt file is missing or corrupted from a single failed Read.

### Must use venv Python — system Python 3.9 will crash

**Always** invoke with `~/.venv/bin/python3` — system `/usr/bin/python3` (3.9)
fails on PEP 604 union syntax. Full explanation: `references/scripting-pitfalls.md` Pitfall 4.

```bash
IMPROVED_WIKI_ROOT="$(pwd)" ~/.venv/bin/python3 ~/.agents/skills/improved-wiki/scripts/ingest.py "raw/Book/Book.pdf"
```

### minerU OCR can take 10+ minutes — use `--stop-after-stage 0`

A 272-page book takes ~10 min for minerU OCR (9 chunks × 32 pages/chunk). This will
exceed foreground terminal timeouts. Split the run:

```bash
# Phase 1: OCR only (may timeout, re-run resumes from cache)
~/.venv/bin/python3 scripts/ingest.py "raw/Book/Book.pdf" --stop-after-stage 0

# Phase 2: LLM stages (conversation mode, multiple exit-101 cycles)
~/.venv/bin/python3 scripts/ingest.py "raw/Book/Book.pdf"
```

`--stop-after-stage 0` halts **cleanly (exit 0, `{"status":"ok","stopped_after":"0"}`)**
after Stage 1.1–1.3 (text + image + caption) complete and **before** Stage 2.2 —
no chunk-analysis prompt is even submitted. Re-running without the flag resumes
from the cached extraction (stage_1_x_done markers). If the OCR phase times out
mid-chunk, re-running the same command resumes from the last completed chunk
(minerU caches per-chunk results in `.llm-wiki/extract-tmp/`).

> **Behavior note (2026-06-25 fix):** previously `--stop-after-stage 0` was
> effectively dead on a fresh run — the stop check sat *after* `_do_prepare`,
> which runs all of Stage 0-2 (pausing at the 2.1/2.2/2.4 LLM handoffs) before
> that check, so the process entered 2.1 and exited 101 instead of halting after
> OCR. The check now raises `PrepareStopAfter` at the in-prepare boundary. The
> same fix makes `2` (after generation) halt cleanly; stop point `1` (after the
> former Stage 2.1 digest) retired with 2.1's removal (2026-07-08). `1.5` stops
> at the prefetch boundary after 2.2 (see `batch-parallel-prefetch.md`).

### 🔒 项目锁冲突（看 ps 别抢锁）

`Could not acquire project lock` 绝大多数是**另一本书的后台 OCR 还在跑**，不是死锁：先
`ps aux | grep ingest.py`，看到就**不要 kill**，等它自然完成释放锁后重跑本书（缓存都在，
从 Stage 2.2+ 续上）。完整诊断三件套与真死锁处置见 `maintenance-cleanup.md` "🔒 项目锁冲突诊断"（权威版）。

### Wikilink enrichment generates many merge tasks

After Stage 3.1 (write files), the pipeline enters wikilink enrichment which generates
multiple `LLM-task-*.md` merge prompts in the conversation directory. Each prompt asks
to merge an existing wiki page with a new version from the current ingest. There can be
5-15 such tasks for a single book ingest.

> **零出链闸门（2026-07-09）**：enrichment 批量 round-trip 只覆盖写盘后正文
> **零出链**的页面（`_enrich_wikilinks.py`）。2.4 生成时已强制内联
> `[[wikilinks]]`，所以绝大多数 ingest 的 enrichment 批次为空、整个 round-trip
> 被跳过（打印 `[enrich] N/M page(s) already carry inline [[wikilinks]]`）。
> 见到 enrichment handoff 本身就说明确有零出链页，正常作答即可。

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

### Re-ingest pattern: `--delete` first — ask full-redo vs analysis-only

`--delete` removes the source page, orphaned concept/entity pages (those whose
only source was the deleted book), media directory (images+captions — backed
up to `page-history/media/` first, 2026-07-10), and cache entry, then a fresh
run re-ingests cleanly. **Ask the user first** whether they want a full redo
(re-extract OCR/images/captions too) or an analysis-only re-ingest that reuses
existing OCR/images/captions (`--delete --keep-media`) — media has no separate
cache, so once removed without `--keep-media` it can only come back via a full
minerU re-call. **Authoritative flow (backup → delete → re-ingest → compare),
both variants: `references/re-ingest-comparison.md`.**

### Source page may be merged multiple times（已代码化，2026-07-09）

历史问题：单次 ingest 可能产生 2-3 个冗余的源页 merge `LLM-task` prompt（同一
FILE block 在写循环中重复出现，与我们自己刚写的字节级相同内容再 merge 一次）。
旧缓解是操作纪律（"复用第一次 merge 结果"）。现已在写循环代码级修掉
（`_ingest_write.py::_is_redundant_duplicate_write`）：同一路径+相同内容的重复
块直接跳过（打印 `[skip]`）；同一路径+**不同**内容仍走 merge——那是设计内的
same-slug collision merge，不是冗余。如再见到重复 merge 任务，属回归，应查代码
而非手工绕过。

## 链式作答 → 每个 handoff 独立 subagent（L4 修订，2026-07-08）

**旧政策（2026-07-02）：** 链式作答上限 2 个 handoff，用于压缩交接死区（≈30%
墙钟），叠加预取后单书 ≈ -15%。

**为什么废除：** 上限=2 只是**减缓**上下文累积，不**消除**它。2026-07-08 的
EW and Radar Systems Handbook 事故证明：即使不连答（在主对话里逐个答），
主对话自身的 context 也会随每个 chunk 的 250K 字符 prompt + 响应单调累积，
到后面的 chunk 时模型注意力被稀释到全书广度上，退化成"凭记忆答题"而非读原文。
C1/C3 硬门禁（source_quotes / key_details≤5）是在**输出端**拦症状，但**输入端**
的根因——上下文累积导致注意力分散——只有结构隔离能治。

**新政策（2026-07-08；当晚扩展到全部 handoff）：每个 LLM handoff——逐 chunk
的 2.2/2.4，以及单发的 2.4 去重确认 / 2.6 源页 / 2.7 问题+跨源判定 / 2.9 对比 /
3.4 review / merge loop / wikilink enrichment——一律派一个全新 subagent，
上限 1 handoff，答完即销毁。主对话零 LLM 作答，只做编排。**

- **唯一例外：context probe**（一次 ~百字节小往返，发生在任何累积之前，派
  subagent 只添延迟无收益；且 probe 的意义就是测"当前会话模型"的窗口）。
- **为什么扩展**：单发 handoff 每本书只出现一次，单书场景累积可忽略；但 batch
  连续消化多本书时，主对话要吃下 N 组 2.6+2.7+2.9+3.4（每组几十到几百 KB）+
  编排噪音——同一条注意力稀释曲线，只是斜率更缓。全 handoff 隔离后主对话内容
  恒定为纯编排，与 NashSU 的 per-call 无状态 `streamChat` 完全等价，不再是
  "关键处等价"的折衷。
- **代价**：每本书多 ~5-7 次交接死区。质量优先，接受。

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

### Hansen 事故（2026-07-09）：subagent 自行拆分单个 handoff 会产生"已完成"但答案文件不存在

一个 2.2 chunk 的原始文本 ~250K 字符，subagent 有时会把这一个 handoff
**自行拆成多个自己派发的子任务**（如"Ch1+Ch2 start / Ch2 continued / Ch3
part1 / Ch3 part2"分头并行提取），逐段返回散装的 entities/concepts/claims 文本，
最终给主对话回报"completed"，但从未把结果写成 schema 要求的单个 YAML 写入
`<stage-slug>.txt`——因为分头提取的每个子任务各自只覆盖了整体 schema 的一部分
（例如只有 concepts 没有 updated_global_digest），没有一个子任务是"完整答案"。
主对话核对 `.txt` 文件是否存在时才发现文件从未生成，此前的分头输出全部作废，
被迫重新派发一次完整版 subagent 重做——纯浪费。

**根因**：delegate-mode 的"1 handoff = 1 subagent"约束只界定了主对话侧的派发
粒度，没有明确禁止 subagent 自己在内部再次派发 Agent 工具。当 chunk 文本很长时，
subagent 会"合理地"想要并行化，但并行化的产物不满足 schema 契约。

**修复**：派发 2.2/2.4 等 chunk-analysis handoff 时，prompt 必须显式包含：
"这是一个自包含的单一任务——不要派发任何后台进程或等待任何其他 agent，你自己
读完整个 chunk、自己分析、自己写出这一个完整的 YAML 答案文件，仅此而已。"
主对话在收到"completed"通知后，**必须先验证 `<stage-slug>.txt` 确实存在
且内容通过 schema 校验，再继续 re-invoke `ingest.py`**——不能只信任
"completed"状态本身，那只代表 harness 认为 subagent 停止了，不代表它完成了
被要求的任务。
