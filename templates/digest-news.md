# digest-news.md — Ingest template for web clips and news articles

> **Use this template** when a file lives at `raw/News/<...>/*.md` (or `.pdf` for archived articles).
> News and web clips are short-lived, low-effort. Output should be 1 source page + 0-2 concept pages. (Auto-marking `status: outdated` after 6 months is a **planned** Lint pass — **not yet implemented**; see below.)

---

## What the LLM is asked to produce

### Step 1: Analysis

```yaml
clip_meta:
  title: "<headline>"
  outlet: "<e.g. EE Times>"
  author: "<byline or null>"
  publication_date: "<YYYY-MM-DD>"
  url: "<original URL or null>"
  content_type: "news" | "blog_post" | "press_release" | "interview" | "tweet_thread"

  # News has a SHORT half-life. Mark this.
  evergreen_value: "low" | "medium" | "high"
  # low: a product announcement, a person changing jobs
  # medium: a market trend, a technology shift
  # high: a foundational technical explanation, a historical retrospective

summary:
  # The 5W1H
  who: "..."
  what: "..."
  when: "..."
  where: "..."
  why: "..."
  how: "..."

key_facts:
  - fact: "<e.g. Company X acquired Y for $Z billion>"
    source_quote: "<verbatim if short>"
  - fact: "..."

key_entities:
  - name: "<company or person>"
    wikilink_target: "<existing-slug>"

key_concepts:
  # 0-2 concepts. News rarely introduces new concepts; it reports on existing ones.
  - name: "<e.g. GaN power semiconductors>"
    importance: "mentioned"
    wikilink_target: "GaN-semiconductor"

context:
  # Why does this matter in the broader landscape?
  bigger_picture: "..."

related_news_in_wiki:
  - existing_page: "<wikilink to a prior news page about the same topic>"
    relationship: "followup" | "background"
```

### Step 2: Generation

Files to write:

1. **`wiki/sources/<Outlet> - <Date> - <Headline-Slug>.md`** — source page
   - Body: metadata, summary, key facts, bigger picture, "参见"

2. **`wiki/concepts/<slug>.md`** — 0-2 concept pages (only if evergreen_value is "high" AND the concept is new to the wiki)

3. **Update `wiki/index.md`**, **`wiki/log.md`**

---

## Type-specific guidance

- **Most news is short-lived**: A product announcement from 6 months ago is history. A market trend from 2 years ago may still be relevant. The `evergreen_value` field is intended to drive a future `status: outdated` decision (**not yet consumed by any code**).
- **0-2 concept pages is the norm**: don't try to extract 5 concepts from a 500-word article. The article probably only mentions 1-2 concepts, and they're usually already in the wiki.
- **The "bigger_picture" field is the value**: this is what makes a news article worth ingesting. "Company X acquired Y" without context is just trivia. With context ("this consolidates the GaN supply chain"), it's knowledge.

---

## Lint: marking outdated news (planned — not yet implemented)

> ⚠️ This describes a **planned** capability. No current code marks news `status: outdated`
> by date or moves index entries — treat the steps below as a design sketch, not existing
> behavior. (`wiki-lint-semantic.py` only surfaces a generic "appears outdated" hint via the
> LLM; it does not apply this date-based rule.)

The intended Lint pass (cron monthly) would:
- For every `wiki/sources/<Outlet> - <Date> - ...` page where `Date` is > 6 months old
- Add `status: outdated` to its frontmatter
- Keep the page in the wiki (don't delete — historical record)
- In `wiki/index.md`, move it to a `## Sources (outdated)` subsection

That would keep the wiki's "current knowledge" view clean while preserving the historical record.

---

## Common pitfalls when ingesting news

| Symptom | Fix |
|---|---|
| LLM produces 3+ concept pages from a short article | Force: "News articles rarely introduce new concepts. Only create a concept page if the article defines or formalizes something that wasn't already in the wiki" |
| LLM paraphrases quotes | News is full of quotes. Keep them verbatim (with quotation marks) |
| LLM adds marketing claims as `key_facts` | Distinguish: "Company X claims Y" (vendor's claim, not a fact) vs "Y happened" (verifiable event). The first goes in `key_claims` with a flag; the second goes in `key_facts` |

---

## See also

- `references/naming-conventions.md` — frontmatter schema + wikilink naming
- `templates/digest-paper.md` — for the underlying technical paper a news article reports on
