# Batch Pipeline Ingest — 多书流水线（minerU[N+1] ∥ spine[N]）

> **归属**：`batch_ingest`（ingest.py）的内部设计。多书 batch 时自动启用，**不需要操作者手动编排**。
>
> **核心设计：流水线并行，不是 barrier。** 一条命令传所有书 → 系统自动把下一本的 OCR 预取和当前书的 LLM 工作重叠。**不要手动分书分步跑**（会丧失并行能力，OCR 被串行到 LLM 后面）。
>
> **并行边界（三层）**：
> ① **进程级后台预取**（bg OS 子进程）：Phase 0/1（minerU + caption，非 LLM）——自动与主对话 LLM 并行；
> ② **可并行作答的 conversation prompt** = wiki-independent 段（Phase 0/1 + Stage 2.2）——多个同时 pending 时可每个派一个 sub-agent 并行作答，**并发上限 = `--parallel N`**（默认4，见下方"并发上限"节）；
> ③ **wiki-dependent spine（2.3+）**：串行，一次只有一本书在 2.3+，其 handoff 一次只有一个 pending（见 [[batch-digest-loop]]、[[delegate-mode]]）。

## 并发上限（2026-07-07 补全）

`--parallel N` 只约束了代码层（OCR 子进程数），没有一处明说"派给这些 handoff 作答的 sub-agent 数量也要 ≤N"。**这条现在补上：并行作答 wiki-independent handoff 时，同时存在的 sub-agent 数量不得超过 `--parallel` 的值**（默认4）。

- 原因：即使 minerU 本身靠 `fcntl.flock` 强制单实例（不受 `--parallel` 影响），LLM handoff（2.2）不受这把锁约束——如果一次喂几十本书且它们的 Phase 0/1 恰好在短时间内密集完成，理论上可能同时冒出远多于 `--parallel` 数量的 pending handoff。不设上限会导致同时派发的 sub-agent 数量跟 `--parallel` 脱钩，跟"`--parallel` 控制并行度"这个用户可见的预期不一致，也可能不必要地推高瞬时并发成本。
- 做法：主对话维护一个"当前活跃 sub-agent 数"计数，达到 `--parallel` 时新出现的 pending handoff 排队，等某个 sub-agent 完成退出后再派发下一个——不需要精确的调度器，简单的"派够 N 个就等一个回来再派"即可。

## 设计：流水线，不是 barrier

旧设计（barrier）：所有书的预取（0/1/2.2）全跑完 → 才开始任何 spine。问题：book 1 的 spine 干等 book N 的 minerU。

新设计（pipeline，2026-06-28）：book N 的 LLM 工作（2.2 + spine）和 book N+1 的 minerU 提取**重叠**。

```
主对话（串行，一本一本）：
  book1: 等 Phase0/1(已缓存) → 2.2(LLM handoff) → 2.3+(spine, LLM handoff)
  → book2: 等 Phase0/1(bg 已跑完) → 2.2 → spine
  → book3: 等 Phase0/1 → 2.2 → spine

后台 detached 子进程（minerU + caption，非 LLM）：
  bg-A: book2 的 Phase 0/1  ──────┐
  bg-B: book3 的 Phase 0/1  ──────┤  (minerU fcntl.flock 串行，但与主对话 LLM 重叠)
                                  └→ book2/3 的 Phase 0/1 在 book1 spine 期间跑完
```

## 实现机制（ingest.py）

- **`--no-project-lock` 标志**：单文件路径跳过 `ProjectLock`。供后台 extract 子进程用——主 batch 持锁，bg 不能再 acquire（会死锁）。Phase 0/1 不写 wiki/，不需要锁。
- **`_launch_bg_extract`**：用 `subprocess.Popen(start_new_session=True)` 启动 detached 子进程跑 `ingest.py --stop-after-stage 0 --no-project-lock <book>`（"0" = Phase 1 完成后干净退出；旧值 "1" 的停靠点已随 Stage 2.1 移除，2026-07-08 改）。`start_new_session` 让它跨主 batch 的 ConversationPending exit 存活。
- **`_wait_extract_done`**：主 batch 到达 book N 时，poll `is_stage_done(h, "stage_1_3_done")` 等 bg 跑完（book 1 是初始 minerU 等待；book 2+ 应已被前一本 spine 期间跑完）。
- **bg-state 持久化**（`.llm-wiki/batch-bg.json`）：记录已启动的 bg PID。`_pid_alive` 检查 PID 存活——死了就重开，不死等。跨 handoff re-invoke 复用。
- **stage 标记**：Phase 1 完成 = `stage_1_3_done`（不是 "1"）。

## 操作者只需

```
cd <project-root>
python3 ingest.py "raw/Book/A.pdf" "raw/Book/B.pdf" "raw/Book/C.pdf"
```

batch 自动：启动 bg extract（B、C）→ 处理 A（2.2 + spine，LLM handoff 给你）→ 你派 fresh subagent 答一个 handoff、重跑，batch 推进 → A 写完 → B（Phase 0/1 已就绪）→ ...。每个 handoff 派 subagent 作答后重跑即可。

## context 隔离（强制，2026-07-08 起）

主对话累积历史会让每个 handoff 作答越来越贵。**强制（2026-07-08 起，原为可选）**：每个 handoff 派一个 fresh subagent 作答（读 prompt、写 .txt、返回），主对话只协调（唯一例外 context probe）。subagent 不继承主对话上下文，省 ~150k input token/handoff，且消除多书累积的注意力稀释（见 delegate-mode.md L4）。

**注意（与 SKILL.md 规则的关系）**：可**并行**作答的只有 wiki-independent 的预取 handoff（Phase 0/1 + 2.2，SKILL.md 规则）。串行 spine（2.3+）的 handoff 一次只有一个 pending——把它交给一个 fresh subagent 作答是 context 隔离（自 2026-07-08 起为强制政策），**不是并行**，不违反串行不变量；spine 的协调（重跑 batch）必须留在主对话，保证一本一本串行。任何时刻都不允许两本书的 2.3+ handoff 同时在处理。

## 进阶：`--stop-after-stage 1.5` 预取（隐藏下一本的 2.2，实测 2026-07-02）

进程级 bg 预取只能到 Phase 0/1（bg 进程无法作答 LLM handoff）。但 2.2 是
wiki-independent（SKILL.md 规则 ②），操作者可以**手动开第二条 conversation 流水线**
把下一本书的 digest+chunk 分析也提前做掉：

```
# book N 的 spine 正常推进的同时，另开一条：
python3 ingest.py --stop-after-stage 1.5 --no-project-lock "raw/Book/<book N+1>.pdf"
# 每次 exit 101 → 派一个 fresh subagent 作答该 handoff → re-invoke（与 spine 的
# subagent 并行，互不等待）。到 2.2 全部完成时干净退出（PrepareStopAfter("1.5")，
# stage_2_2_done 缓存）。book N 写完后直接跑 spine，2.2 全部 cache 命中，
# 从 2.3 起步。
```

**实测**（RadarWiki，2026-07-02，64K chunk）：book4（3 chunks，spine 全程 125 min
LLM 延迟）期间并行预取 book5（Barton 系统分析与建模，6 chunks）的 digest + 3 个
chunk 分析共 **86 min LLM 延迟，全部隐藏、零墙钟成本**。按今晚三本书统计，
2.2（实测时含已并入的 2.1）占单书总 LLM 延迟的 40-56%——即该模式能把整批消化墙钟压掉约四成。

**注意**：预取时 2.2 的 existing-wiki 快照早于前一本书落盘，book N+1 对 book N
新页的连接提案会缺失（SKILL.md 设计上接受的折衷）；主题强相关的相邻两本书若在意
交链密度，可放弃预取回退纯串行。

## 关键不变量

- **spine 串行**：一次只有一本书在 2.3+。bg 只做 Phase 0/1（不碰 wiki/），所以并行安全。
- **ConversationPending 跨 re-invoke**：bg 是 detached 子进程，主 batch exit 101 不杀它。stage-progress cache 让主 batch loop 干净 resume。
- **minerU fcntl.flock**：多个 bg 的 minerU 自动串行（不并发开两个 minerU）。

## 相关

- [[batch-digest-loop]] — batch 驱动循环、串行主干规则与坑
- [[delegate-mode]] — conversation mode 的 agent 作答机制
- [[conversation-mode-agent-workflow]] — 单书 ingest 的逐 stage 作答 cheat sheet
