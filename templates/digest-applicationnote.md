# digest-applicationnote.md — Ingest template for vendor application notes

> **Use this template** when a file lives at `raw/Applicationnote/<...>/*.pdf`.
> App notes (TI SLVAxxx, ADI CN-xxxx, etc.) are "how to design X" documents. Focus on the design procedure, calculations, and worked example. Often pairs with a datasheet (cross-ref).

---

## What the LLM is asked to produce

### Step 1: Analysis

```yaml
appnote_meta:
  part_number: "<AN id, e.g. SLVA477>"
  manufacturer: "<e.g. Texas Instruments>"
  title: "<full title>"
  topic: "<one-line description, e.g. Synchronous buck loop compensation>"
  pages: <int>
  publication_date: "<if known>"

target_audience: "<e.g. power supply design engineer>"
prerequisite_knowledge: ["..."]  # what the reader should know

problem_solved:
  # The design problem the app note addresses
  statement: "..."
  why_it_matters: "..."
  who_uses_this: "..."

design_procedure:
  # The actual step-by-step procedure (this is the meat of the app note)
  steps:
    - step: 1
      name: "<e.g. Determine converter topology>"
      what_to_do: "..."
      formulas:
        - equation: "<LaTeX>"
          purpose: "..."
      decision_points:
        - condition: "<e.g. Vin_max > 30V>"
          choose: "<e.g. use HV buck controller>"
  worked_example:
    # A specific numerical example from the app note
    input_parameters: {...}
    calculation_walkthrough:
      - step: 1
        computation: "..."
        result: "..."

key_equations:
  - equation: "<LaTeX>"
    purpose: "..."
    where_in_appnote: "<section / page>"
  - equation: "..."

design_tradeoffs:
  - tradeoff: "efficiency vs cost"
    how_to_decide: "..."
  - tradeoff: "..."

components_used_in_example:
  - "<e.g. TPS54360 buck converter>"
  - "<e.g. 10µH inductor, XAL series>"

key_entities:
  - name: "<AN id>"
    wikilink_target: "SLVA477"
  - name: "<manufacturer>"
    wikilink_target: "<existing-slug>"

key_concepts:
  - name: "<e.g. Loop compensation design>"
    importance: "core"
    wikilink_target: "loop-compensation"
  - name: "<e.g. Type II compensator>"
    importance: "core"
    wikilink_target: "type-ii-compensator"
  # 1-3 concepts expected

key_claims:
  - claim: "<e.g. 10µH gives best efficiency at 500kHz / 1A load>"
    evidence: "Fig 12 efficiency curve"

connections_to_existing_wiki:
  - existing_page: "<e.g. TPS54360 datasheet wikilink>"
    relationship: "applies"  # the app note applies the datasheet's product
  - existing_page: "<e.g. synchronous-buck-converter concept page>"
    relationship: "extends"
```

### Step 2: Generation

Files to write:

1. **`wiki/sources/<Mfr> - <AN-Number> - <Topic>.md`** — source page
   - Body: app note metadata, problem, design procedure, key equations, worked example summary, components used, "参见" with concept + entity + datasheet pages

2. **`wiki/concepts/<slug>.md`** — 1-3 concept pages (the design concepts the app note teaches)
   - These are the high-value outputs: each app note should add 1-3 reusable design knowledge pages

3. **`wiki/entities/<Mfr>.md`** — entity page for the manufacturer (if not already exists)

4. **Update `wiki/index.md`**, **`wiki/log.md`**, **`wiki/overview.md`**

---

## Type-specific guidance

- **App notes are "design knowledge" not "marketing"**: extract the design procedure as the primary content, not the marketing wrap-up.
- **Worked examples are gold**: a worked example is more useful than the abstract procedure. Always include the input parameters and walkthrough.
- **Cross-ref to datasheet**: most app notes reference a specific part number. Make sure the datasheet's source page (if already in wiki) gets a `related: [...]` link back to the app note.

---

## Common pitfalls when ingesting app notes

| Symptom | Fix |
|---|---|
| The "design procedure" is summarized away | Tighten prompt: "Preserve the step structure. Each step gets a `what_to_do` bullet, a `formulas` block, and a `decision_points` list" |
| Worked example is paraphrased instead of preserved | Force: "All input parameters must appear verbatim. The walkthrough steps must use the same numbers as the source" |
| App note's marketing conclusion overpowers the technical content | Don't extract "summary" or "conclusion" sections that are pure marketing. Only extract the engineering parts |

---

## See also

- `references/naming-conventions.md` — frontmatter schema + wikilink naming
- `templates/digest-datasheet.md` — the typical companion (datasheet of the part used)
- `templates/digest-designexample.md` — the typical companion (reference design implementing the procedure)
