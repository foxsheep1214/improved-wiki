# Deep Research — NashSU v0.6.7 搜寻 → 融合 → 单页写入

本流程以 NashSU v0.6.7 的实际源码 `src/lib/deep-research.ts`、
`anytxt-search.ts`、`optimize-research-topic.ts` 和 `wiki-filename.ts` 为行为基线。
仓库根目录的 `llm-wiki.md` 是抽象模板，不覆盖这些实现细节。

默认链路只有：

```text
已确认 topic / queries
  → 按 source mode 收集 web / AnyTXT 摘要
  → URL 优先去重并全局截到 20 条
  → 仅用摘要 + wiki/index.md 融合
  → 原子写一个 wiki/queries/research-*.md
  → 可选、非阻断地只更新该页 embedding
  → 完成
```

**Deep Research 结果不再送进 source ingest。** 不自动生成 concept/entity/
synthesis/thesis 页，不自动生成 review，不修改 index/log/overview，也不复制到
`raw/queries/`。这样可避免研究页被二次总结，以及它自身的 gaps/references
形成 review 放大回路。

## 与 v0.6.7 的对齐表

| NashSU v0.6.7 | improved-wiki 调用代理 |
|---|---|
| `deepResearchSource = web \| anytxt \| both`，默认 `web` | 使用同名三种模式；未指定时只搜 web |
| 直接输入 topic 时 queries = `[topic]` | 不擅自扩成固定 3–5 个查询 |
| Review 的 `searchQueries` 原样传入 | 非空时逐条原样用于 web；空时回退到 topic |
| Web 每个 query 请求 5 条 | 每个 query 最多保留 5 条 provider 结果 |
| AnyTXT 先改写为 1–3 个关键词查询，最后共取 15 条 | 调用代理先做同样改写，再用 `search_local.py ... --max-results 15` |
| `both` 的各来源并发，以 `Promise.allSettled` 收集 | 可用并行工具同时发起；个别来源失败不丢弃其他成功结果 |
| URL 优先、否则 `source:title:snippet` 去重；忽略大小写；全局最多 20 | 相同 |
| 只把 `[N] title (source) + snippet` 给 LLM | 不读网页正文，不把 URL 放进融合上下文 |
| 只读 `wiki/index.md` 做 wikilink grounding | 相同；缺失时使用空 index |
| 固定系统提示词，正文由 LLM 自由组织 | 相同；不强制固定章节模板 |
| 代码写 frontmatter、H1 和 References | `write_research_page.py` 确定性完成 |
| 研究页不再 `autoIngest` | 默认绝不调用 `ingest.py` |
| embedding 开启时仅 upsert 该页，失败只告警 | 可选运行 page-scoped upsert；失败不撤销研究页 |
| 内存队列最大并发 3 | CLI/对话适配为每个 topic 独立完成；批量时串行写入 |

## 1. 触发与确认

触发语句包括：

- `/improved-wiki deep-research <topic>`
- `deep research <topic>` / `deep-research <topic>`
- `深度研究 <主题>`
- `研究 <主题> 并写入 wiki`

确认规则：

- 用户在命令或自然语言中明确给出 topic，已经构成确认；不要重复追问。
- 用户在 Process Reviews 中选择 **Deep Research**，已经确认该 review 的研究范围。
- 如果 topic 是由 Graph gap、lint finding 或代理主动建议而来，先展示拟定 topic
  和 queries，等用户确认后才开始外部检索与写入。
- v0.6.7 对直接输入的宽泛 topic 不插入澄清步骤。除非用户的意图本身无法
  确定，否则按原 topic 搜索。

来源模式：用户明确指定时服从 `web`、`anytxt` 或 `both`；未指定时使用
v0.6.7 默认值 `web`。不要把“先本地、再网络”设成强制前置流程。
`web`/`anytxt` 分别要求对应能力；`both` 只要其中一项已配置即可启动，并且
只调度已配置的分支——缺少的另一分支本身不算 source error。这与 v0.6.7 的
`hasConfiguredDeepResearchSources`/`collectResearchSources` 一致。

## 2. 确定搜索 queries

### 2.1 直接研究

使用且只使用：

```text
[<topic>]
```

不要自动生成“原理 / 应用 / 挑战 / 对比 / 最新进展”等固定 3–5 查询。

### 2.2 来自 Review

- topic = review 标题，去掉 `Save to Wiki:`、`Create:`、`Research:` 前缀。
- `search_queries` 非空：按存储顺序原样使用。
- `search_queries` 为空：使用 `[topic]`。
- 记录 source review 的路径或 ID，供成功写盘后回填；此时不要提前 resolve。

### 2.3 来自 Graph knowledge gap

读取 `purpose.md` 和 `wiki/overview.md`（缺失即空），将 gap type/title/
description 一起交给 LLM。要求严格输出 4 行：

```text
TOPIC: <一个精确研究主题>
QUERY: <关键词丰富、面向搜索引擎的 query 1>
QUERY: <query 2>
QUERY: <query 3>
```

解析一个 topic 和最多三个 queries；没有有效 query 时回退到 `[topic]`。
展示优化结果并等用户确认。这是 v0.6.7 的 gap 专用优化，不应用到直接输入 topic。

### 2.4 AnyTXT 查询改写

只有 source mode 含 `anytxt` 时执行。把当前 topic/queries 改写为总共 1–3 个
本地全文检索关键词短语：保留专有名词、文件名、技术词、日期、缩写和非英语词；
不要用完整问句；去空、忽略大小写去重、最多三条。改写失败则使用原 queries
按同样规则清理后的结果。

## 3. 收集来源

所有结果统一为：

```json
{
  "title": "...",
  "url": "https://... 或 file:///...",
  "snippet": "...",
  "source": "provider host 或 AnyTXT"
}
```

### 3.1 Web mode

对第 2 节确定的每个 query 调用当前可用的 web search provider，**每个 query
请求 5 条**。只保存 provider 返回的 title、URL、snippet、source。

不要打开搜索结果网页、下载论文/PDF 或用页面正文替换 snippet；v0.6.7 的
Deep Research 融合路径只消费搜索摘要。需要全文研究属于另一个显式工作流，
不能悄悄混入“对齐 NashSU”的结果。

### 3.2 AnyTXT mode（本地 CLI 适配）

`search_local.py` 用项目内 `wiki/` 的 NashSU-style keyword scorer，以及
`raw/` 的 Spotlight/ripgrep sidecar 作为 AnyTXT 的 CLI 替代后端。它返回
相同四字段，source 固定为 `AnyTXT`，URL 为 `file://`：

```bash
python3 "$SKILL_DIR/scripts/search_local.py" \
  "<prepared query 1>" "<prepared query 2>" "<prepared query 3>" \
  --project <wiki-root> --max-results 15 --json
```

它按 query 顺序搜索、URL 优先去重，并对全部本地结果执行一个 15 条全局上限。
本地 helper 已负责生成匹配摘要；调用代理不要再打开原文件扩写上下文。

### 3.3 Both mode、顺序、错误与全局去重

- Web query 调用与 AnyTXT 调用可并发发起。
- 合并顺序保持 v0.6.7 的调用数组顺序：各 web query 结果在前，AnyTXT
  结果在后；不是按完成先后排序。
- 对每个结果计算 key：有 URL 时用 `url.lower()`；URL 为空时用
  `(source + ":" + title + ":" + snippet).lower()`。
- 首次出现者保留，所有来源合计最多 **20** 条。不要按来源另设 20 条上限。
- 某个调用失败但仍有结果：保留成功结果并继续融合，同时在最终报告中列出错误。
- 所有调用得到 0 条且至少一个调用失败：任务失败，不写页面。
- 所有调用干净地得到 0 条：任务完成为 `No research sources found.`，不写页面。

## 4. 融合

只读取 `wiki/index.md` 作为现有 wiki grounding；不要把整库正文、overview、
raw 文件或网页正文加入默认融合上下文。

按最终去重顺序构造：

```text
[1] **<title>** (<source>)
<snippet>

[2] **<title>** (<source>)
<snippet>
```

注意：这里不含 URL。URL 只在写入时由代码生成 References。

系统提示词保持 v0.6.7 的内容和顺序；在第二行空行后加入基于 topic 的
mandatory output-language directive：

```text
You are a research assistant. Synthesize the collected research sources into a comprehensive wiki page.

<output-language directive>

## Cross-referencing (IMPORTANT)
- The wiki already has existing pages listed in the Wiki Index below.
- When your synthesis mentions an entity or concept that exists in the wiki, ALWAYS use [[wikilink]] syntax to link to it.
- For example, if the wiki has an entity 'anthropic', write [[anthropic]] when mentioning it.
- This is critical for connecting new research to existing knowledge in the graph.

## Writing Rules
- Organize into clear sections with headings
- Cite sources using [N] notation
- Note contradictions or gaps
- Suggest additional sources worth finding
- Neutral, encyclopedic tone

## Existing Wiki Index (link to these pages with [[wikilink]])
<wiki/index.md，若存在>
```

用户消息严格按以下形状：

```text
Research topic: **<topic>**

## Research Sources

<search context>

Synthesize into a wiki page.
```

保存 LLM synthesis 原文；不要让调用代理另行重写、强制 Overview/Key
Findings 等固定章节，或替 LLM 添加 References/frontmatter。只允许写入器移除
`<think>...</think>`、`<thinking>...</thinking>` 及未闭合 thinking 尾段。

## 5. 确定性写入

把 synthesis 和最终 20 条以内的来源 JSON 暂存在
`/tmp/codex-work/<task>/`，再调用：

```bash
python3 "$SKILL_DIR/scripts/write_research_page.py" \
  --project <wiki-root> \
  --topic "<confirmed topic>" \
  --synthesis-file /tmp/codex-work/<task>/synthesis.txt \
  --sources-file /tmp/codex-work/<task>/sources.json
```

helper 再执行一次 URL/fallback-key 去重和 20 条上限作为写入门禁，并原子写到
`wiki/queries/`。输出唯一的项目相对路径。零来源时返回 3 且不写文件。

页面格式严格为：

```markdown
---
type: query
title: "Research: <topic；双引号转义>"
created: <当前本地日历日期 YYYY-MM-DD>
origin: deep-research
tags: [research]
---

# Research: <topic>

<synthesis 原文，仅去 thinking blocks>

## References

1. [<title>](<url>) — <source>
2. ...
```

文件名是 `makeQueryFileName("research-" + topic)` 的移植：topic 先 NFKC；
空白转 `-`；只留 Unicode 字母/数字和 ASCII `-`；合并/修剪连字符；小写；
截到 50 个 Unicode code points；空则 `query`。后缀使用 UTC
`-YYYY-MM-DD-HHMMSS.md`，而 frontmatter `created` 使用本地日历日期。

写入成功前不得修改 review 状态。写入成功即是 Deep Research 的核心完成点。

## 6. 可选的单页 embedding

只有项目已明确启用并配置 embedding 时，才对刚写页面做一次：

```bash
python3 "$SKILL_DIR/scripts/build_embeddings.py" \
  --project <wiki-root> upsert --page wiki/queries/<saved-file>.md
```

这是非阻断增强：失败时保留已成功写入的研究页，记录 warning，并在结果中说明。
它与 ingest Stage 3.7 的强制完成门禁不是同一个契约。

## 7. Review 回填与结果报告

如果任务来自 review，仅在研究页写入成功、且路径已确定后，把原 review 设为
resolved，reason 精确写为：

```text
Research saved: wiki/queries/<saved-file>.md
```

搜索失败、融合失败或写入失败时 review 保持 pending。

最终只报告事实：

- 研究页相对路径；
- 实际采用的 source mode 和 queries；
- web / AnyTXT / 去重后总来源数；
- 被保留的 partial source errors；
- 单页 embedding 是成功、跳过还是告警；
- source review 是否在写盘后 resolved。

不要报告“消化出的新 concept/entity/synthesis 页面”或“新生成 reviews”，因为
v0.6.7 默认 Deep Research 不产生这些工件。

## 8. 边界与兼容能力

- 一个显式 topic 对应一个独立研究页；不从 synthesis 或新 review 自动链式派生
  下一轮研究。
- improved-wiki 仍允许 `ingest.py wiki/queries/<file>.md`，用于历史数据和用户
  **另行明确要求**把某个 query page 当作 source 再消化的兼容场景。它不是
  v0.6.7 Deep Research 默认步骤，Deep Research 不得自行调用。
- Save Chat to Wiki 有自己的 auto-ingest 契约；不要据此推断 Deep Research
  也应 auto-ingest。
- 如果运行环境没有该 source mode 可使用的任何能力，不要悄悄换 mode。报告
  缺失配置，或让用户选择另一个模式；`both` 中仍有一个已配置分支时则按该分支
  正常运行。
