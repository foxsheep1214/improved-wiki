
from _stage_2_base import *

# Stage 2.9: in-source concept comparison pages.


def _stage_2_9_build_prompt_in_source(
    concept_titles: list[str],
    file_path: Path,
    config: Config,
    source_context: str = "",
) -> str:
    """Build prompt for Stage 2.9: in-source concept comparisons."""
    concepts_with_desc = '\n'.join(f"- {c}" for c in concept_titles[:60])
    today_str = time.strftime("%Y-%m-%d")
    try:
        raw_rel = str(file_path.relative_to(config.raw_root))
    except ValueError:
        raw_rel = file_path.name

    # P1 parity with Stage 2.4 (2026-06-27): ground comparisons in the raw source
    # so the LLM only contrasts what the SOURCE itself sets side-by-side, using the
    # source's own dimensions/figures — not comparisons fabricated from memory.
    if source_context.strip():
        source_section = (
            "\n# Source Text (ground comparisons in THIS — do not invent from memory)\n"
            "Only build a comparison the SOURCE actually makes; use its own contrast\n"
            "dimensions, figures, and wording. Never fabricate a side-by-side the\n"
            "source does not draw.\n"
            "<source>\n"
            f"{source_context}\n"
            "</source>\n"
        )
    else:
        source_section = ""

    return f"""# Role
You are maintaining a wiki knowledge base. Review the concepts just generated for a book.

# Source
{file_path.stem} (raw/{raw_rel})

# Generated Concepts
{concepts_with_desc}
{source_section}
# Task
Identify **comparison groups** — two OR MORE concepts that the source sets
side-by-side, where understanding them together illuminates each one.
Good candidates:
- Two choices on the same dimension (CCM vs DCM, Buck vs Boost, Voltage Mode vs Current Mode)
- Commonly confused pairs (EMI vs EMC, SNR vs SINAD, PSRR vs CMRR)
- Explicitly contrasted in the book
- **A systematic multi-way comparison (3+ alternatives)** that THE SOURCE ITSELF
  benchmarks against each other across multiple dimensions — e.g. Phased-Array vs
  MIMO vs Phased-MIMO across SINR / beampattern / sidelobe. This is the headline
  page for any source whose central contribution is positioning a new method
  against the existing alternatives; do NOT skip it just because there are 3+ items.

Bad candidates:
- Upstream/downstream relationships (MOSFET → Gate Driver)
- Parent/child relationships (DC-DC Converter → Buck Converter)
- An arbitrary list of unrelated concepts that the source never actually contrasts

Generate at most 3 comparison pages. Output 0 if no genuine comparison exists.

# Output Format
# (For a 3+ way comparison, just add more columns to the table and more
#  items to title / related / See Also — "A vs B vs C".)
---FILE:wiki/comparisons/{{slug}}.md---
---
type: comparison
title: "{{Concept A}} vs {{Concept B}}"
tags: [{{2-4 tags}}]
related: [{{concept-A-stem}}, {{concept-B-stem}}]
sources: ["raw/{raw_rel}"]
created: {today_str}
updated: {today_str}
---

# {{Concept A}} vs {{Concept B}}

## Why Compare
{{1-2 sentences: why these benefit from side-by-side understanding}}

## Comparison Table
| Dimension | {{Concept A}} | {{Concept B}} |
|-----------|---------------|---------------|
| {{dim 1: e.g. operating principle}} | | |
| {{dim 2: e.g. key characteristic}} | | |
| {{dim 3: e.g. typical application}} | | |
| {{dim 4: e.g. advantages/disadvantages}} | | |

## Selection Guide
{{When to choose each — 2-3 specific recommendations}}

## See Also
- [[{{Concept A}}]] — {{one-line description}}
- [[{{Concept B}}]] — {{one-line description}}
---END FILE---

If no good comparison pair exists, output:
---COMPARISONS_IN_SOURCE: 0---
---END COMPARISONS_IN_SOURCE---

START IMMEDIATELY with ---FILE: or ---COMPARISONS_IN_SOURCE: — no preamble.
"""


def stage_2_9_comparison_generation(
    global_digest: dict,
    chunk_analyses: list[dict],
    file_blocks: list[tuple[str, str]],
    file_path: Path,
    config: Config,
    template: str = "",
    verbose: bool = False,
    source_context: str = "",
) -> tuple[list[tuple[str, str]], str]:
    """Stage 2.9: Generate in-source concept comparison pages.

    Returns (new_comparison_blocks, raw_response).
    Skips when fewer than 2 concepts were generated (no pair to compare).
    """
    # Get concept/entity titles from generated file blocks
    concept_titles: list[str] = []
    entity_titles: list[str] = []
    for path, _ in file_blocks:
        if path.startswith("concepts/"):
            concept_titles.append(path.replace("concepts/", "").replace(".md", ""))
        elif path.startswith("entities/"):
            entity_titles.append(path.replace("entities/", "").replace(".md", ""))

    if not concept_titles and not entity_titles:
        if verbose:
            print("[stage 2.9] Skipped — no concepts/entities generated")
        return [], ""

    comp_tokens = config.compute_max_tokens(4096)
    all_blocks: list[tuple[str, str]] = []

    # In-source concept comparison
    response = ""
    if len(concept_titles) >= 2:
        if verbose:
            print(f"[stage 2.9] In-source comparison — {len(concept_titles)} concepts...")
        prompt = _stage_2_9_build_prompt_in_source(
            concept_titles, file_path, config,
            source_context=source_context,
        )
        try:
            response, _stop = call_anthropic_protocol(prompt, config, max_tokens=comp_tokens)
        except Exception as e:
            print(f"[stage 2.9] LLM call failed: {e}")
            response = ""
        if response:
            blocks = parse_file_blocks(response)
            if blocks:
                print(f"[stage 2.9] {len(blocks)} comparison page(s)")
                for path, _ in blocks:
                    print(f"  → {path}")
                all_blocks.extend(blocks)
            else:
                print("[stage 2.9] no comparison pairs found")
    else:
        if verbose:
            print("[stage 2.9] skipped — fewer than 2 concepts")

    if all_blocks:
        print(f"[stage 2.9] Total: {len(all_blocks)} comparison page(s)")
    else:
        print("[stage 2.9] No comparisons generated")

    return all_blocks, response
