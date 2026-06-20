# Naming Conventions — NashSU-aligned

> **权威来源**：NashSU LLM Wiki app v0.4.23 (`src/lib/ingest.ts`, `lint.ts`, `wiki-page-types.ts`, `wiki-schema.ts`) + improved-wiki `scripts/ingest.py`。
> 旧 karpathy-llm-wiki / HardwareWiki / RadarWiki 的历史惯例不作为参考。

---

## 1. 目录结构

### 1.1 wiki/ 子目录（NashSU 9 种 page type）

```
wiki/
├── sources/       # type: source     — 来源摘要页
├── concepts/      # type: concept    — 概念页
├── entities/      # type: entity     — 实体页
├── queries/       # type: query      — 开放问题
├── comparisons/   # type: comparison — 对比分析
├── synthesis/     # type: synthesis  — 跨领域综合
├── findings/      # type: finding    — 研究发现
├── thesis/        # type: thesis     — 工作假说
├── media/         # 提取的图片 (镜像 raw/ 结构: book/<slug>/ 等)
├── REVIEW/        # pipeline↔人类握手点 (improved-wiki 扩展)
│   ├── confirm/       # 需要确认的可疑内容
│   ├── suggestion/     # 改进建议
│   ├── missing-page/   # 缺少的重要页面
│   ├── contradiction/  # 内容矛盾
│   ├── duplicate/      # 重复内容
│   ├── missing-synthesis/   # lint: 可以合成综述了
│   ├── missing-finding/     # lint: 可以提炼发现了
│   ├── missing-thesis/      # lint: 可以提出假说了
│   └── missing-methodology/ # lint: 可以蒸馏方法论了
├── lint/          # lint findings (improved-wiki 扩展)
├── index.md       # 聚合页：索引
├── overview.md    # 聚合页：概述
├── log.md         # 聚合页：变更日志
└── schema.md      # page type → directory 映射 (NashSU 对齐)
```

**来源**：`wiki-page-types.ts:1-21`（9 种 type 枚举 + `WIKI_TYPE_DIRS` 映射）；`ingest.ts:44`（3 个聚合页）。

### 1.2 raw/ 子目录（improved-wiki 布局）

```
raw/
├── Book/
├── Paper/
├── Datasheet/
├── Applicationnote/
├── Designexample/
├── Presentation/
├── Standard/
└── News/
```

**命名规范**：首字母大写，其余小写（Titlecase）。

**来源**：`_core.py:FOLDER_TO_TEMPLATE`。improved-wiki 使用 `raw/<type>/<任意子目录>/<file>`（无 `sources/` 中间层），与 NashSU 原生的 `raw/sources/<type>/` 布局不同（见 Section 12 差异表）。

### 1.3 Type → 目录映射

| type | 目录 | 说明 |
|------|------|------|
| `source` | `sources/` | 复数→单数（唯一的映射例外） |
| `entity` | `entities/` | 复数→单数 |
| `concept` | `concepts/` | 恒等 |
| `query` | `queries/` | 恒等 |
| `comparison` | `comparisons/` | 恒等 |
| `synthesis` | `synthesis/` | 恒等 |
| `finding` | `findings/` | 恒等 |
| `thesis` | `thesis/` | 恒等 |

**来源**：`wiki-page-types.ts:11-21`。

---

## 2. 文件命名

### 2.1 Source 页面

```
wiki/sources/<raw-rel-path>.md
```

**规则**：`<raw-rel-path>` = raw 文件相对于 `raw/` 的路径（去掉 `.pdf` 后缀），**镜像 `raw/` 的目录结构**。

**improved-wiki 实现**：`ingest.py:wiki_path_for_source()` — `raw_file.relative_to(config.raw_root).with_suffix(".md")`。

```
# 示例
raw/Book/Free_Energy_Principle - 2025 - Proulx.pdf
→ wiki/sources/Book/Free_Energy_Principle - 2025 - Proulx.md

raw/Datasheet/ADI/ADL8113.pdf
→ wiki/sources/Datasheet/ADI/ADL8113.md
```

### 2.2 Concept / Entity / 其他页面

```
wiki/concepts/<slug>.md
wiki/entities/<slug>.md
wiki/queries/<slug>.md
...
```

**规则**：`slug` 由 LLM 在 Stage 2.3 生成，**必须用 kebab-case**（`ingest.ts` prompt 指令）。CJK 标题保留可读字符，不强制转拼音。

**macOS 限制**：slug 中不得包含 `/`（macOS / Linux 会将 `/` 解释为目录分隔符，无法在文件名中创建）。如果源页 wikilink 引用了含 `/` 的名称（如 `[[热仿真(Cauer/Foster模型)]]`），Stage 2.3 生成时应用 `_` 替代 `/`。参见 `known-issues.md` 中的详细记录。

**冲突处理**：同名 slug 加数字后缀，如 `impedance-matching-2.md`。

### 2.3 聚合页（固定名称，不可改名）

```
wiki/index.md
wiki/overview.md
wiki/log.md
```

**来源**：`ingest.ts:44` — `AGGREGATE_WIKI_PATHS`。这三个文件**由 Stage 3.4 程序化 append，LLM 永远不应生成它们**。

---

## 3. Wikilink 规范

### 3.1 基本格式

```
[[target]]
[[target|display text]]
```

**来源**：`lint.ts:43` — regex `\[\[([^\]|]+?)(?:\|[^\]]+?)?]]`。

### 3.2 解析规则（不区分大小写 + 双索引）

| 规则 | 示例 |
|------|------|
| 完整相对路径 | `[[concepts/impedance-matching]]` |
| 纯 stem（仅当全局唯一） | `[[impedance-matching]]` |
| 不区分大小写 | `[[Transformer]]` = `[[transformer]]` |
| 管道显示文本 | `[[concepts/foo\|显示名]]` |

**来源**：`lint.ts:46-65` — `buildSlugMap()` 双索引 + `toLowerCase()` 不区分大小写。

### 3.3 禁止的写法

| 错误 | 原因 |
|------|------|
| `[[雷达原理]]` 当实际文件是 `雷达原理 - 2009 - 张光义.md` | 必须用完整 stem |
| `[[wiki/concepts/foo]]` | 不需要 `wiki/` 前缀 |
| `[[foo.md]]` | 不需要 `.md` 扩展名 |

---

## 4. Frontmatter 规范

### 4.1 所有页面必填字段

```yaml
---
type: source | concept | entity | query | comparison | synthesis | finding | thesis
title: "页面标题"
domain: circuit-fundamentals | power-electronics | thermal-management | emc | signal-integrity | digital-circuits | pcb-design | rf-microwave | radar-systems | analog-circuits | semiconductor-devices | reliability-engineering | general
tags: [tag1, tag2]
related: [other-slug, another-slug]
created: 2026-06-14
updated: 2026-06-14
---
```

**`domain` 字段**（必填，concept/entity 页面必须包含）：
- 指明概念所属领域，用于消歧义和跨领域链接
- 取值见 `references/domains.md`
- 跨领域通用概念标注 `general`
- source 页面可从 `domain: general` 或省略

**来源**：`ingest.ts:634-648`（prompt 指令）；`parse_and_write.py:104-111`（improved-wiki 校验）。

**格式约束**：
- 文件第一行必须是 `---`，前置 matter 后紧跟 `---`
- `type` 必须匹配所在目录（NashSU schema 校验强制）
- `title` 含冒号时加引号
- `tags` 和 `related` 用内联数组 `[a, b, c]`
- `related` 用**裸 stem**（无 `wiki/`、无 `.md`、无 `[[]]`）——这是 NashSU 格式
- `created` / `updated` 不用引号，格式 YYYY-MM-DD
- 不要在 frontmatter 外加 ` ```yaml ` 围栏

### 4.2 Source 页面额外字段（NashSU schema.md）

```yaml
authors: [name1, name2]
year: 2025
url: ""
venue: ""
sources: ["raw/Book/原文件.pdf"]
```

### 4.3 Finding 页面额外字段

```yaml
source: "[[source-slug]]"
confidence: low | medium | high
replicated: true | false | null
```

### 4.4 各 type 的 `sources` 字段

| type | `sources` 内容 |
|------|---------------|
| `source` | 本 raw 文件路径 |
| `concept` | 来源 raw 文件路径（继承） |
| `entity` | 来源 raw 文件路径（继承） |
| 其他 type | 引用相关 source 路径 |

---

## 5. 媒体文件命名

### 5.1 目录

```
wiki/media/<raw_subpath>/<source_stem>/
```

**规则**：media/ 目录结构镜像 raw/ 结构，与 sources/ 对齐：
- `raw/Book/Foo.pdf` → `wiki/media/Book/Foo/`
- `raw/Datasheet/05_AMP/ADL8113.pdf` → `wiki/media/Datasheet/05_AMP/ADL8113/`

`_media_slug()` 返回 raw 文件相对于 `raw/` 的父目录 + stem。

### 5.2 图片文件

```
p<page_number>-fig<figure_index>.<ext>
```

- `page_number`：PDF 页码（0-based）
- `figure_index`：该页内图片序号（0-based）
- 示例：`p0-fig1.png`、`p123-fig4.jpeg`

**去重**：sha256 相同只存一份。**过滤**：< 100×100px 的装饰/logo 剔除。

**来源**：`ingest.py:975`；`ingest-stages-mandatory.md:62-65`。

### 5.3 Caption 文件

```
p123-fig4.png.caption.txt
```

每图一个 `.caption.txt`，长度 ≥ 20 字符。VLM 生成，中文优先。

### 5.4 Manifest

```
wiki/media/<slug>/_manifest.json
```

记录：图片路径 / 来源页 / 尺寸 / sha256。文件名以下划线开头（`_manifest.json`，不是 `manifest.json`）。

**来源**：`ingest.py:914`。

---

## 6. Runtime 状态文件

> **与 NashSU 对齐，统一使用 `.llm-wiki/`**。`.iwiki-runtime/` 已于 2026-06-16 废弃——`_paths.detect_runtime_dir()` 会在首次访问时自动迁移旧目录内容。

```
.llm-wiki/
├── ingest-cache.json           # sha256 → {hash, filesWritten, stages}
├── ingest-queue.json           # 待处理队列
├── ingest-progress/            # <hash[:16]>.json 检查点
├── extract-tmp/<slug>/         # 文本抽取临时文件
├── review-suggestions.json     # Stage 2.3.5 产物
├── review.json                 # Stage 3.5 产物（run_review_suggestions.py）
├── lint-cache.json             # lint 结果缓存
├── lint-semantic.json          # 语义 lint 结果
├── ingest-lock                 # 并发锁
└── lancedb/                    # 向量嵌入索引
```

### 6.1 Cache entry schema

```json
{
  "entries": {
    "relative/raw/path.pdf": {
      "hash": "<sha256>",
      "timestamp": 1718300000000,
      "filesWritten": ["wiki/sources/xxx.md", "wiki/concepts/yyy.md", ...],
      "method": "pymupdf" | "mineru-ocr",
      "template": "digest-book.md",
      "fileBlockCount": 15,
      "stages": {
        "global_digest_keys": 6,
        "chunks_analyzed": 3,
        "file_blocks_generated": 15,
        "images_extracted": 42,
        "images_captioned": 42,
        "images_injected": 42,
        "review_items": 5
      }
    }
  }
}
```

**来源**：`ingest.py:2630-2647`。

---

## 7. Lint 文件命名

### 7.1 Lint finding ID

```
lint-<N>
```

N 为单调递增计数器（`lint.ts` 实现）。improved-wiki 使用更具体的 ID：`lint-<type>-<page_stem>`。

### 7.2 Lint 页面文件

```
wiki/lint/<type>-<safe_page>-<NN>.md
```

Lint 页面含 frontmatter `resolved: false`，修复后改 `true`，下次 lint 自动清理。

### 7.3 6 种 structural lint 类型

| type | severity | 说明 |
|------|----------|------|
| `broken-link` | warning | wikilink 目标不存在 |
| `orphan` | info | 无入向链接 |
| `no-outlinks` | info | 无出向链接 |
| `missing-frontmatter` | error | 缺 YAML frontmatter（improved-wiki 扩展） |
| `missing-domain` | warning | concept/entity 页面缺少 `domain` 字段（Plan B 扩展） |
| `invalid-domain` | warning | `domain` 值不在允许列表中（Plan B 扩展） |

**`--strict` 模式**：`broken-link`、`missing-frontmatter`、`missing-domain` 触发 `exit 1`。

**来源**：`lint.ts:5`。

---

## 8. Review 文件命名

### 8.1 Review item ID

```
review-<N>
```

N 为单调递增计数器（`review-store.ts:10`）。

### 8.2 5 种 review 类型

| type | 说明 |
|------|------|
| `confirm` | 需人工确认 |
| `suggestion` | 改进建议 |
| `missing-page` | 缺页 |
| `contradiction` | 页面间矛盾 |
| `duplicate` | 内容重复 |

### 8.3 Review 产物

| 文件 | 来源 |
|------|------|
| `wiki/REVIEW/<type>/<date>-<source>-<short-slug>.md` | `ingest.py` Stage 2.3.5 每项一个 md |
| `<runtime>/review-suggestions.json` | Stage 2.3.5 汇总 JSON |
| `<runtime>/review.json` | `run_review_suggestions.py` Stage 3.5 产物 |

---

## 9. Stage Compliance 文件

(removed — validate_ingest.py covers stage compliance)

记录每个 source 的 15 阶段完成状态（Markdown 表格）。每行含 Stage 编号、状态（✅/❌/⚪）、详情。

**来源**：`ingest.py:1436-1528`。

---

## 10. 日期格式

| 位置 | 格式 | 示例 |
|------|------|------|
| Frontmatter `created`/`updated` | `YYYY-MM-DD` | `2026-06-14` |
| Log 条目标题 | `## [YYYY-MM-DD] ingest \| <Title>` | `## [2026-06-14] ingest \| ADL8113` |
| Review 文件名 | `<date>-<stem>-<NNN>.md` | `2026-06-14-ADL8113-001.md` |

**来源**：`ingest.ts:302-307`（log 格式）；`parse_and_write.py`（frontmatter 校验）。

---

## 11. 禁止的文件名模式

| 禁止项 | 原因 |
|--------|------|
| `..` 路径穿越 | `isSafeIngestPath()` 拒绝 |
| 绝对路径 | 同上 |
| `<>:"\|?*` 字符 | Windows 不兼容 |
| 尾随空格/点 | 文件系统歧义 |
| 控制字符 / NUL | 文件系统不安全 |

**来源**：`ingest.ts:120-157`。

---

## 12. improved-wiki 与 NashSU 的已知差异

以下为 2026-06-14 全面审计后的残余差异。标注"已对齐"的项不再列出。

| 项目 | NashSU 原生 | improved-wiki | 说明 |
|------|------------|---------------|------|
| Raw 布局 | `raw/sources/<type>/<file>` | `raw/<type>/<任意子目录>/<file>` | 刻意设计，人类友好 |
| 聚合页排除 | `index.md` + `log.md` | 额外排除 `schema.md` + `overview.md` | NashSU 无 schema.md |
| Manifest 命名 | 无独立文件 | `_manifest.json` | improved-wiki Stage 1.2 独有产物 |
| Lint 页面 | app UI 直接展示 | `wiki/lint/<type>-<page>.md` | CLI 场景需要文件化输出 |
| Review 页面 | `review.json`（app UI） | `wiki/REVIEW/<type>/<date>-<stem>-<NNN>.md` + `review.json` | 人类可浏览 + 机器可读双输出 |
| 页面合并 | 3-layer LLM merge | 同 NashSU（2026-06-14 实现） | — |
| 路径安全 | 8 项 `isSafeIngestPath` | 同 NashSU（2026-06-14 实现） | — |
| 栅栏感知解析 | CommonMark fence tracking | 同 NashSU（2026-06-14 实现） | — |
| CRLF 规范化 | `\r\n` → `\n` | 同 NashSU（2026-06-14 实现） | — |
| 内容清理 | `ingest-sanitize.ts` | `sanitize_ingested_content()` | — |
| 页面历史备份 | `.llm-wiki/page-history/` | 同 NashSU（2026-06-14 实现） | — |
| 动态 token 预算 | 4 层缩放 | `compute_max_tokens()` | — |
| 内联 embedding | Stage 6 auto-run | `_auto_embed_new_pages()` | — |
| Lint orphan 检测 | 无条件 | 同 NashSU（2026-06-14 对齐） | — |
| Slug 优先级 | last-write-wins | 同 NashSU（2026-06-14 对齐） | — |
