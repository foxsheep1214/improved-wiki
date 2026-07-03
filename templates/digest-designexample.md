# digest-designexample.md — Ingest template for reference designs

> **Use this template** when a file lives at `raw/Designexample/<...>/*.pdf`.
> Reference designs (TI PMPxxxx, ADI EVAL-xxx) are "complete circuit + BOM + measurement" documents. Focus on the topology, key components, design tradeoffs, and measured performance.

---

## What the LLM is asked to produce

### Step 1: Analysis

```yaml
design_meta:
  reference_design_id: "<e.g. PMP8861>"
  manufacturer: "<e.g. Texas Instruments>"
  title: "<e.g. 200W Active Clamp Forward Converter>"
  topology: "<e.g. Active Clamp Forward, Synchronous Rectification>"
  pages: <int>
  publication_date: "<if known>"

specifications:
  input:
    voltage_min: "<e.g. 36V>"
    voltage_max: "<e.g. 75V>"
    voltage_nominal: "<e.g. 48V>"
  output:
    voltage: "<e.g. 12V>"
    current_max: "<e.g. 16.7A>"
    power_max: "<e.g. 200W>"
  switching_frequency: "<e.g. 200kHz>"
  efficiency_target: "<e.g. >93% at full load>"
  isolation: "<e.g. 1500VAC>"

measured_performance:
  efficiency:
    peak: "<e.g. 95.2%>"
    at_load: "<e.g. at 50% load, 48Vin>"
    figure_ref: "Fig 4 efficiency curve"
  thermal:
    max_temp_rise: "<e.g. 45°C above ambient at full load>"
  load_transient:
    response: "<e.g. <5% Vout deviation on 50% load step>"

key_components:
  - part_number: "<e.g. UCC2897A>"
    role: "<e.g. Active clamp forward controller>"
    wikilink_target: "UCC2897A"  # should be a cross-ref to its datasheet page
  - part_number: "<e.g. FDMS86201>"
    role: "<e.g. Primary-side MOSFET>"
    wikilink_target: "FDMS86201"
  - part_number: "<...>"
    role: "<...>"

# The design "story" — why each part was chosen
design_decisions:
  - decision: "Use active clamp topology (vs hard-switched forward)"
    reason: "Reduces switching losses, allows higher frequency / smaller magnetics"
    tradeoff: "More complex control, additional MOSFET + capacitor"
  - decision: "<e.g. Use synchronous rectification on secondary>"
    reason: "<e.g. Reduce rectification losses at high output current>"
  - decision: "<...>"

# Lessons learned / gotchas
design_notes:
  - "Layout: keep the current sense resistor trace short and direct to the controller GND"
  - "Magnetics: use litz wire for the transformer primary to reduce AC losses at 200kHz"
  - "..."

schematic_description:
  # Description of the schematic (we don't extract the schematic as an image)
  blocks:
    - name: "Input filter"
      components: ["input cap", "common-mode choke"]
    - name: "Primary switching"
      components: ["main MOSFET", "clamp MOSFET", "clamp capacitor"]
    - name: "Transformer"
      turns_ratio: "<e.g. 4:1>"
    - name: "Secondary rectification"
      components: ["synchronous rectifier MOSFETs", "output inductor"]
    - name: "Control"
      components: ["UCC2897A", "feedback network", "compensation"]

# What's NOT in the reference design (for honest wiki content)
limitations:
  - "Design validated at 25°C ambient only; thermal derating at higher temp not characterized"

key_entities:
  - name: "<reference design id>"
    wikilink_target: "<stem>"
  - name: "<manufacturer>"
    wikilink_target: "<existing-slug>"

key_concepts:
  - name: "<e.g. Active Clamp Forward Topology>"
    importance: "core"
    wikilink_target: "active-clamp-forward"
  - name: "<e.g. Synchronous Rectification>"
    importance: "supporting"
    wikilink_target: "synchronous-rectification"
  - name: "<e.g. Transformer Design>"
    importance: "supporting"
    wikilink_target: "..."

key_claims:
  - claim: "Efficiency peaks at 95.2% at 50% load, 48Vin"
    evidence: "Fig 4 efficiency curve, p. 6"

connections_to_existing_wiki:
  - existing_page: "<datasheet of UCC2897A>"
    relationship: "applies"  # the design uses this controller
  - existing_page: "<active-clamp-forward concept page>"
    relationship: "illustrates"  # this design is a concrete example
```

### Step 2: Generation

Files to write:

1. **`wiki/sources/<Mfr> - <Ref-Design-Name>.md`** — source page
   - Body: design ID, specifications, measured performance, schematic description, key components table, design decisions, design notes, "参见"

2. **`wiki/concepts/<slug>.md`** — 1-5 concept pages (the topology, key sub-circuits)
   - These are higher-value than the source page: they capture the design knowledge in a reusable form

3. **Update `wiki/index.md`**, **`wiki/log.md`**, **`wiki/overview.md`**

---

## Type-specific guidance

- **Reference designs are concrete examples** of concept pages. The highest value is: (1) the topology concept page, (2) the key_components cross-refs back to datasheets, (3) the measured_performance numbers.
- **Don't extract schematics as images**: the LLM can't reliably read schematics. Use `schematic_description.blocks` to describe the block structure in words.
- **Design notes are gold**: lessons learned from the designer. Always extract them.

---

## Common pitfalls when ingesting reference designs

| Symptom | Fix |
|---|---|
| LLM extracts the BOM but not the design rationale | Force: "Each key_component must have a `role` field AND a `why_this_part` field. Why was THIS part chosen over alternatives?" |
| Measured numbers are wrong (LLM misread curve) | Use absolute format: "value at <specific load, input voltage, frequency>". Don't approximate |
| Schematic description is too vague | Force: "Each block must list its components by name. The transformer block must have turns_ratio, inductance, frequency rating" |

---

## See also

- `references/naming-conventions.md` — frontmatter schema + wikilink naming
- `templates/digest-datasheet.md` — for each key_component's datasheet
- `templates/digest-applicationnote.md` — for the design rationale of sub-circuits
