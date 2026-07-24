# Naming Conventions — NashSU-aligned

> **权威来源**：NashSU LLM Wiki app 最新版本（`src/lib/ingest.ts`, `lint.ts`, `wiki-page-types.ts`, `wiki-schema.ts`, `graph-filters.ts`）+ improved-wiki `scripts/ingest.py`。
> 旧 karpathy-llm-wiki / HardwareWiki / RadarWiki 的历史惯例不作为参考。

---

## 1. 目录结构

### 1.1 wiki/ 子目录（NashSU 基础类型 + Research 模板类型）

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
├── methodology/   # type: methodology — 方法论（NashSU 支持的顶级 type）
├── media/         # 提取的图片 (镜像 raw/ 结构: <type>/<stem>/，如 Book/Foo/)
├── REVIEW/        # pipeline↔人类握手点 (improved-wiki 扩展)
│   ├── confirm/       # 需要确认的可疑内容
│   ├── suggestion/     # 改进建议
│   ├── missing-page/   # 缺少的重要页面
│   ├── contradiction/  # 内容矛盾
│   └── duplicate/      # 重复内容
├── index.md       # 聚合页：索引
├── overview.md    # 聚合页：概述
└── log.md         # 聚合页：变更日志

# 项目根（wiki/ 之外）：
<project>/schema.md  # page type → directory 映射；放在根目录，不进 wiki/ 扫描
<project>/purpose.md # 可选；项目目标/范围，与 schema 一同注入 LLM 上下文
```

**Schema 驱动路由（NashSU 0.6.5 parity）**：NashSU 通用模板的基础类型是
`entity/concept/source/query/comparison/synthesis/overview`；Research 模板再声明
`thesis/methodology/finding`。improved-wiki 的兼容常量仍允许这些历史目录，但项目
`schema.md` 的 `## Page Types` 表才是权威类型映射。完整的语义 schema（排除
improved-wiki 专用的机器命名 YAML）会注入 Stage 2.2/2.4/2.6/2.9/3.4；可选
`purpose.md` 同时注入。Stage 2.2 只把非 pipeline-managed 类型
（如 finding/methodology/person/decision）作为 `schema_typed_candidates`，并在
Stage 2.4 按解析后的 type→dir 重新裁决，绝不信任 LLM 自报 folder。

**来源**：NashSU 0.6.5 `templates.ts`（通用/Research 场景 schema）、
`wiki-schema.ts`（Page Types 结构化解析与路由校验）、`ingest.ts`
（analysis/generation 全 schema 注入）。

**entity `role:` 字段——已删除，无替代轴**：NashSU 没有 entity `role:` frontmatter 字段（`wiki-schema.ts` 的 frontmatter 是开放的 `Record<string,unknown>`）。improved-wiki 曾自造一个封闭集 `role:` 轴（person/organization/system/standard/model/device），已从生成提示词、Stage 2.2 analyze YAML、`graph.py` 的 by-role 配色模式、以及 invalid-role lint 检查里全部移除。人物/机构/系统的区分**只**靠 schema.md 声明的 typed 文件夹，没有其它机制（封闭集或开放集都没有）。

**Schema 路由——两层机制，同时生效（不是新旧替换关系）**：
1. **Accept-list 门禁**：`schema_folders()` 只消费 `parse_wiki_schema_routing()`
   对 `## Page Types` 表的结构化结果，再与兼容基础目录合并。禁止全文正则扫描；
   `wiki/index.md`/`wiki/log.md` 等正文提及不能泄漏为 phantom folder。
2. **精确路由器**（后加，NashSU `wiki-schema.ts` parity）：`_core.py` 的 `parse_wiki_schema_routing()`（结构化解析 `type→dir` 映射表）+ `schema_route_dir()` + `BASE_TYPE_TO_DIR`，接入 `_stage_3_write.py::_stage_3_1_schema_route()`，由 `_ingest_write.py` 调用。每本书算一次路由表，按 FILE block 的 frontmatter `type` 精确路由到目录。

两层在同一次写盘中都跑：第 1 层管"这个目录能不能收"，第 2 层管"具体该放哪个目录"。**与 NashSU 的刻意分歧**：NashSU 路由不上就丢弃该页；improved-wiki 自动纠正、把页面挪到正确目录（不丢数据，符合 no-silent-fallback 策略）。

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
| `methodology` | `methodology/` | 恒等 |
| `overview` | `wiki/` 根 | 聚合页 |

**来源**：`wiki-page-types.ts:11-21`。

---

## 2. 文件命名

### 2.1 Source 页面

```
wiki/sources/<raw-rel-path>.md
```

**规则**：`<raw-rel-path>` = raw 文件相对于 `raw/` 的路径（去掉 `.pdf` 后缀），**镜像 `raw/` 的目录结构**。

**improved-wiki 实现**：`_stage_3_write.py:_stage_3_1_wiki_path_for_source()` — `raw_file.relative_to(config.raw_root).with_suffix(".md")`。

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

**规则**（**2026-07-02 用户裁决**，取代此前任何按页型"英文 kebab-case"的表述）：**slug 语言 = 源文语言**——中文书 → 中文 slug，英文书 → 英文 kebab-case slug。英文术语放 `title`，**不进 slug**；**例外**：约定俗成的缩写（mti、cfar、dds）可留在 slug 中。**禁止中英双拼混合 slug**。英文 slug 仍用 kebab-case；CJK slug 保留可读字符，不强制转拼音。（历史注记：该裁决曾同样约束 Stage 2.7 query slug；2.7 已于 2026-07-12 整体移除，query 页现仅来自 deep-research/save-chat/process-reviews，slug 规则不变。）

**macOS 限制**：slug 中不得包含 `/`（macOS / Linux 会将 `/` 解释为目录分隔符，无法在文件名中创建）。如果源页 wikilink 引用了含 `/` 的名称（如 `[[热仿真(Cauer/Foster模型)]]`），Stage 2.4 生成时应用 `_` 替代 `/`。参见 `known-issues.md` 中的详细记录。

**冲突处理**：同名 slug 加数字后缀，如 `impedance-matching-2.md`。

### 2.3 聚合页（固定名称，不可改名）

```
wiki/index.md
wiki/overview.md
wiki/log.md
```

**来源**：`ingest.ts:44` — `AGGREGATE_WIKI_PATHS`。这三个文件由 Stage 3.5 在每次 ingest 时维护（见 `_stage_3_write.py::stage_3_5_aggregate_repair`），**不应由用户手写**：

- `log.md`：**程序化追加**变更日志条目（确定性，从不调用 LLM）。
- `index.md` / `overview.md`：默认由 **LLM 整页重写**（喂入磁盘上的权威页面清单 / 内容综合），LLM 调用失败或超出体量上限时回退到确定性追加。

> 注意：与早期文档「纯程序化 append，LLM 永远不应生成」的表述不同——index.md/overview.md 现在确实经过 LLM 重写，需留意内容漂移风险（重写 prompt 已要求逐字保留已有条目描述与 frontmatter）。

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
type: source | concept | entity | query | comparison | synthesis | finding | thesis | methodology
title: "页面标题"
tags: [tag1, tag2]
related: [other-slug, another-slug]
created: 2026-06-14
updated: 2026-06-14
---
```

**来源**：`ingest.ts:634-648`（prompt 指令）；`_ingest_write.py`（improved-wiki 写盘校验）。

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

`media_slug()`（在 `scripts/_paths.py`）返回 raw 文件相对于 `raw/` 的父目录 + stem。

### 5.2 图片文件

```
p<NNNN>-mineru_<md5前8>.<ext>
```

- `NNNN`：PDF 页码（zero-padded，4 位）
- `mineru_<md5前8>`：minerU 提取，图片内容 md5 前 8 位
- 示例：`p0007-mineru_a1b2c3d4.png`

**去重**：按 `page+md5前8` 命名，**不做跨页 sha256 全局去重**（同一图重复出现在不同页会各存一份）。**过滤**：`MINERU_IMG_MIN_WIDTH/HEIGHT` 默认 20px（故意设低，只过滤 1×1/2×2 噪声，保留公式截图）。

**来源**：`_stage_1_2_images.py`；`ingest-stages-mandatory.md` Stage 1.2；`image-caption-strategy.md`。

### 5.3 Caption 文件

```
p0007-mineru_a1b2c3d4.png.caption.txt
```

每图一个 `.caption.txt`，长度 ≥ 20 字符。VLM 生成，与源文本同语言（NashSU `captionImage` parity）。

### 5.4 Manifest

```
wiki/media/<slug>/_manifest.json
```

记录：图片路径 / 来源页 / 尺寸 / sha256。文件名以下划线开头（`_manifest.json`，不是 `manifest.json`）。

**来源**：`_stage_1_2_images.py::_stage_1_2_write_manifest`。

---

## 6. Runtime 状态文件

> **与 NashSU 对齐，统一使用 `.llm-wiki/`**。

```
.llm-wiki/
├── ingest-cache.json           # sha256 → {hash, filesWritten, stages}
├── ingest-queue.json           # 待处理队列
├── ingest-progress/            # <hash[:16]>.json 检查点
├── extract-tmp/<slug>/         # 文本抽取临时文件
├── review-suggestions.json     # Stage 3.4 产物（`_stage_3_4_review.py`）
├── review.json                 # review store（LLM Wiki app 维护，NashSU review-store.ts）
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
      "method": "plain-text" | "zipfile-pptx" | "zipfile-docx" | "mineru-api",   // ocr/low-quality 变体已随质量门于 2026-07-08 移除，存量 cache 可能仍带旧值
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

**来源**：`_core.py::save_cache`（cache schema `{"version": "2", "entries": {...}}`）。

---

## 7. Lint 文件命名

### 7.1 Lint finding ID

```
lint-<N>
```

N 为单调递增计数器（`lint.ts` 实现）。improved-wiki 使用更具体的 ID：`lint-<type>-<page_stem>`。

### 7.2 Lint 页面文件

```
.llm-wiki/lint/<type>-<safe_page>-<NN>.md
```

Lint 页面位于运行时目录（不在 `wiki/` 下）——它们是派生诊断产物，非源知识；放在 `.llm-wiki/` 下避免污染 search/graph 扫描，与 NashSU「lint state 属 runtime，非 wiki 内容」的边界一致。首次运行时旧 `wiki/lint/` 会自动迁移到此处。Lint 页面含 frontmatter `resolved: false`，修复后改 `true`，下次 lint 自动清理。

### 7.3 4 种 structural lint 类型

| type | severity | 说明 |
|------|----------|------|
| `broken-link` | warning | wikilink 目标不存在 |
| `orphan` | info | 无入向链接 |
| `no-outlinks` | info | 无出向链接 |
| `missing-frontmatter` | error | 缺 YAML frontmatter（improved-wiki 扩展） |

**`--strict` 模式**：`broken-link`、`missing-frontmatter` 触发 `exit 1`。

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
| `wiki/REVIEW/<type>/<date>-<source>-<short-slug>.md` | `ingest.py` Stage 3.4 每项一个 md |
| `<runtime>/review-suggestions.json` | Stage 3.4 汇总 JSON（`_stage_3_4_review.py`） |
| `<runtime>/review.json` | review store（LLM Wiki app 维护，NashSU review-store.ts；review 状态维护见 `sweep_reviews.py`） |
| `wiki/REVIEW/_summaries/_audit_<scope>.md` | LLM 审计汇总报告（meta，非 review item） |

**`_summaries/` 约定**：
- 装**跨类审计汇总报告**（如 `_audit_report.md` 总报告、`_audit_<category>.md` 分类明细），是审计 agent 对一批 lint findings 的 verdict/action 统计表，**不是 per-finding review item**。
- `_` 前缀目录 = meta 产物，review-sweep / lint 不当 finding 扫描。
- **不要**写进 `wiki/REVIEW/audit/`——Stage 3.6 质量评分卡已于 2026-06-25 移除（NashSU 对齐），该目录不再自动生成，混入会污染扫描。
- **不要**散落 `wiki/REVIEW/` 根目录——保持根目录只有 `<type>/` 子目录 + `_summaries/`。

---

## 9. 日期格式

| 位置 | 格式 | 示例 |
|------|------|------|
| Frontmatter `created`/`updated` | `YYYY-MM-DD` | `2026-06-14` |
| Log 条目标题 | `## [YYYY-MM-DD] ingest \| <Title>` | `## [2026-06-14] ingest \| ADL8113` |
| Review 文件名 | `<date>-<stem>-<NNN>.md` | `2026-06-14-ADL8113-001.md` |

**来源**：`ingest.ts:302-307`（log 格式）；`_ingest_write.py`（frontmatter 校验）。

---

## 10. 禁止的文件名模式

| 禁止项 | 原因 |
|--------|------|
| `..` 路径穿越 | `isSafeIngestPath()` 拒绝 |
| 绝对路径 | 同上 |
| `<>:"\|?*` 字符 | Windows 不兼容 |
| 尾随空格/点 | 文件系统歧义 |
| 控制字符 / NUL | 文件系统不安全 |

**来源**：`ingest.ts:120-157`。

---

## 11. improved-wiki 与 NashSU 的已知差异

以下为残余差异。已对齐的项不列出。

| 项目 | NashSU 原生 | improved-wiki | 说明 |
|------|------------|---------------|------|
| Raw 布局 | `raw/sources/<type>/<file>` | `raw/<type>/<任意子目录>/<file>` | 刻意设计，人类友好 |
| Raw 命名门禁 | schema 中为自然语言约定 | schema 末尾另有 machine-readable YAML，Stage 0.1 强制 | improved-wiki 扩展；不注入 LLM prompt |
| 聚合页排除 | `index.md` + `log.md`（lint universe） | findings 豁免 `index/log/overview/schema` | 分层模型见 `_lint_suggest.py` |
| Manifest 命名 | 无独立文件 | `_manifest.json` | improved-wiki Stage 1.2 独有产物 |
| Lint 页面 | app UI 直接展示（内存） | `.llm-wiki/lint/<type>-<page>.md` | CLI 场景需要文件化输出；属 runtime 状态，不放入 wiki/ |
| Review 页面 | `review.json`（app UI） | `wiki/REVIEW/<type>/<date>-<stem>-<NNN>.md` + `review.json` | 人类可浏览 + 机器可读双输出 |
