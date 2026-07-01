# Dedup 设计：两种去重的职责划分

improved-wiki 有**两种职责不同、不可互换**的去重。它们名字不同、时机不同、侧重点不同——不要混淆，也不要合并。

## 命名

| 种类 | 中文名 | 英文名 | 模块 | 入口 |
|---|---|---|---|---|
| ① 消化时 | **源内去重** | intra-source dedup | `_stage_2_5_dedup.py` | `stage_2_5_dedup()` |
| ② 检查时 | **跨源去重** | cross-source dedup | `cross_source_dedup.py`（CLI）+ `_dedup.py`（引擎）| `cross_source_dedup.py` CLI |

## 职责对比

| 维度 | 源内去重（Stage 2.4 收尾子步，原 2.5） | 跨源去重（lint sweep） |
|---|---|---|
| 范围 | 单源——只看本次 LLM 生成的 file_blocks | 全 wiki——跨所有已消化源 |
| 目标问题 | LLM 在**同一本书内**把同一概念起两个名 | 跨源累积——多次 ingest 把同一主题命名不同 |
| 时机 | 写盘前（3.1 之前），是 file_blocks 的**过滤器** | 离线，用户手动 `--dedup` 触发，在多次 ingest 之后 |
| 速度要求 | 必须快（inline，阻塞 ingest）| 可慢（离线，LLM 语义扫描）|
| 激进度 | **保守**——页面还没写，误合并=丢数据，且无备份 | **彻底**——有 backup + report，可回滚，可大胆合并 |
| 跨引用改写 | **不做**——页面尚未落盘，没有 `[[wikilink]]` 可改 | **必须做**——全 wiki 重写 `[[old-slug]]` + `related:` 指向 canonical |
| 检测方法 | embedding 语义候选（cosine ≥0.82，复用 `_dedup_embedding`；无回退，缺 embedding stack 则 raise）+ LLM 逐组确认 | LLM 语义检测（NashSU `dedup.ts` 移植）|
| 输出 | 静默过滤——dup 块直接不写盘 | backup 目录 + JSON report，供人复核 |
| 可逆性 | 不可逆（dup 页根本没生成）| 可逆（有 backup，可还原）|

## 核心区别一句话

- **源内去重**：预防性、单源、保守过滤。问"LLM 这次有没有重复造轮子"——写盘前把 dup 块踢掉，**不碰跨引用**。
- **跨源去重**：治疗性、全 wiki、LLM 语义合并 + 跨引用修正。问"整个 wiki 累积了哪些重复"——合并后**必须**全 wiki 重写链接。

## 跨源去重的内部结构

```
cross_source_dedup.py        # CLI + 编排：LLM 语义检测 + 合并，backup，report
  └─ _dedup.py               # 引擎：LLM 语义检测 + 合并（NashSU dedup.ts 移植）
```

NashSU `dedup.ts` parity：纯 LLM 语义检测，无确定性预筛。三阶段：
1. `extract_entity_summary` — 纯数据提取（slug, title, description, tags），无 LLM
2. `detect_duplicate_groups` — LLM 识别同主题 slug 组（同义/中英/缩写/单复数）
3. `merge_duplicate_group` — LLM body 合并 + 确定性 frontmatter union + 跨引用重写 + backup

## 何时用哪个

- **ingest 时**：自动跑源内去重（Stage 2.4 收尾子步，原 2.5），无需人工干预。
- **积累了一批 ingest 后**：手动跑跨源去重清理全 wiki：
  ```bash
  python3 scripts/cross_source_dedup.py --project /path/to/wiki            # LLM 语义去重
  python3 scripts/cross_source_dedup.py --dry-run                          # preview only
  ```

## 已知遗留：跨书历史重复 slug 变体

Stage 2.3 的标题 Jaccard 匹配曾漏判重音/标点变体（如 "Thévenin's" vs "Thevenin's"），已修（见 `known-issues.md`）——但只防未来新重复。已存在的跨书历史重复（同一概念的多个 slug 变体）是更大的内容去重课题，**不会**被这次修复回溯清理，需要靠上面的跨源去重手动扫一遍。
