# Batch Pipeline Ingest — 多书流水线（minerU[N+1] ∥ spine[N]）

> **归属**：`batch_ingest`（ingest.py）的内部设计。多书 batch 时自动启用，**不需要操作者手动编排**。
> **边界**：只并行 wiki-independent 的 Phase 0/1（minerU + caption）；2.1/2.2 + 2.3+ spine 必须串行一本一本（见 [[batch-digest-patterns]]、[[delegate-mode]]）。

## 设计：流水线，不是 barrier

旧设计（barrier）：所有书的预取（0/1/2.1/2.2）全跑完 → 才开始任何 spine。问题：book 1 的 spine 干等 book N 的 minerU。

新设计（pipeline，2026-06-28）：book N 的 LLM 工作（2.1/2.2 + spine）和 book N+1 的 minerU 提取**重叠**。

```
主对话（串行，一本一本）：
  book1: 等 Phase0/1(已缓存) → 2.1/2.2(LLM handoff) → 2.3+(spine, LLM handoff)
  → book2: 等 Phase0/1(bg 已跑完) → 2.1/2.2 → spine
  → book3: 等 Phase0/1 → 2.1/2.2 → spine

后台 detached 子进程（minerU + caption，非 LLM）：
  bg-A: book2 的 Phase 0/1  ──────┐
  bg-B: book3 的 Phase 0/1  ──────┤  (minerU fcntl.flock 串行，但与主对话 LLM 重叠)
                                  └→ book2/3 的 Phase 0/1 在 book1 spine 期间跑完
```

## 实现机制（ingest.py）

- **`--no-project-lock` 标志**：单文件路径跳过 `ProjectLock`。供后台 extract 子进程用——主 batch 持锁，bg 不能再 acquire（会死锁）。Phase 0/1 不写 wiki/，不需要锁。
- **`_launch_bg_extract`**：用 `subprocess.Popen(start_new_session=True)` 启动 detached 子进程跑 `ingest.py --stop-after-stage 1 --no-project-lock <book>`。`start_new_session` 让它跨主 batch 的 ConversationPending exit 存活。
- **`_wait_extract_done`**：主 batch 到达 book N 时，poll `is_stage_done(h, "stage_1_3_done")` 等 bg 跑完（book 1 是初始 minerU 等待；book 2+ 应已被前一本 spine 期间跑完）。
- **bg-state 持久化**（`.llm-wiki/batch-bg.json`）：记录已启动的 bg PID。`_pid_alive` 检查 PID 存活——死了就重开，不死等。跨 handoff re-invoke 复用。
- **stage 标记**：Phase 1 完成 = `stage_1_3_done`（不是 "1"）。

## 操作者只需

```
cd <project-root>
python3 ingest.py "raw/Book/A.pdf" "raw/Book/B.pdf" "raw/Book/C.pdf"
```

batch 自动：启动 bg extract（B、C）→ 处理 A（2.1/2.2 + spine，LLM handoff 给你）→ 你答一个 handoff、重跑，batch 推进 → A 写完 → B（Phase 0/1 已就绪）→ ...。每个 handoff 你作答后重跑即可。

## context 隔离（可选，省钱）

主对话累积历史会让每个 handoff 作答越来越贵。可选：每个 handoff 派一个 fresh subagent 作答（读 prompt、写 .txt、返回），主对话只协调。subagent 不继承主对话上下文，省 ~150k input token/handoff。

**注意**：subagent 只能作答 wiki-independent 的 2.1/2.2 handoff；**2.4+ spine handoff 也可以交给 subagent 作答**（每个 handoff 独立：读 prompt、写 .txt），但 spine 的协调（重跑 batch）留在主对话，保证一本一本串行。

## 关键不变量

- **spine 串行**：一次只有一本书在 2.3+。bg 只做 Phase 0/1（不碰 wiki/），所以并行安全。
- **ConversationPending 跨 re-invoke**：bg 是 detached 子进程，主 batch exit 101 不杀它。stage-progress cache 让主 batch loop 干净 resume。
- **minerU fcntl.flock**：多个 bg 的 minerU 自动串行（不并发开两个 minerU）。

## 相关

- [[batch-digest-patterns]] — batch 串行主干规则与坑
- [[delegate-mode]] — conversation mode 的 agent 作答机制
- [[conversation-mode-agent-workflow]] — 单书 ingest 的逐 stage 作答 cheat sheet
