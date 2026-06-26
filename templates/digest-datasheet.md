# digest-datasheet.md — Ingest template for component datasheets

## LLM Instructions (injected first — always read before generating)

### Chunk Analysis Rules
- For EVERY numerical claim (voltages, timeouts, timer formulas, bit-widths, currents): add
  `table_ref: "Table N"` and `page_ref: "p.NN"` inside the `claims` entry.
- In `formulas`: transcribe the exact expression from the datasheet in LaTeX — do NOT
  paraphrase. Define every variable. Example: `T = \frac{4096 \times 2^{WDGTB} \times (T[5:0]+1)}{f_{CPU}}`
- When narrative text conflicts with a table, **the table wins**. Note the conflict in the claim's `evidence`.
- For register/bit-field specs: treat each bit-field row as a separate claim with its own `table_ref`.

### Generation Constraints (applies to all FILE blocks)
1. `key_specs` MUST be a markdown table with columns: Parameter | Min | Typ | Max | Unit | Conditions
2. `pin_function_summary` MUST be a markdown table: Pin | Name | Type | Function
3. Every numerical spec value MUST come verbatim from the datasheet — never from general knowledge.
4. Extract 3–7 genuine differentiators; do NOT copy the full marketing feature list.
5. Every part number ALWAYS gets `wiki/entities/<part-number>.md` — this is non-negotiable.
6. Manufacturer entity page: create only if it does not already exist in the linkable slugs list.

---

## What the LLM is asked to produce

> **Use this template** when a file lives at `raw/Datasheet/<...>/*.pdf`.
> Datasheets are heavily structured (tables, figures, pinouts). Focus on the key specs table,
> electrical characteristics, pin-function summary, and typical application topology.
> Output: source page + entity page(s) + 2–5 concept pages.

### Step 1: Chunk Analysis YAML schema

```yaml
part_meta:
  part_number: "<e.g. TPS54360>"
  manufacturer: "<e.g. Texas Instruments>"
  family: "<e.g. TPS54360 family — buck converters>"
  package: "<e.g. HTSSOP-8>"
  datasheet_rev: "<e.g. SLVSAS5A — March 2018>"
  status: "active" | "NRND" | "obsolete" | "preview"

key_specs:
  category: "operating_rating" | "electrical_char" | "thermal" | "package"
  specs:
    - parameter: "<e.g. Input voltage range>"
      min: "<value>"
      typ: "<value>"
      max: "<value>"
      unit: "<V>"
      conditions: "<e.g. TA = 25°C>"
      table_ref: "<e.g. Table 3 Absolute Maximum Ratings>"   # REQUIRED

pin_function_summary:
  - pin: "<name + number, e.g. VIN (pin 1)>"
    function: "<one-line description>"

typical_application:
  topology: "<e.g. Synchronous Buck Converter>"
  input_range: "<e.g. 4.5V to 60V>"
  output: "<e.g. 3.3V @ 3.5A>"
  switching_frequency: "<e.g. 100kHz to 2.5MHz>"
  key_passive_components:
    - role: "input cap"
      typical_value: "<e.g. 1µF X7R>"

features:
  - "Wide input voltage range: 4.5V to 60V"
  differentiators: ["..."]  # only genuinely distinctive, max 5

protection_features:
  - "OCP (over-current protection)"

applications_marketed:
  - "Industrial PLC"

key_entities:
  - name: "<e.g. Texas Instruments>"
    role: "organization"
    wikilink_target: "Texas-Instruments"

key_concepts:
  - name: "Synchronous Buck Converter"
    importance: "core"
    wikilink_target: "synchronous-buck-converter"

key_claims:
  - claim: "Efficiency peaks at 95% at 24Vin → 5Vout @ 1A"
    evidence: "Fig 6-1 (efficiency curve)"
    table_ref: "Figure 6-1"   # REQUIRED for any numerical claim
    page_ref: "p.7"           # REQUIRED for any numerical claim
    confidence: "high"

formulas:
  - formula: "LaTeX expression — exact from datasheet, all variables defined"
    meaning: "..."
    table_ref: "Table N"      # cite source table/figure

known_limitations:
  - "No mention of EMI performance"

companion_documents:
  - name: "TPS54360EVM User's Guide"
    url: "<slvu..."
```

### Step 2: Generation — Files to write

1. **`wiki/sources/<Mfr> - <Part-Number>.md`** — source page
   - First section after frontmatter: key specs markdown table (not bullet list)
   - Then: pin function summary table, typical application, features/protection
   - Last section: "参见" with concept + entity wikilinks

2. **`wiki/entities/<Part-Number>.md`** — entity page for the part (ALWAYS required)
   - Frontmatter: `type: entity`
   - Body: brief description, package, datasheet rev, status, key specs summary table, "参见"

3. **`wiki/entities/<Manufacturer>.md`** — manufacturer entity (only if not in linkable slugs)

4. **`wiki/concepts/<slug>.md`** — 2–5 concept pages for key concepts the datasheet uses

---

## Type-specific guidance

- **Tables over narrative**: spec tables are the source of truth; narrative may be simplified.
- **Don't bloat with marketing**: 20+ feature bullets → extract 3–7 genuine differentiators.
- **Part-number as entity**: makes parts linkable from design pages ("used in design X").
- **Companion docs**: if the user already ingested matching app notes / reference designs,
  the datasheet source page should wikilink them.

---

## Common pitfalls

| Symptom | Fix |
|---|---|
| Spec values wrong / hallucinated | Every spec must cite table_ref + page_ref |
| Pin table missing or wrong | Pin tables are tabular; if OCR failed, flag as OCR-failed source |
| Timer formula paraphrased | Transcribe LaTeX verbatim; never approximate |
| 10+ marketing feature bullets | Cap at 5 differentiators |
| Ordering-information table variants | Entity page = family; only ingest specific part |

---

## See also

- `SKILL.md` §5, §6
- `templates/digest-applicationnote.md` — vendor app notes (typical companion doc)
- `templates/digest-designexample.md` — reference designs
