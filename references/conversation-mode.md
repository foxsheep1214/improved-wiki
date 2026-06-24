# Conversation Mode — Direct LLM Execution

本模式描述如何在当前对话中直接执行 improved-wiki 的消化流程，**无需脚本、无需 API key、无需 delegate 往环**。

适用于：当前对话本身就是 LLM（Claude Opus/GPT-4 等），可直接处理文本生成任务。

---

> **例外**：图片 caption（Stage 1.3）走 MiniMax VLM（需 `MINIMAX_CN_API_KEY`）；Embedding（Stage 3.7）可选，独立配置（`EMBEDDING_BASE_URL`），不走 MiniMax。round iv（2026-06-22）起文本生成只有 conversation 一条路径，无 direct API 分支——wikilink enrichment 也已改为走 conversation（批量：每次 ingest 汇总所有新写页面问一次，而非逐页问）。

## Mode Comparison

| 维度 | Conversation Mode |
|------|-------------------|
| LLM 调用 | 当前对话直接执行（当前模型）；单本书永远串行，仅当多本书batch ingest同时产生多个pending prompt时才派生子代理并行回答 |
| API Key | 不需要（仅 caption 需 MiniMax key） |
| 执行方式 | `ingest.py` 交接（唯一路径，无需 flag） 或 对话中逐步完成 |
| 状态保存 | 对话上下文 / `.llm-wiki/conversation/` |
| 适用场景 | 人工单次消化、agent 自动化 |

---

## Conversation Mode Workflow

### Stage 1.2: Image Extraction

Handled automatically by `ingest.py` Stage 1.2. For standalone use:

```bash
python3 -c "
from _stage_1_extract import stage_1_2_extract_images
from _core import Config
config = Config.from_env()
stage_1_2_extract_images(Path('raw/Book/Book.pdf'), config)
"
```

**输出**: `wiki/media/<type>/<slug>/` + `_manifest.json`

---

### Stage 1.1: Text Extraction

```python
import fitz

doc = fitz.open("raw/Book/Book.pdf")
text = "\n\n".join(page.get_text() for page in doc)
doc.close()

# 保存备用
Path("/tmp/extract.txt").write_text(text, encoding="utf-8")
```

---

### Stage 2.1: Global Digest

**输入**：`ingest.py` 采样发送一小段文本（约 4-10K chars，从书中段提取），**不是**全文前 100K 字符。完整文本在 `.llm-wiki/extract-tmp/<book-stem>/p*.txt`（每页一个文件）。

> ⚠️ **2026-06-24 实测修正**：之前文档说"前 100K 字符"是错误的。实际 pipeline 只发送采样的几K字符。**当回答 Stage 2.1 prompt 时，应额外读取 `extract-tmp/` 下的页面样本**来补充对全书内容的理解，仅靠 prompt 中的采样文本不足以生成完整的 outline 和 chunk_plan。

**Prompt 模板**：
```
请分析以下硬件书籍的前 100K 字符，生成结构化摘要：

# 书籍信息（按此格式输出）
```yaml
book_meta:
  title: "..."
  authors: [...]
  year: N
  pages: N
  publisher: "..."
  language: "zh" | "en" | "mixed"

outline:
  - chapter: 1
    title: "..."
    key_topics: ["...", "..."]
    start_marker: "..."  # 章节 开头 30 字符

key_entities:
  - name: "..."
    role: "person" | "organization" | "system" | "model" | "standard"

key_concepts:
  - name: "..."
    importance: "core" | "supporting" | "mentioned"

key_claims:
  - claim: "..."
    chapter: N

chunk_plan:
  estimated_total_chunks: N
  - chunk: 1
    chapters: [1, 2]
    estimated_chars: N
```

<extracted_text>
{前100K 字符}
</extracted_text>
```

**输出**：保存为 `digest.yaml`

---

### Stage 2.2: Chunk Analysis

**输入**：完整文本 + Global Digest

**分块策略**：
- 目标：~60K 字符/块（与 ingest.py `target_chars` 一致）
- 重叠：3K 字符
- 短源（≤ 60K）仍然跑 1 块——Stage 2.2 永远不能跳过

**每个块的 Prompt 模板**：
```
分析以下文本块（块 {i+1}/{total}）：

<chunk_text>
{块内容}
</chunk_text>

上下文（已知的）：
- 书籍：《{title}》
- Global Digest 关键概念：{关键概念列表}

请生成：
```yaml
chunk_index: {i+1}
chunk_total: {total}

entities_found:
  - name: "..."
    type: "..."
    description: "..."
    first_appears: "..."

concepts_found:
  - name: "..."
    importance: "core" | "supporting" | "mentioned"
    definition: "..."
    related_entities: ["...", "..."]

claims:
  - claim: "..."
    evidence: "..."
    confidence: "high" | "medium" | "low"

formulas:
  - formula: "LaTeX"
    meaning: "..."
    variables: {"x": "..."}

connections_to_existing_wiki:
  - existing_page: "..."
    relationship: "extends" | "contrasts" | "applies" | "cites"

digest_updates:
  - type: "correction" | "extension" | "contradiction"
    detail: "..."
```

**输出**：保存为 chunk 分析（conversation mode 中记录在对话上下文）

---

### Stage 3.4: Review Suggestions

**输入**：所有 chunk analyses + Global Digest

**Prompt 模板**：
```
基于以下分析结果，找出需要审查的可疑内容：

<all_analyses>
{所有 chunk 分析的汇总}
</all_analyses>

生成 5 类审查项（YAML 格式，与 ingest.py stage_3_4_review_suggestions 一致）：
1. **confirm** — 需要确认的可疑内容
2. **suggestion** — 改进建议
3. **missing-page** — 缺少的重要页面
4. **contradiction** — 内容矛盾
5. **duplicate** — 重复内容

格式：
```yaml
- id: 1
  type: confirm
  title: "一句话标题"
  description: "详细描述（说明在哪个页面/章节、什么内容、为什么可疑）"
  affected_pages: ["sources/xxx.md", "concepts/yyy.md"]
  severity: high | medium | low
- id: 2
  type: suggestion
  title: "..."
  description: "..."
  affected_pages: [...]
  severity: medium
```

**输出**：保存为 `review-suggestions.json`（再通过 `run_review_suggestions.py` 或 ingest.py 写入 `wiki/REVIEW/`）

---

### Stage 2.3: Synthesis + Wiki Generation

**输入**：Global Digest + 所有 Chunk Analyses

**Prompt 模板**：
```
结合以下分析结果，生成 HardwareWiki 的 wiki 页面：

<global_digest>
{Global Digest YAML}
</global_digest>

<chunk_analyses_summary>
{Chunk Analyses 简要}
</chunk_analyses_summary>

现有 wiki 页面：{现有页面列表}

请生成以下页面（不要生成 index/log/overview——那由程序化的 Stage 3.5 处理）：

1. **Source 页面**：`wiki/sources/书名.md`
   - Frontmatter: type, title, created, updated, tags, related, sources
   - 内容：书籍信息、章节大纲、核心观点、参见图表链接

2. **Concept 页面**：为所有 `importance: "core"` 的概念
   - Frontmatter: type, title, created, updated, tags, related, sources
   - 内容：定义、关键细节、公式、案例、参见表格、参见图表

3. **Entity 页面**：为所有重要实体
   - Frontmatter: type, title, created, updated, tags, related, sources
   - 内容：是什么、为什么重要、相关概念、参见表格

输出格式（每页一个 FILE/END FILE 块，与 ingest.py parse_file_blocks() 一致）：
```
---FILE:wiki/sources/书名.md---
---
type: source
title: "..."
created: 2026-06-11
updated: 2026-06-11
tags: [...]
related: []
sources: ["raw/Book/书名.pdf"]
---

# 标题

...内容...
---END FILE---

---FILE:wiki/concepts/概念名.md---
---
type: concept
title: "..."
created: 2026-06-11
updated: 2026-06-11
tags: [...]
related: [wiki/sources/书名.md, wiki/concepts/相关概念.md]
sources: ["raw/Book/书名.pdf"]
---

# 概念名

...内容...
---END FILE---
```

约束：
- 所有 `[[wikilink]]` 使用**全文件名 stem**
- Frontmatter 包含 7 个必填字段
- 数学用 `$inline$` 和 `$$display$$`
- 每个 claim 标注章节来源
- Chinese 概念优先，英文括号补充
```

**处理结果**：解析 `---FILE:wiki/<path>---...---END FILE---` 块，写入 `wiki/` 目录

---

### Stage 3.2: Image Injection

**输入**：`wiki/media/<type>/<slug>/_manifest.json`

**Prompt 模板**：
```
给以下 source 页面添加 ## Embedded Images 段：

<source_page_content>
{现有 source 页面内容}
</source_page_content>

图片列表（来自 manifest.json）：
<images>
{图片列表}
</images>

在页面末尾添加：

```markdown
## Embedded Images

本书共抽出 X 张嵌入图：

| 页号 | 图片说明 | 文件 |
|------|----------|------|
| p0 | ... | wiki/media/... |
...
```

注意：caption 需要对每张图片进行简要说明（中文，20 字符+）。
```

**处理结果**：更新 source 页面

---

### 更新 Index/Log/Overview

- **Index.md**: 添加 source 页面链接
- **Log.md**: 添加 digest 记录
- **Overview.md**: 更新摘要

---

## 完整流程（单次对话）

> ⚠️ 以下为**概念性**流程示意。实际执行时由 `ingest.py` 驱动（conversation mode），
> 每个 LLM 步骤通过 prompt-file handoff 交接。Stage 编号和执行顺序以
> `references/ingest-stages-mandatory.md` 为准。

```
1. Stage 1.1: minerU OCR 提取文本 → p*.txt 每页一个文件
2. Stage 1.2/1.3: 图片提取 + caption（在 1.1 内部完成）
3. Stage 2.1: 采样文本 + prompt → global digest YAML
4. Stage 2.2: 分 N 次读取文本块 + prompt → N 个 chunk 分析 YAML
5. Stage 2.3: 增量关联检测（与已有 wiki 页面匹配）
6. Stage 2.4: digest + analyses + prompt → FILE 块（---FILE:wiki/<path>--- 格式）
7. Stage 2.5: 概念去重合并（多 chunk 书）
8. Stage 2.6: 源页面生成
9. Stage 2.7-2.9: Query 生成 → 跨源解析 → Comparison 生成
10. Stage 3.1: 写盘（所有 FILE 块）
11. Stage 3.2: 图片注入 source 页
12. Stage 3.3: 跨域 slug 碰撞审查
13. Stage 3.4: Review 审查建议
14. Stage 3.5: 程序化追加 index/log + LLM 重写 overview
15. Stage 3.6: 质量评分
16. Stage 3.7: Embeddings（LanceDB）
17. Stage 4.1: validate_ingest 自动验证
18. Wikilink enrichment: 多轮 LLM-task merge（可用 delegate_task 批量处理）
```

---

## 注意事项

1. **文本长度**：长文本需要分阶段处理，避免超上下文
2. **批次处理**：Stage 2.2 需要分批，每块 ~60K（ingest.py `target_chars`），短源仍然跑 1 块
3. **最终整合**：Stage 2.4 需要整合所有分析结果，合理归并概念/实体
4. **图片 caption**：Stage 3.2 需要注入 `## Embedded Images` 段到 source 页
5. **Frontmatter 完整性**：确保每个页面有 7 个必填字段
6. **不要生成 index/log/overview**：这三页由 Stage 3.5 程序化 append，LLM 不应输出它们（防止 ADL8113 事故——整文件重写导致静默丢失历史条目）
