# digest-paper.md — Ingest template for academic / industry papers

> **Use this template** when a file lives at `raw/Paper/<...>/*.pdf`.
> Differs from `digest-book.md` only in: (1) lighter output (2-5 concepts vs 10-50), (2) no chapter structure, (3) focuses on methodology + results + comparison to related work.

---

## What the LLM is asked to produce

### Step 1: Analysis

```yaml
paper_meta:
  title: "<full title>"
  authors: ["<author 1>", "<author 2>"]
  year: <int>
  venue: "<conference or journal, e.g. IEEE TPEL 2024 or APEC 2024>"
  pages: <int>
  paper_type: "journal" | "conference" | "whitepaper" | "preprint" | "industry"
  doi: "<doi or null>"
  topic_subfolder: "<which paper/<NN_xxx>/ subfolder the file is in — for wiki destination>"

problem:
  # The problem the paper solves, in 1-2 sentences
  statement: "..."
  why_it_matters: "..."

methodology:
  # The technical approach — what did they actually do?
  core_idea: "..."
  key_equations:
    - equation: "<LaTeX>"
      role: "..."
  algorithms:
    - name: "..."
      steps: ["..."]
  assumptions: ["..."]

results:
  # What did they achieve? Numbers if applicable.
  main_findings:
    - finding: "..."
      quantitative: "<numbers if any>"
  comparison_to_prior_work:
    - prior: "<reference or method>"
      theirs_vs_prior: "..."

key_entities:   # same schema as digest-book.md
  - name: "<entity>"
    wikilink_target: "<entity-slug>"

key_concepts:
  - name: "<concept>"
    importance: "core" | "supporting" | "mentioned"
    wikilink_target: "<concept-slug>"

key_claims:
  - claim: "<the assertion>"
    evidence: "<brief description of supporting argument or data>"
    section: "<paper section / page>"

connections_to_existing_wiki:
  - existing_page: "<wikilink>"
    relationship: "extends" | "applies" | "cites" | "contrasts"

recommended_wiki_structure:
  new_concept_pages:  # 2-5 expected
    - slug: "<concept-slug>"
      rationale: "<why this needs its own page>"
  new_entity_pages:
    - slug: "<entity-slug>"
      rationale: "<why this needs its own page>"

open_questions_in_paper:    # what the paper admits it didn't solve
  - "..."

reproducibility:
  code_available: "yes" | "no" | "not-mentioned"
  url: "<github or project url or null>"
  data_available: "..."
```

### Step 2: Generation

Files to write (lighter than book):

1. **`wiki/sources/<Authors> - <Year> - <Short-Title>.md`** — source page
   - Body: paper metadata, problem statement, methodology, key results, comparison table, "参见"

2. **`wiki/concepts/<slug>.md`** — 2-5 concept pages
   - Smaller than book concepts: focus on the paper's novel contribution, not background

3. **`wiki/entities/<slug>.md`** — 1-3 entity pages (the paper's authors, if notable)

4. **Update `wiki/index.md`**, **`wiki/log.md`**, **`wiki/overview.md`**

---

## Type-specific guidance

- **`paper_type: "whitepaper"`** — vendor whitepapers (TI, ADI, Maxim) often mix marketing with technical content. Treat the technical section as the "methodology", treat the marketing claims with appropriate skepticism in `key_claims`.
- **`paper_type: "preprint"`** — flag the venue as unverified. Note in the source page that this is a preprint, not peer-reviewed.
- **`topic_subfolder`** — this is the *destination* in the wiki, NOT the source folder. The pipeline reads it from the second-level folder under `raw/Paper/`. Use it to assign the source page to the right wiki section (e.g. `wiki/sources/Paper/02_硬件电路设计/<file>.md` if the paper is about hardware circuit design).

---

## Common pitfalls when ingesting papers

| Symptom | Fix |
|---|---|
| LLM produces textbook background as concept pages | Prompt: "ONLY extract concepts that the paper introduces or applies non-trivially. Standard textbook concepts that the paper merely cites should NOT get new pages" |
| LLM conflates "their contribution" with "background they reviewed" | The `methodology.core_idea` field must be one paragraph max. Background goes in `assumptions` |
| `comparison_to_prior_work` is empty | Force a re-prompt: "Every paper positions itself against prior work. Find that section and extract 1-3 specific comparisons with numbers" |
| Concept page duplicates an existing page | The `connections_to_existing_wiki` step should detect this. If not, prompt: "Check the existing wiki for similar concept slugs before recommending a new page" |

---

## See also

- `references/naming-conventions.md` — frontmatter schema + wikilink naming
- `templates/digest-book.md` — for full-length books
- `templates/digest-datasheet.md` — for component datasheets (different focus)
