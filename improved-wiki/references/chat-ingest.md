# Chat Ingest — Interactive Human-Guided Ingestion

NashSU v0.4.25 parity for `startIngest()` + `executeIngestWrites()` two-step interactive ingest.

## Why Chat Ingest

Auto-ingest treats every source equally — every concept gets a page, every claim gets equal weight. But you know what matters to you. Chat ingest lets you guide the LLM: emphasize this concept, skip that section, connect to existing pages, flag contradictions you care about.

The key insight: **in Claude Code, the conversation IS the chat.** No separate UI needed. The same Claude that's talking to you is the one that will generate wiki pages. Your guidance is part of the context.

## NashSU Alignment

| NashSU | improved-wiki (Claude Code) |
|--------|---------------------------|
| `startIngest()` — reads source, starts chat | Claude reads source + presents analysis |
| User chats with LLM about the source | You discuss with Claude naturally |
| `executeIngestWrites()` — conversation history → FILE blocks | Claude uses full conversation context → FILE blocks |
| "Save to Wiki" button | You say "生成" / "写入 wiki" / "generate" |

## Workflow

### Step 0: Invocation

```
User: /improved-wiki chat-ingest raw/book/Book.pdf
```

Or with a wiki project context:

```
User: 在 HardwareWiki 中用 chat 模式消化 raw/papers/新论文.pdf
```

### Step 1: Mechanical Pre-processing (Stage 0)

Claude runs the same Stage 0 as auto-ingest:

```bash
# Text extraction + PDF type detection
python3 scripts/ingest.py --stage 0.5 --source raw/book/Book.pdf

# Image extraction (if applicable)
python3 scripts/ingest.py --stage 0.7 --source raw/book/Book.pdf

# Image captioning (if images found)
python3 scripts/ingest.py --stage 0.9 --source raw/book/Book.pdf
```

These are I/O operations — no LLM reasoning needed, no reason to involve the user.

### Step 2: Present Analysis (Stage 1 equivalent)

Claude reads the extracted text and presents a structured **digest** to the user:

```
## 📖 《Book Title》— 消化预览

**基本信息**: 作者 XXX, 2024, 350页
**类型**: 教材 / 论文 / 数据手册 / ...

### 章节结构
1. 第一章：XXX — 涵盖 A, B, C
2. 第二章：YYY — 涵盖 D, E, F
...

### 核心概念（将生成 concept 页面）
- **概念A** — 定义简述 (core)
- **概念B** — 定义简述 (core)
- **概念C** — 定义简述 (supporting)
...

### 关键实体（将生成 entity 页面）
- **实体X** — 型号/人名/机构
...

### 与已有 wiki 的关系
- [[已有页面1]] — extends (新书提供了更详细的推导)
- [[已有页面2]] — contrasts (新书的结论与现有结论不同)
...

### 建议
- 重点消化: 第3章 (新方法)、第5章 (实验数据)
- 可跳过: 第1章 (与已有 wiki 重复)
- 建议对比: 概念A vs [[已有概念Z]]

---

请回复你的指导：
- 哪些内容要重点消化？
- 哪些可以跳过或简化？
- 有没有想特别关注的公式/图表/案例？
- 需要和哪些已有页面做对比？
```

This is NOT a script output — Claude generates this digest directly in the conversation, using the source text it has read. The key is that Claude presents **actionable choices**, not just a summary.

### Step 3: User Guidance

The user responds naturally — no structured format required:

```
User: 重点消化第3章的新方法，第5章的实验数据要完整保留。
      第1-2章可以跳过，wiki里已经有了。
      概念A和已有页面[[existing-concept]]做一个对比页。
      另外注意公式(5.3)，这个很重要。
```

Or:

```
User: 全部消化，但特别关注作者关于 XX 的论断，和 wiki 里 [[YY]] 页面的结论有冲突。
```

Claude should **confirm understanding** before proceeding:

```
Claude: 明白了。消化策略：
  ✅ 完整消化：第3章（新方法）、第5章（实验数据）
  ⏭️ 跳过：第1-2章（wiki 已有）
  🔗 生成对比页：概念A vs [[existing-concept]]
  ⭐ 重点标注：公式(5.3)
  ⚠️ 关注矛盾：XX 论断 vs [[YY]]
  
  确认后我就开始生成 wiki 页面。输入"生成"继续。
```

### Step 4: Generate Wiki Pages (Stage 2 with user guidance)

When the user confirms ("生成" / "generate" / "写入" / "proceed"), Claude generates wiki pages. The generation prompt **must include** the user's guidance:

```
System: You are generating wiki pages from a source document.
The user has provided specific guidance on what to emphasize, skip, and connect.

## User Guidance (FOLLOW THIS — it overrides default behavior)

<user_guidance_from_conversation>

## Source Context

<source_text_or_summary>

## Existing Wiki Index

<wiki_index>

## Generation Rules

1. Follow the user's guidance above — it takes priority over general rules
2. Use ---FILE: wiki/path.md--- ... ---END FILE--- format
3. Every page must have complete YAML frontmatter
4. Use [[wikilinks]] for cross-references
5. Source file: <source_identity>

Generate FILE blocks now. Start with ---FILE: as the first characters.
```

**Critical difference from auto-ingest**: The user guidance section is placed FIRST and labeled as authoritative. This ensures the LLM prioritizes user intent over default behavior.

### Step 5: Write & Validate (Stage 3-4)

After generation, Claude:
1. Parses FILE blocks
2. Writes pages to `wiki/` (using page merge if page exists)
3. Injects image references into source page (Stage 3.8)
4. Appends to index.md / log.md (Stage 4.7, programmatic)
5. Runs `validate_ingest.py`

```bash
python3 scripts/validate_ingest.py
```

### Step 6: Present Results

```
## ✅ 消化完成：《Book Title》

**生成页面** (8 个):
- wiki/sources/book-title.md — 源页面
- wiki/concepts/概念A.md — core concept
- wiki/concepts/概念B.md — core concept
- wiki/concepts/概念C.md — supporting concept
- wiki/entities/实体X.md — entity
- wiki/comparisons/概念A-vs-已有概念Z.md — 对比页
- wiki/queries/概念A的开放问题.md — query

**Review items** (2 个):
- ⚠️ 概念A 的公式推导需要人工验证
- 🔗 实体X 与已有页面 [[old-entity]] 疑似重复

**跳过**: 第1-2章（按你的指导）
**图片**: 12 张已注入 source 页面
**验证**: validate_ingest.py 15/15 通过
```

## Trigger Phrases

The user signals readiness to generate with any of:
- `生成` / `写入` / `写入 wiki` / `开始生成`
- `generate` / `write` / `proceed` / `go ahead`
- `确认` / `没问题` / `开始`

Claude must NOT auto-generate without explicit user confirmation. The whole point of chat ingest is the human-in-the-loop guidance.

## When Claude Should Ask for Guidance

After presenting the digest, Claude should proactively ask:

1. **If the book is long (>200 pages)**: "这本书较长，有哪几章是你最关心的？其他部分我可以简化处理。"
2. **If there's overlap with existing wiki**: "第X章的内容和 wiki 里 [[已有页面]] 高度重叠，要跳过还是做对比？"
3. **If the book has many formulas/figures**: "这本书有 50+ 个公式和 30+ 张图，要全部保留还是只保留核心的？"
4. **If contradictions found**: "发现新书和 wiki 现有内容有 3 处矛盾，要我生成对比页吗？"

## Comparison: Chat Ingest vs Auto Ingest

| 维度 | Auto Ingest | Chat Ingest |
|------|------------|-------------|
| 人工参与 | 零（全自动） | 中等（1-2轮交互） |
| 相关性 | LLM 自行判断重要性 | 用户指定重点 |
| 遗漏风险 | 可能漏掉用户关心的细节 | 用户主动标记重点 |
| 噪音控制 | 所有概念等权处理 | 用户可跳过不感兴趣的部分 |
| 连接质量 | 仅基于 wikilink 文本匹配 | 用户可指定跨域连接 |
| 速度 | 快（无人等待） | 中等（需等待用户回复） |
| 适用场景 | 批量消化、定时导入 | 重要单书、高价值论文、有明确关注点的内容 |

## Edge Cases

### Very Long Sources (>200K chars)
Chat ingest should still use chunked analysis (Stage 1.5), but the user guidance is merged into each chunk's analysis prompt. The user doesn't need to review each chunk — only the global digest is presented.

### Re-ingest with Chat
If a source was previously auto-ingested, chat re-ingest can be used to add missing pages or correct emphasis. Claude should:
1. Show which pages already exist from the previous ingest
2. Ask the user what to add/change
3. Use page merge for existing pages, create new pages for additions

### Multiple Sources in One Chat Session
User can discuss multiple sources sequentially:
```
User: 先消化 A.pdf，重点关注第3章
[discuss A, generate A]
User: 好，现在消化 B.pdf，和刚才的 A 做对比
```

Claude should track which sources have been discussed and generated in the current session.

## Integration with Existing Pipeline

Chat ingest is NOT a replacement for auto-ingest. They coexist:

```
ingest.py --mode auto    → 全自动流水线（批量、定时）
/improved-wiki chat-ingest → 交互式人工引导（单本、重要源）
```

Both modes write through the same `writeFileBlocks` → `validate_ingest.py` path. Both update the same `ingest-cache.json`. Both trigger the same aggregate repair. The only difference is where the generation prompt's "user guidance" section comes from: empty in auto mode, filled from conversation in chat mode.
