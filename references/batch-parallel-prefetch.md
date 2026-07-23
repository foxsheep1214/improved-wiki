# Batch Pipeline Ingest — 两级 Phase-1 预取 + 单写入主干

> **归属**：`ingest.py::batch_ingest`。多文件批处理自动启用；操作者传入完整文件列表，
> 不需要手工拆分 OCR。

## 当前并行模型

自动批处理有三类并行边界：

1. **Phase 1 后台进程**：最多两个 detached worker。全局始终只有一个 worker
   使用 minerU；另一个可同时进行图片描述，形成 `OCR[N+1] ∥ caption[N]`。
2. **主对话 Stage 2.2**：只推进当前书。chunk 分析依赖滚动 digest，严格逐 chunk
   串行；普通 batch 不会自动让多本书同时产生 2.2 handoff。
3. **Stage 2.3+ spine**：跨书严格串行。项目锁只在当前书的
   `2.3 → generate → write → finalize` 区间持有。书内 Stage 2.4 的多个
   generation prompt 仍可并行作答，但按 `--parallel N` 分波发出，而不是无上限
   一次吐出全部 prompt。

```
后台 worker A:  book N   OCR ─────────→ caption ─────→ done
后台 worker B:             等 minerU ─→ OCR ────────→ caption
主协调进程:     等 book N Phase 1 → 2.2 → [ProjectLock: 2.3+ spine]
```

minerU 和 caption 都各有一个跨进程资源槽：

- minerU：`~/.cache/improved-wiki/.mineru.lock`，一次一本。
- caption：系统临时目录中的 per-user flock，一次一个 caption round；round 内仍按
  `CAPTION_MAX_WORKERS=4` 并行逐图调用。这样两个后台 worker 不会叠加成 8 个远程请求。

## `--parallel N` 的真实语义

- `N=1`：只启动一个 Phase-1 worker，不做 OCR/caption 跨书重叠。
- `N>=2`：Phase-1 worker 上限为 2；超过 2 不会再增加 OCR 进程，因为流水线只有
  minerU 和 caption 两个独占资源槽。
- 同一个数也是 Stage 2.4 并行 handoff 波次的硬上限。例如 10 个未缓存 chunk
  配 `--parallel 4` 时按 `4 + 4 + 2` 推进；`N=1` 才会显式退化为串行；若 N
  不小于剩余 chunk 数，仍一次发出全部。
- Stage 2.2 **不**套用这类并行波次：chunk N+1 的 prompt 依赖 chunk N 更新后的
  rolling digest，因此严格串行。Stage 2.4 的 owner slug inventory 在生成前已
  确定，各 chunk 不存在这种滚动内容依赖。

默认 `--parallel 4`，因此自动 Phase-1 实际使用两个 worker。

## 顺序保证

调度器不会同时盲启两个 PDF：

1. 先启动按文件列表排序的第一本待提取书。
2. worker 状态进入 `mineru`（已取得全局锁）后，才允许启动下一本。
3. 第二本进入 `waiting_mineru`，因此不能抢在第一本前面。
4. 当前书完成 Phase 1 后释放一个 worker 槽，再按列表顺序补入下一本。

恢复批次时会扫描并跳过任意长度的 `stage_1_3_done` 缓存前缀，启动第一本真正
待提取的书，而不是只检查相邻一本。

## 进程监督

`.llm-wiki/batch-bg.json` 保存协调信息；每个 worker 另有
`.llm-wiki/batch-workers/<hash>-<token>.json`，包含：

- 随机 token、source hash、PID、进程组 PGID；
- `started_at`、`heartbeat_at`；
- `status`、`phase`、`exit_code`、`error`。

每个状态文件还有同名 `.lease` 文件。worker 从启动到退出始终持有随机 token
对应的 kernel flock；协调器按“token + source hash + 心跳 + lease + PID/PGID”
联合判断：

- `EPERM` 只表示 PID 探测未知；新鲜心跳仍视为运行中。
- lease 已存在但无人持有，证明原 worker 已退出；即使数字 PID 又变成 live，也按
  PID 复用处理，绝不向它发信号。
- 心跳过期时，即使 PID 探测返回 `EPERM`，也判为 stalled，不再无限等待。
- worker 明确失败、退出或停滞后，协调器立即转为前台缓存恢复。
- 单次 heartbeat 原子写失败只告警并在下一周期重试，不会永久杀死心跳线程。
- 默认没有固定两小时总超时；可用
  `IMPROVED_WIKI_BG_EXTRACT_MAX_SECONDS` 设置硬上限。默认心跳失效窗口为 60 秒。

后台 worker 使用独立 session/process group。停止时对整个进程组发信号，worker、
minerU API 子进程和其后代一起清理，不再只终止 Python 父进程。只有 lease
确证为当前 worker 时才允许发信号；旧版无 lease 状态宁可警告并拒绝误杀。

## 两层主干锁与协调器锁

- `.llm-wiki/batch-coordinator.lock`：短期 kernel flock，同一项目同一时刻只允许一个
  live batch/watch 调度调用修改 `batch-bg.json`。exit 101 后自动释放，便于重启。
- `.llm-wiki/ingest.lock`：短期 kernel flock，仅覆盖当前进程中实际执行
  Stage 2.3+ 的窗口。conversation handoff 退出时会自动释放。
- `.llm-wiki/spine-reservation.json`：跨 exit 101 的持久逻辑 owner。它绑定 source
  hash，同一本书重启可重入；不同书会被拒绝，直到 owner 完成/skip，或操作者明确
  放弃。这样不会在当前书等待 Stage 2.4、merge、review 等回答时让后一本偷进
  Stage 2.3，导致关联快照漂移。
- batch 重启会在启动新 prefetch 前校验：reservation owner 必须仍在文件列表中，
  且必须是列表里的第一本未完成书。漏传或重排不会先做一轮昂贵 OCR 后才暴露冲突。

如果 serial spine 发生写入或 finalization 错误，batch 会在第一本失败书处停止，
后续书不进入 Stage 2.3+；watch 中这些未尝试的条目保持 pending，也不消耗 retry。
失败书的 reservation 保留，先从缓存恢复它。

## 暂停和恢复

```bash
# 在项目根目录执行；不需要重复文件列表
python3 "$SKILL_DIR/scripts/ingest.py" --pause-batch

# 只暂停 OCR/caption 预取；不冻结已经提取完的书
python3 "$SKILL_DIR/scripts/ingest.py" --pause-prefetch

# 查看 pause、worker、handoff、spine owner 和未完成 source
python3 "$SKILL_DIR/scripts/ingest.py" --batch-status

# 只恢复 OCR/caption 预取
python3 "$SKILL_DIR/scripts/ingest.py" --resume-prefetch

# 恢复时必须重新给出已经确认过的完整列表
python3 "$SKILL_DIR/scripts/ingest.py" --resume-batch \
  "raw/Book/A.pdf" "raw/Book/B.pdf" "raw/Book/C.pdf"
```

`--pause-batch` 写入 `.llm-wiki/batch.pause` 并终止所有身份已验证的后台进程组。
当前协调器会停止；进度、OCR chunk、图片和 caption 均保留。该 marker 是项目级
full pause：multi-file batch、watch 和普通单文件 `ingest.py book.pdf` 都会拒绝
推进，避免旧驱动把一批书拆成单文件命令后绕过暂停。必须用已确认的完整列表显式
`--resume-batch`。`--dry-run` 和 maintenance/status 命令不受影响。

`--pause-prefetch`（别名 `--pause-batch-ocr`）写
`.llm-wiki/batch-prefetch.pause`，只停止并禁止新建 Phase-1 worker。已存在
`stage_1_3_done` 的书仍可进入 Stage 2.2/serial spine；遇到第一本仍需 Phase 1
的书时以 exit 76 干净让出。`--resume-prefetch`（别名
`--resume-batch-ocr`）只清这个 marker；`--resume-batch` 会同时清 full 和
prefetch 两类 marker。

Ctrl-C/SIGTERM 命中 batch 协调器时也会建立 pause marker，并清理后台进程组。

若 `--batch-status` 显示某个失败/人为停在 Stage 2 的 source 长期占有 spine，
应优先重跑同一本书。只有确认不再恢复它、并检查过潜在部分写入后，才可显式：

```bash
python3 "$SKILL_DIR/scripts/ingest.py" --abandon-spine <status显示的8位hash>
```

不要直接删除 reservation 文件；命令会核对 owner hash，避免放错锁。
若仍有进程持有 `.llm-wiki/ingest.lock`，该命令会拒绝执行。

## `--stop-after-stage` 防误用

`--stop-after-stage` 仅允许单文件诊断/预取。多文件 batch 与它组合会在 context
probe 和任何 OCR 之前直接报错，避免把整批任务误变成“OCR-only batch”。
自动后台 worker 使用内部 `--batch-extract-worker` 模式，不依赖公开 stop flag。
公开 `--no-project-lock` 也只允许和 `--stop-after-stage 0` 或 `1.5` 组合；它不能
被误用于完整写入。

## 进阶：显式预取下一本 Stage 2.2

普通 batch 自动重叠 Phase 1，但不会自动并发不同书的 conversation handoff。若需要
隐藏下一本的 Stage 2.2，可在另一条受控对话流中运行单文件：

```bash
python3 "$SKILL_DIR/scripts/ingest.py" \
  --stop-after-stage 1.5 --no-project-lock \
  "raw/Book/<book N+1>.pdf"
```

每次 exit 101 仍需 fresh subagent 作答并重跑，直到 Stage 2.2 完成。Stage 2.2
只分析本书内容；真正读取现有 Wiki 并建立跨书关联的是之后的 Stage 2.3，因此
当前书写入完成后再进入 N+1 spine，仍会读取最新 Wiki。

## 关键不变量

- minerU 同时最多 1 个。
- 跨进程 caption round 同时最多 1 个；round 内远程调用默认最多 4 个。
- Phase-1 worker 同时最多 2 个，且按完整文件列表顺序取得 minerU。
- Stage 2.3+ 跨书同时最多 1 个。
- ProjectLock 不覆盖 OCR、caption、Stage 2.2 或空闲等待；single 与 batch 使用
  相同边界。
- durable spine reservation 跨 exit 101 保持 source owner，不允许 handoff
  等待期换书。
- Stage 2.4 保持并行，但每波未完成 handoff 不超过 `--parallel`；Stage 2.2 串行。
- ConversationPending exit 101 不清理健康 detached worker；显式 pause/信号才清理。

## 相关

- [[batch-digest-loop]] — batch 驱动与恢复
- [[delegate-mode]] — conversation handoff
- [[conversation-mode-agent-workflow]] — 单书逐阶段作答
