# Improved-wiki 时间记录契约

本契约把“页面内容时间”“完整消化历史”“当前完成状态”分开，避免再用一个
`created`、`updated` 或 `wiki/log.md` 日期同时回答不同问题。

## 1. 五类记录及权威性

| 记录 | 含义 | 权威来源 |
|---|---|---|
| `created` | 页面第一次逻辑创建的本地日历日期 | 页面 frontmatter；一旦建立只允许向更早的证据纠正 |
| `updated` | 页面正文/知识内容最后一次实质变化的本地日历日期 | 页面 frontmatter；只投影消化时间不得改它 |
| `first_ingested_at` | 该 source 第一次成功完成全流程消化的精确时刻 | `.llm-wiki/ingest-events.jsonl` 的最早 `ingest_completed` 投影 |
| `last_ingested_at` | 该 source 最近一次成功完成全流程消化的精确时刻 | 同一账本的最新 `ingest_completed` 投影 |
| `ingested` | 当前 source/hash 的产物是否完成并可跳过 | `<hash>.stages.json` 数值 marker |

`.llm-wiki/ingest-events.jsonl` 是消化**历史**唯一权威；source 页的
`first_ingested_at` / `last_ingested_at` 和 `wiki/log.md` 都是可重建投影。
`ingested` 仍是**当前状态**权威，两者职责不同，不能互相替代。

## 2. 时间格式

- 页面 `created` / `updated`：`YYYY-MM-DD`。
- 完成事件与 source 页 ingest 字段：RFC 3339，必须带 UTC offset，精确到毫秒，
  例如 `2026-08-14T10:34:43.334+08:00`。
- marker 继续存 epoch milliseconds，且必须与事件 `completed_at_ms` 完全相同。

## 3. Run 语义

- `task_id` 继续稳定绑定 `source identity + source hash`，用于恢复合同。
- 每次显式新消化使用新的 UUID `run_id`。
- 同一未完成任务的崩溃恢复、conversation handoff 和普通 resume 复用原
  `run_id`。
- 同 hash 经 `--delete` 后重消化也必须得到新 `run_id` 和新完成事件。
- 同一 `(event, run_id)` 重放幂等；内容不同则硬失败，禁止覆盖历史。

## 4. 完成顺序

```
页面/媒体/review/cache 完整性门禁
  → Stage 3.7 touched-page embeddings 成功
  → 冻结本 run 的 completed_at
  → 投影 source 页 first/last 时间（不改 updated）
  → 向 wiki/log.md 投影 run_id 完成记录
  → 原子追加 ingest_completed 事件（历史 commit point）
  → 用同一 run_id / completed_at_ms 写 ingested marker
```

Stage 3.3 只保证 `wiki/log.md` 存在；不得在 embedding 成功前写
“INGEST COMPLETED”。若在事件 commit 后、marker 前崩溃，resume 以同一
`run_id` 幂等重放投影并补 marker。

四个运维时间字段不会送入 embedding 模型，因此最终完成投影不会让已经成功
写入的语义索引失配，也不会因纯时间变化制造无意义向量更新。

## 5. 内容更新时间规则

- 新页：`created = updated = 当天`。
- 正文发生实质变化：只更新 `updated`。
- 仅数组合并、完成时间投影或同内容重消化：不更新 `updated`。
- `--delete` 不删除事件账本；重消化完成时由最早事件恢复 `created` 和
  `first_ingested_at`。删除前还会保存一次 identity 级 source 页时间/内容 hash
  快照：新页归一化内容相同时恢复旧 `updated`，内容变化时只恢复 `created`、保留
  新 `updated`；快照在该 run 成功完成后清除。

## 6. Repair 规则

媒体等修复写独立 `repair_completed` 事件，不计入 first/last full ingest。
修复后恢复原 `ingested` marker 的精确值和 payload；不得把修复时间伪装为最近
一次完整消化时间。

## 7. 查询

```bash
python3 "$SKILL_DIR/scripts/ingest_history.py" \
  --project "$WIKI_ROOT" list --sort last --order desc --limit 10

python3 "$SKILL_DIR/scripts/ingest_history.py" \
  --project "$WIKI_ROOT" list --sort first --order asc --limit 10
```

`list` 只统计 `ingest_completed`，不会把 repair 混入“最后一次消化”。

## 8. 旧项目一次性迁移

先预览，再显式写入：

```bash
python3 "$SKILL_DIR/scripts/ingest_history.py" \
  --project "$WIKI_ROOT" migrate

python3 "$SKILL_DIR/scripts/ingest_history.py" \
  --project "$WIKI_ROOT" migrate --apply
```

迁移从两类现有证据恢复：旧 `wiki/log.md` 的 `— INGEST` block（可能只有日期
精度）和所有 `.stages.json` 的精确 `ingested` marker。marker 优先从 task manifest
恢复 source/run 身份；早于 task manifest 的 marker 从 `ingest-cache.json` 的 hash、
source 和 `filesWritten` 恢复。任何已完成 marker 若无法唯一映射，迁移硬失败，不能
静默漏记。同 source/hash 同日时只保留精度更高的 marker，避免把一次完成计为两次。

迁移还会以现存 `raw/`、source 页及 cache hash 统一历史路径的大小写和已确认改名
（例如 `raw/book`→`raw/Book`）；只有唯一证据才会归并，hash 映射有歧义时不猜测。

旧 Stage 3.3 在整条流水线完成前就写日志，且当时没有 `run_id`；因此同
source/hash、相邻不超过 1 小时的旧 block 按 retry artifact 合并，保留较晚时间，
并在迁移事件的 `legacy_log_record_count` / `legacy_log_first_at` 中保留推断依据。
超过 1 小时的记录不自动合并。这是对无 run ID 历史的保守推断；迁移预览会报告
合并数，且不会虚构日志或 marker 都未记录的历史。

迁移会投影 source 页。完成后显式执行一次 full embedding rebuild，使旧向量行
也采用“排除运维时间字段”的新归一化合同；新 ingest 已自动遵守该合同。
