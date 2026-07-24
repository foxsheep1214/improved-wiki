# Wiki Schema — Research Deep-Dive

## Page Types

| Type | Directory | Purpose |
|------|-----------|---------|
| entity | wiki/entities/ | Named things such as people, organizations, products, systems, and standards |
| concept | wiki/concepts/ | Reusable ideas, techniques, phenomena, models, and frameworks |
| source | wiki/sources/ | One grounded summary page per immutable raw source |
| query | wiki/queries/ | User-initiated open questions, saved answers, and deep research |
| comparison | wiki/comparisons/ | Source-grounded side-by-side analysis of related alternatives |
| synthesis | wiki/synthesis/ | Cross-source summaries and conclusions |
| overview | wiki/ | Application-maintained project overview |
| thesis | wiki/thesis/ | Falsifiable working hypotheses that evolve across sources |
| methodology | wiki/methodology/ | Reusable research, design, test, or verification methods |
| finding | wiki/findings/ | Source-backed empirical or quantitative observations |

## Naming Conventions

- Page slugs follow the source language: English uses `kebab-case`; CJK keeps
  readable CJK characters. Do not mix transliterated English and CJK in one slug.
- Source pages mirror the raw relative path and filename without the extension.
- Comparison pages use `<A>-vs-<B>.md`.
- Finding names describe the result, thesis names state the hypothesis, and
  methodology names identify the reusable method.
- Filenames must not contain commas, `<>:"/\|?*`, trailing spaces, or trailing dots.

## Frontmatter

All generated pages include:

```yaml
---
type: entity | concept | source | query | comparison | synthesis | overview | thesis | methodology | finding
title: "Human-readable title"
tags: []
related: []
sources: []
created: YYYY-MM-DD
updated: YYYY-MM-DD
---
```

Source pages also include:

```yaml
authors: []
year: YYYY
url: ""
venue: ""
```

Thesis pages also include:

```yaml
confidence: low | medium | high
status: speculative | supported | refuted | settled
```

Finding pages also include:

```yaml
source: "[[sources/source-slug]]"
evidence: "section, equation, figure, table, or page anchor"
confidence: low | medium | high
replicated: true | false | null
```

## Type Selection Rules

- Use `concept` for reusable knowledge, not a single product's implementation detail.
- Use `entity` for named products, systems, organizations, people, and standards.
- Use `finding` only for an evidence-anchored observation or measured result.
- Use `methodology` for a reusable procedure with rationale and verification steps.
- Use `thesis` only for a falsifiable, evolving cross-source hypothesis.
- Use `comparison` only when the source makes a genuine multi-dimensional contrast.
- Use `synthesis` only for cross-source integration; one source alone is not synthesis.
- Ingest never invents user goals, decisions, findings, or hypotheses absent from evidence.

## Index Format

`wiki/index.md` lists all pages grouped by type:

```text
- [[type/page-slug]] — one-line description
```

## Log Format

`wiki/log.md` records activity in reverse chronological order:

```text
## YYYY-MM-DD

- Action taken / finding noted
```

## Cross-referencing Rules

- Use `[[type/page-slug]]` when the bare stem is ambiguous.
- Findings link to their source and cite evidence anchors.
- Thesis pages reference supporting and refuting findings through `related:`.
- Methodology pages are cited by findings or concepts that apply them.
- Synthesis pages cite every contributing source.

## Contradiction Handling

1. Record the disagreement without silently choosing a winner.
2. Link all conflicting source pages.
3. Create a review/query item when evidence is insufficient.
4. Update a thesis or synthesis page only after cross-source evaluation.

## Machine-Readable Naming Rules

This improved-wiki extension is consumed only by Stage 0.1 and is excluded from
LLM schema context. Tailor it to the project's actual raw folders.

```yaml
forbidden_chars:
  - ","
  - "，"
rules:
  Book:
    pattern: "Title - Year - Author"
    min_parts: 3
    year_field: 1
    author_field: -1
    surname_only: true
  Paper:
    extends: Book
  Presentation:
    extends: Book
```
