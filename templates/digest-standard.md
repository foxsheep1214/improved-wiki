# digest-standard.md — Ingest template for industry standards

> **Use this template** when a file lives at `raw/Standard/<...>/*.pdf`.
> Standards (IEEE, IEC, MIL-STD, GB) are formal normative documents. Focus on clause structure, definitions, and conformance criteria. Often produces many "term" pages.

---

## What the LLM is asked to produce

### Step 1: Analysis

```yaml
standard_meta:
  standard_number: "<e.g. IEEE 802.11ax-2021>"
  sdo: "<Standards Developing Organization, e.g. IEEE>"
  title: "<full title>"
  publication_year: <int>
  status: "active" | "superseded" | "withdrawn" | "draft"
  supersedes: "<previous version, e.g. IEEE 802.11n-2009>"
  pages: <int>

scope:
  purpose: "<what this standard defines>"
  applies_to: "<products / systems / methods that must comply>"

clause_structure:
  # The standard's table of contents (just major clauses, not all subclauses)
  - clause: 1
    title: "Overview"
  - clause: 2
    title: "Normative references"
  - clause: 3
    title: "Definitions"
  - clause: 4
    title: "<...>"
  # ...

key_definitions:
  # The glossary entries — these become concept pages
  - term: "<e.g. MIB (Management Information Base)>"
    definition: "<verbatim from clause 3.1>"
    clause: "3.1"
  - term: "<...>"
    definition: "<...>"
    clause: "<...>"

key_requirements:
  # The "shall" / "should" / "may" normative statements
  - requirement: "<verbatim from the standard>"
    clause: "<e.g. 4.5.2>"
    mandatory: "shall" | "should" | "may"
    test_method: "<how compliance is verified>"
  - requirement: "<...>"

conformance_criteria:
  # What it takes to claim compliance
  - "<e.g. Pass all 'shall' requirements in clause 4>"
  - "<e.g. Pass the test suite in Annex A>"

deprecated_or_removed:
  # What was removed vs the prior version
  - "<e.g. Clause 11 (HT) was removed in this revision>"

key_entities:
  - name: "<SDO, e.g. IEEE>"
    wikilink_target: "IEEE"
  - name: "<Working Group, e.g. IEEE 802.11>"
    wikilink_target: "IEEE-802-11-WG"
  - name: "<Standard number>"
    wikilink_target: "<stem>"

key_concepts:
  # Terms / definitions that deserve their own pages
  - name: "<term>"
    importance: "core"  # if it's a foundational definition
    wikilink_target: "<term-slug>"
  - name: "<...>"
    importance: "supporting"

key_claims:
  - claim: "<e.g. Mandatory support for 1024-QAM>"
    evidence: "Clause 27.2, p. 1234"

connections_to_existing_wiki:
  - existing_page: "<a related standard or concept>"
    relationship: "extends" | "supersedes" | "contrasts" | "cites"
```

### Step 2: Generation

Files to write:

1. **`wiki/sources/<SDO> - <Std-Number> - <Year>.md`** — source page
   - Body: standard metadata, scope, clause structure, key requirements table, conformance criteria, "参见"

2. **`wiki/concepts/<term-slug>.md`** — one page per **key definition** in the glossary
   - Standards often have 30-100 definitions; extract the 5-15 most important ones as concept pages

3. **`wiki/entities/<SDO>.md`** — entity page for the SDO (if not already in wiki)

4. **Update `wiki/index.md`**, **`wiki/log.md`**, **`wiki/overview.md`**

---

## Prompt template (the actual prompt sent to the LLM)

```
# Role
You are the LLM maintainer of a Karpathy-pattern personal knowledge base.
You ingest industry standards (IEEE, IEC, MIL-STD, GB, etc.) into a structured wiki.

# Input
- Standard number: {std_number}
- SDO: {sdo}
- Title: {title}
- File path: {raw_path}
- Extracted text: <full text in <extracted_text>...</extracted_text>>
- Existing wiki context: <slugs in <existing_wiki>...</existing_wiki>>

# Task
Two-step chain.

## Step 1: Analysis
YAML block with the full analysis. Use the schema in §Analysis above.
A standard is expected to produce 1 source page + 5-15 concept pages (the key glossary terms).

## Step 2: Generation
File contents in order:
### File 1: wiki/sources/<SDO> - <Std-Number> - <Year>.md
### File 2..N: wiki/concepts/<term-slug>.md (5-15 glossary term pages)
### File N+1 (optional): wiki/entities/<SDO>.md
### Update: wiki/index.md
### Append: wiki/log.md

# Constraints
- Every `[[wikilink]]` MUST use the FULL filename stem (per improved-wiki §6.2)
- Frontmatter must follow improved-wiki §5
- Quoted definitions and requirements must be verbatim from the source
- Use a markdown table for the clause_structure
- Use a markdown table for key_requirements (requirement | clause | mandatory | test_method)
- Mark which terms have a dedicated concept page vs. which are just mentioned in the source page
- If the standard is in a language other than English, keep the original term and add an English translation in parentheses
```

---

## Type-specific guidance

- **Standards are usually long and dense**: don't try to extract every clause. Focus on the **definitions** (clause 3) and the **key "shall" requirements**.
- **Verbatim quotes matter**: a standard's normative text is often legally binding. Quote it exactly. Don't paraphrase.
- **Cross-ref to prior versions**: if the standard supersedes a prior version, link the prior version's source page (if it's in the wiki).
- **Conformance criteria**: these are the testable requirements. They're often the most cited part of a standard.

---

## Common pitfalls when ingesting standards

| Symptom | Fix |
|---|---|
| LLM paraphrases normative "shall" statements | Force: "Every requirement field must be a VERBATIM copy of the standard's text. Do not rephrase" |
| Too many concept pages (one per term) | Filter by `importance: "core"` in the analysis. Standard defines 100 terms, but only 5-15 are worth their own page |
| Clause structure is incomplete | Standards are large. The LLM may only read the first 30 pages. Prompt: "Make sure you have the full TOC. If not, say which clauses are missing" |

---

## See also

- `SKILL.md` §5, §6
- `templates/digest-paper.md` — for academic papers (different focus on methodology vs. requirements)
