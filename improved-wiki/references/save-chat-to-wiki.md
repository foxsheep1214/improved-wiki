# Save Chat to Wiki — Conversation → Knowledge

NashSU v0.4.25 parity for `chat-save-to-wiki.ts`: capture any LLM conversation as a structured wiki page and auto-ingest it into the knowledge base.

## Core Idea

You ask Claude questions all the time. Some answers are throwaway — but some contain real insight: a novel analysis, a comparison you hadn't considered, a connection between two concepts that clicked during the conversation. Without save-to-wiki, that insight disappears into chat history.

With save-to-wiki, any conversation turn (or series of turns) can be captured as a wiki page and auto-ingested — extracting entities, concepts, and cross-references just like a source document.

```
User asks a question → Claude answers → User: "save this to wiki"
  → Claude extracts the valuable content
  → Writes wiki/queries/<slug>.md
  → Auto-ingests → generates entity/concept pages
  → Knowledge permanently integrated
```

## NashSU Alignment

| NashSU | improved-wiki (Claude Code) |
|--------|---------------------------|
| UI button "Save to Wiki" | User says "保存到 wiki" / "save to wiki" / "记住这个" |
| `cleanAssistantContentForWikiSave()` — strips thinking blocks | Claude strips `<think>` blocks before saving |
| `titleFromCleanAssistantContent()` — extracts title from first heading | Claude generates a descriptive slug from the content |
| Saves to `wiki/queries/<slug>.md` with frontmatter | Same format: `type: query`, `origin: chat-save` |
| Auto-ingest triggers on the saved page | `ingest.py wiki/queries/<slug>.md` |

## Workflow

### Step 1: User Triggers Save

After any Claude response that contains knowledge worth keeping:

```
User: 保存到 HardwareWiki
User: save this to wiki
User: 记住这个
User: add this to the wiki
```

### Step 2: Claude Extracts the Valuable Content

Claude identifies the knowledge-bearing part of the conversation:
- The user's question (provides context for why this matters)
- Claude's substantive answer (the knowledge)
- Exclude: pleasantries, meta-discussion, tool call results

Claude cleans the content:
1. Remove `<think>` / `<thinking>` blocks
2. Remove tool use artifacts
3. Remove "How can I help you?" / "Let me know if..." boilerplate
4. Preserve: headings, lists, tables, code blocks, [[wikilinks]]

If the conversation is long, Claude can ask: "要保存整个讨论还是只保存最后的回答？"

### Step 3: Claude Generates the Wiki Page

```
---
type: query
title: "<descriptive title>"
created: <today>
origin: chat-save
tags: [<auto-detected tags>]
sources: []
---

# <Title>

## Context (用户提问)
<User's question that prompted this>

## Analysis
<Claude's substantive response, cleaned>

## Follow-up Questions
<Any open questions identified in the conversation>
```

Key formatting rules:
- Replace any existing `[[wikilinks]]` with correct wiki paths
- Add new `[[wikilinks]]` where the content mentions known wiki pages
- Use the wiki's output language
- Preserve code blocks with language markers

### Step 4: Write to Wiki

```bash
# Write the page
# Path: wiki/queries/<slug>.md
```

If a page with the same title exists, append as a new section or version the filename.

### Step 5: Auto-Ingest

```bash
python3 scripts/ingest.py wiki/queries/<slug>.md
```

This extracts entities/concepts from the saved conversation, creating new wiki pages and cross-references. Without this step, the saved page is just a static note — with it, the knowledge is decomposed and integrated.

### Step 6: Confirm

```
## ✅ 已保存到 HardwareWiki

**页面**: wiki/queries/gan-vs-sic-驱动电路对比.md
**消化产出**:
- wiki/entities/GaN-Systems.md (新增)
- wiki/concepts/enhancement-mode-gan.md (合并更新)
- wiki/comparisons/GaN-vs-SiC-驱动.md (新增)

**Review**: 1 个建议 — "补充 EPC 与 Navitas 的最新产品对比"
```

## Trigger Phrases

- `保存到 wiki` / `保存到 <项目名>`
- `save to wiki` / `save this to wiki`
- `记住这个` / `记录下来`
- `add this to the wiki` / `wiki this`
- `消化这段对话`

## When Claude Should Proactively Suggest Saving

Claude should suggest save-to-wiki when:

1. **The response is in-depth** (>500 words of substantive analysis)
2. **Novel connections are made** between concepts not previously linked
3. **Comparative analysis** that would be useful for future reference
4. **A question the wiki couldn't answer** was just answered from external knowledge
5. **User explicitly asked for research or analysis**

```
Claude: [详细回答后]
💡 这个分析涉及了几个 wiki 里没有的概念。要我保存到 HardwareWiki 并消化吗？
```

## Variants

### Variant A: Save Full Conversation

```
User: 把整个讨论保存到 wiki
Claude: [extracts all substantive exchanges, organizes into a single query page]
```

### Variant B: Save Only The Last Response

```
User: 只保存刚才那个对比分析
Claude: [extracts only the last substantive response]
```

### Variant C: Save with Custom Title

```
User: 保存到 RadarWiki，标题叫"雷达信号分选方法对比 2025"
Claude: [uses the specified title for the page slug and frontmatter]
```

### Variant D: Save as Source (not Query)

If the conversation is about analyzing a specific external article or paper that was discussed:

```
User: 把这段分析保存为 source 页面
Claude: [saves to wiki/sources/ instead of wiki/queries/]
```

## Edge Cases

### Duplicate Save
If the user accidentally triggers save twice, Claude should detect the existing page and either skip or append.

### Empty/Trivial Content
If the conversation is just chitchat, Claude should decline: "这段对话似乎没有需要保存到 wiki 的知识内容。"

### Very Long Conversation
If the conversation is >10K words, Claude should summarize rather than save verbatim. Ask the user what to focus on.

### Multi-Turn Discussion
If the insight developed over multiple turns, Claude should include the full context chain, not just the last response.

## Integration with Deep Research

Save-to-wiki and deep research form a natural pair:

```
User asks a question → Claude answers from knowledge → save to wiki
  → page has open questions → deep research those questions
  → new knowledge → save again → loop
```

This is the "conversation as knowledge driver" pattern — every good question expands the wiki, and the expanded wiki enables better questions.
