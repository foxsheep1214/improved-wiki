# digest-presentation.md — Ingest template for slide decks

> **Use this template** when a file lives at `raw/Presentation/<...>/*.pdf`.
> Presentations (conference talks, internal reviews) are slide-by-slide. Output a per-slide summary plus a single key-claims / concepts extraction. Lighter than a book or paper.

---

## What the LLM is asked to produce

### Step 1: Analysis

```yaml
deck_meta:
  title: "<full title of the talk>"
  speaker: "<speaker name, may be multiple>"
  affiliation: "<speaker's organization>"
  venue: "<conference name, e.g. APEC 2024>"
  date: "<YYYY-MM-DD or just YYYY>"
  pages: <int>  # number of slides
  deck_type: "conference_talk" | "internal_review" | "tutorial" | "sales" | "training"

# Per-slide summary (just 1 line per slide)
slide_summary:
  - slide: 1
    type: "title"
    content: "<title slide>"
  - slide: 2
    type: "outline"
    content: "<outline>"
  - slide: 3
    type: "motivation"
    content: "<what problem motivates this work>"
  # ... one entry per slide
  - slide: N
    type: "conclusion" | "thank_you" | "backup"
    content: "<...>"

# Extract the meat: claims, concepts, entities
key_entities:
  - name: "<speaker>"
    wikilink_target: "<existing-slug>"
  - name: "<affiliation>"
    wikilink_target: "<existing-slug>"

key_concepts:
  # Presentations usually introduce 1-3 concepts, more if it's a tutorial
  - name: "<concept>"
    importance: "core"
    wikilink_target: "<concept-slug>"
  - name: "<...>"

key_claims:
  # The "take-aways" — usually on the conclusion slide
  - claim: "<take-away statement>"
    evidence: "<slide N>"
    section: "<slide title>"

# What kind of talk is this
key_questions_answered:
  - "<the question this presentation is structured to answer>"

recommendations_from_speaker:
  - "<e.g. 'Use GaN for 1-2kW totem-pole PFC for best efficiency'>"
```

### Step 2: Generation

Files to write:

1. **`wiki/sources/<Speaker> - <Venue> - <Year> - <Title>.md`** — source page
   - Body: deck metadata, talk type, per-slide summary (collapsed or as a table), key claims, recommendations, "参见"

2. **`wiki/concepts/<slug>.md`** — 1-3 concept pages (only the genuinely new concepts)

3. **Update `wiki/index.md`**, **`wiki/log.md`**, **`wiki/overview.md`**

---

## Prompt template (the actual prompt sent to the LLM)

```
# Role
You are the LLM maintainer of a Karpathy-pattern personal knowledge base.
You ingest slide decks (conference talks, internal reviews) into a structured wiki.

# Input
- Title: {title}
- Speaker: {speaker}
- Venue: {venue}
- Date: {date}
- File path: {raw_path}
- Extracted text: <per-slide text in <extracted_text>...</extracted_text>>
- Existing wiki context: <slugs in <existing_wiki>...</existing_wiki>>

# Task
Two-step chain.

## Step 1: Analysis
YAML block with the full analysis. Use the schema in §Analysis above.
A presentation is expected to produce 1 source page + 1-3 concept pages (only the new concepts).

## Step 2: Generation
File contents in order:
### File 1: wiki/sources/<Speaker> - <Venue> - <Year> - <Title>.md
### File 2..N: wiki/concepts/<slug>.md (1-3 files)
### Update: wiki/index.md
### Append: wiki/log.md

# Constraints
- Every `[[wikilink]]` MUST use the FULL filename stem (per improved-wiki §6.2)
- Frontmatter must follow improved-wiki §5
- The per-slide summary can be a markdown table (slide | type | content)
- The key_claims are usually on the "conclusion" or "summary" slide
- Skip "thank you" / "Q&A" / pure title slides in the per-slide summary
```

---

## Type-specific guidance

- **Presentations are condensed**: don't try to extract every detail. Focus on the **take-aways** (usually the conclusion slide) and the **novel concepts** introduced.
- **Per-slide summary should be a table**: it's the most scannable form. The user will come back to the deck to look at specific slides.
- **Speaker's recommendations are valuable**: if the speaker says "use X for Y", that's an opinion worth recording.
- **No worked examples** (usually): presentations don't have the depth of papers. Don't try to extract them.

---

## Common pitfalls when ingesting presentations

| Symptom | Fix |
|---|---|
| LLM writes a wall of text per slide | Force: "Per-slide content is 1-2 lines max. The user has the original deck" |
| LLM misses the speaker's actual claims | The "conclusion" or "summary" slide is where the take-aways are. Make sure the analysis captures them |
| LLM extracts every bullet from the deck | Only extract what's relevant to engineering knowledge. Skip "thank you to sponsors" etc. |

---

## See also

- `SKILL.md` §5, §6
- `templates/digest-paper.md` — for the published version of the same talk (usually exists alongside)
