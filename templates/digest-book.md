# digest-book.md — Ingest template for full-length books

> **Use this template** when a file lives at `raw/Book/<Title> - <Year> - <Author>.pdf`.
> **The pipeline auto-selects** based on the first-level folder, but you can override with `--template=book`.

---

## What the LLM is asked to produce

For a book ingest, the LLM does two-step chain (per `references/ingest-stages-mandatory.md`, Stage 2.2 → 2.4):

### Step 1: Analysis

Read the book's extracted text and produce a **structured analysis** with these sections:

```yaml
# Actual ingest.py build_global_digest_prompt() schema (6 top-level keys):

book_meta:
  title: "<full title>"
  authors: ["<author 1>", "<author 2>"]
  year: <int>
  pages: <int>
  publisher: "<publisher>"
  language: "zh" | "en" | "mixed"

outline:
  # Complete chapter / section tree with key topics and page anchors
  - chapter: 1
    title: "<chapter title>"
    key_topics: ["topic 1", "topic 2"]        # ingest.py uses key_topics, not sections
    start_marker: "<distinctive opening text>"  # for chunk boundary detection

key_entities:   # named things: people, organizations, systems, models, …
  - name: "<entity>"
    description: "<1-2 sentence definition>"
    first_appears: "<chapter/section reference>"

key_concepts:   # math frameworks, methodologies, taxonomies
  - name: "<concept>"
    definition: "<1-2 sentence definition>"
    importance: "core" | "supporting" | "mentioned"
    related_entities: ["<entity name>", ...]

key_claims:
  - claim: "<the assertion>"
    chapter: <int>
    confidence: "high" | "medium" | "low"

chunk_plan:
  estimated_total_chunks: <int>
  chunk_boundaries:
    - chunk: 1
      chapters: "<chapter range>"
      estimated_chars: <int>
      overlap_with_next: "<strategy description>"
```

### Step 2: Generation

The LLM takes the analysis and writes these files:

1. **`wiki/sources/<Title> - <Year> - <Author>.md`** — the source page
   - Frontmatter: `type: source`, `title`, `created`, `updated`, `tags`, `related: []`, `sources: ["raw/Book/<file>.pdf"]`
   - Body: book metadata table, chapter outline, summary, key claims, reading notes, "参见" with all new concept/entity pages

2. **`wiki/concepts/<slug>.md`** — one page per key concept (10-50 expected for a book)
   - Frontmatter: `type: concept`, `title`, `created`, `updated`, `tags`, `related: [...]`, `sources: ["raw/Book/<file>.pdf"]`
   - Body: definition, derivation/algorithm, examples, "参见" with 3+ related pages

3. **`wiki/entities/<slug>.md`** — one page per key entity
   - Same frontmatter as concept but `type: entity`
   - Body: who/what/when/where, significance, "参见"

4. **Update `wiki/index.md`** — add the new source under `## Sources`, new concepts under `## Concepts`, etc.

5. **Append to `wiki/log.md`** — operation record (see `templates/log.md`; appended by Stage 3.5)

6. **Update `wiki/overview.md`** — if the book adds a major claim, update the relevant section

---

## Field-level guidance

- **`key_concepts[].importance: "core"`** → MUST have a wiki page. Default for any concept mentioned >5 times in the book.
- **`key_concepts[].importance: "supporting"`** → SHOULD have a wiki page, or merge into a related concept page.
- **`key_concepts[].importance: "mentioned"`** → OK to skip, just note in the source page.
- **`key_entities`** — every person / org / system / model / standard / device that's central to the book's argument gets a page. Briefly mentioned people (one sentence) do not.
- **`chunk_plan.estimated_total_chunks`** — chunk 大小由上下文窗口动态计算（`_core.py _compute_chunk_targets`：默认 token ceiling 64K，可用 `IMPROVED_WIKI_TARGET_TOKENS_CEIL` 覆盖），不是固定 60K。文本量不足一个 chunk 预算的书（如短 datasheet、应用笔记）仍得 1 chunk — Stage 2.2 is never skipped.
- **`connections_to_existing_wiki`** (in Stage 2.2 per-chunk analysis, not Stage 2.1) — be conservative. Only flag clear conflicts, not subtle differences in framing. False positives pollute the LLM-curated review queue.

---

## Common pitfalls when ingesting books

| Symptom | Fix |
|---|---|
| LLM produces 100+ concept pages, mostly trivial | The `importance` field is too permissive. Re-prompt with "only `core` concepts get their own page; merge `supporting` into related pages" |
| LLM uses `[[雷达原理]]` instead of `[[雷达原理 - 2009 - 张光义]]` | Frontmatter's `related: []` array is auto-included in the prompt. Make sure it has the full stems |
| LLM invents chapter structure not in the book | The extracted text may have failed OCR for chapter pages. Re-run minerU on those pages and re-Ingest |
| `wiki/overview.md` is rewritten too aggressively | The LLM was told to "update if major claim added". Tighten the prompt: "only add a 1-sentence summary to overview.md under an existing section" |

---

## See also

- `references/naming-conventions.md` — frontmatter schema + wikilink naming convention
- `templates/digest-paper.md` — for shorter / paper-form sources
- `templates/digest-datasheet.md` — for components
