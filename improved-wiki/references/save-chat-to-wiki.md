# Save Chat to Wiki — Conversation → Knowledge

参考 NashSU `chat-save-to-wiki.ts` + the `SaveToWikiButton` flow
(`chat-message.tsx`): capture an assistant answer as a wiki query page and
auto-ingest it into the knowledge base. **Strict NashSU minimal implementation**
— no invented frontmatter fields, no body restructuring; the saved page is the
cleaned assistant content written verbatim, exactly like NashSU.

## Core Idea

You ask the calling agent questions all the time. Some answers are throwaway — but some
contain real insight. Without save-to-wiki, that insight disappears into chat
history. With save-to-wiki, an assistant answer is captured as a `wiki/queries/`
page and auto-ingested — extracting entities, concepts, and cross-references just
like a source document.

```
Agent answers → User: "save this to wiki"
  → clean the assistant content (NashSU cleanAssistantContentForWikiSave)
  → write wiki/queries/<slug>-<YYYY-MM-DD>-<HHMMSS>.md (frontmatter + verbatim body)
  → update index.md (## Queries) + log.md
  → auto-ingest → entity/concept pages
  → knowledge permanently integrated
```

## NashSU Alignment

| NashSU (`chat-save-to-wiki.ts` / `chat-message.tsx`) | improved-wiki (calling agent) |
|--------|---------------------------|
| UI button "Save to Wiki" on an assistant message | User says "保存到 wiki" / "save to wiki" / "记住这个" |
| `cleanAssistantContentForWikiSave()` — strips `<!-- save-worthy: -->` / `<!-- sources: -->` comments + `<think>` blocks, trims | Same cleaning rules (see Step 1), nothing more |
| `titleFromCleanAssistantContent()` — first non-empty line, strip leading `#`, cap 60 chars, fallback `"Saved Query"` | Same title rule (Step 2) |
| `makeQueryFileName()` — NFKC slug, 50-char cap, `+ -YYYY-MM-DD-HHMMSS` (UTC) | Same filename rule (Step 3) |
| Frontmatter: `type / title / created / tags` only | Same 4 fields — **no** `origin`, **no** `sources` |
| Body = cleaned assistant content appended verbatim | Same — no `# Title`, no Context/Analysis sections |
| Update `index.md` `## Queries` + append `log.md` | Same (Step 4) |
| `autoIngest()` on the saved page | `ingest.py wiki/queries/<file>.md` (Step 5) |

## Workflow

### Step 1: Clean the Assistant Content

The unit of saving is **one assistant answer** (the message the button is attached
to in NashSU). Apply exactly NashSU's `cleanAssistantContentForWikiSave`:

1. Remove `<!-- save-worthy: ... -->` and `<!-- sources: ... -->` HTML comments.
2. Remove `<think>` / `<thinking>` blocks (closed, and an unclosed `<think>` that
   runs to the end).
3. Strip leading whitespace; trim trailing whitespace.

Do **NOT** strip tool-use artifacts, do **NOT** remove "How can I help" / "Let me
know" boilerplate, do **NOT** rewrite or inject `[[wikilinks]]`, do **NOT** pull in
the user's question. NashSU keeps the answer verbatim apart from the three rules
above — the downstream auto-ingest is what extracts structure.

### Step 2: Derive the Title

Port of `titleFromCleanAssistantContent`:

- Take the **first non-empty line** of the cleaned content.
- Strip a leading `#`/`##`/… and the following space.
- Truncate to **60 characters**.
- If the content is empty, the title is the literal string `Saved Query`.

The title is the human-readable string only. The filename slug is derived
separately in Step 3 — do not conflate them.

### Step 3: Write the Page

Filename (port of `makeQueryFileName`):

```bash
# slug: NFKC-normalize the title, lowercase, spaces → hyphens, keep only
# letters/digits + hyphen, collapse repeats, cap 50 chars (fallback "query").
# Then append a UTC timestamp so repeated saves never collide:
TS=$(date -u +%Y-%m-%d-%H%M%S)
# path: wiki/queries/<slug>-<TS>.md
```

Page content (frontmatter + blank line + verbatim cleaned body):

```
---
type: query
title: "<title, with any \" escaped as \\\">"
created: <today, UTC>
tags: []
---

<cleaned assistant content, verbatim from Step 1>
```

Rules:
- Frontmatter is exactly these four keys. **No** `origin`, **no** `sources`,
  **no** injected `# Title` heading, **no** Context/Analysis/Follow-up sections.
- Escape any `"` in the title as `\"` (otherwise the YAML is invalid).
- The body is the cleaned content from Step 1, written as-is.

### Step 4: Update Index + Log

NashSU updates two aggregate files on every save — do the same:

- `wiki/index.md`: under a `## Queries` heading (create it if missing), insert
  `- [[queries/<filename-without-.md>|<title>]]`.
- `wiki/log.md`: append `- <today>: Saved query page \`<filename>\``.

### Step 5: Auto-Ingest

```bash
python3 "$SKILL_DIR/scripts/ingest.py" wiki/queries/<filename>.md
```

This bridges the query page into `raw/queries/` and ingests it (NashSU `autoIngest`
parity), extracting entities/concepts and creating cross-references. Without this,
the saved page is a static note; with it, the knowledge is decomposed and
integrated. (NashSU gates auto-ingest on having a usable LLM; here it runs only when
the calling agent can complete the conversation-mode handoffs.)

### Step 6: Confirm

```
## ✅ 已保存到 HardwareWiki

**页面**: wiki/queries/gan-vs-sic-2026-06-30-142210.md
**消化产出**:
- wiki/entities/GaN-Systems.md (新增)
- wiki/concepts/enhancement-mode-gan.md (合并更新)

**Review**: 1 个建议 — "补充 EPC 与 Navitas 的最新产品对比"
```

## Trigger Phrases

- `保存到 wiki` / `保存到 <项目名>`
- `save to wiki` / `save this to wiki`
- `记住这个` / `记录下来`
- `add this to the wiki` / `wiki this`
- `消化这段对话`

## When the calling agent should proactively suggest saving

NashSU's trigger is a manual button — there is no proactive prompting upstream.
The trigger phrases above are the CLI equivalent of the button. As a light CLI
convenience (not NashSU behavior), the calling agent *may* offer to save after an in-depth,
novel answer, but the default is to wait for an explicit trigger.

```
Agent: [详细回答后]
💡 要把这个回答保存到 HardwareWiki 并消化吗？
```

## Notes on Fidelity

- Each save always produces a **new** file (the `-HHMMSS` suffix guarantees no
  collision) — NashSU does not deduplicate, decline trivial content, or summarize
  long answers, and neither do we.
- There is **no** "save as source" variant — NashSU always writes `type: query`
  to `wiki/queries/`.
- Saving captures the assistant answer only; it does not embed the user's question
  or restructure the text.

## Integration with Deep Research

Save-to-wiki and deep research form a natural pair: a saved answer with open
questions can seed a deep-research round, whose result is saved again — every good
question expands the wiki, and the expanded wiki enables better questions.
