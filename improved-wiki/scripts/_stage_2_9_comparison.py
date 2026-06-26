
from _stage_2_base import *

# Stage 2.9: in-source concept comparison pages.


def _stage_2_9_build_prompt_in_source(
    concept_titles: list[str],
    file_path: Path,
    config: Config,
    current_domain: str = "general",
) -> str:
    """Build prompt for Stage 2.9: in-source concept comparisons."""
    concepts_with_desc = '\n'.join(f"- {c}" for c in concept_titles[:60])
    today_str = time.strftime("%Y-%m-%d")
    try:
        raw_rel = str(file_path.relative_to(config.raw_root))
    except ValueError:
        raw_rel = file_path.name

    return f"""# Role
You are maintaining a wiki knowledge base. Review the concepts just generated for a book.

# Current Domain
{current_domain}

# Source
{file_path.stem} (raw/{raw_rel})

# Generated Concepts
{concepts_with_desc}

# Task
Identify pairs of concepts that are **naturally compared** — understanding one illuminates the other.
Good candidates:
- Two choices on the same dimension (CCM vs DCM, Buck vs Boost, Voltage Mode vs Current Mode)
- Commonly confused pairs (EMI vs EMC, SNR vs SINAD, PSRR vs CMRR)
- Explicitly contrasted in the book

Bad candidates:
- Upstream/downstream relationships (MOSFET → Gate Driver)
- Parent/child relationships (DC-DC Converter → Buck Converter)
- Three or more items → NOT a comparison

Generate at most 2 comparisons. Output 0 if no good pair exists.

# Output Format
---FILE:wiki/comparisons/{{slug}}.md---
---
type: comparison
title: "{{Concept A}} vs {{Concept B}}"
domain: {current_domain}
tags: [{{2-4 tags}}]
related: [{{concept-A-stem}}, {{concept-B-stem}}]
sources: ["raw/{raw_rel}"]
created: {today_str}
updated: {today_str}
---

# {{Concept A}} vs {{Concept B}}

## Why Compare
{{1-2 sentences: why these two benefit from side-by-side understanding}}

## Comparison Table
| Dimension | {{Concept A}} | {{Concept B}} |
|-----------|---------------|---------------|
| {{dim 1: e.g. operating principle}} | | |
| {{dim 2: e.g. key characteristic}} | | |
| {{dim 3: e.g. typical application}} | | |
| {{dim 4: e.g. advantages/disadvantages}} | | |

## Selection Guide
{{When to choose A vs B — 2-3 specific recommendations}}

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

    current_domain = (
        global_digest.get("book_meta", {}).get("domain", "general")
        if isinstance(global_digest.get("book_meta"), dict)
        else "general"
    )
    comp_tokens = config.compute_max_tokens(4096)
    all_blocks: list[tuple[str, str]] = []

    # 2.9B: In-source concept comparison
    response_29b = ""
    if len(concept_titles) >= 2:
        if verbose:
            print(f"[stage 2.9] In-source comparison — {len(concept_titles)} concepts...")
        prompt_29b = _stage_2_9_build_prompt_in_source(
            concept_titles, file_path, config, current_domain
        )
        try:
            response_29b, _stop_29b = call_anthropic_protocol(prompt_29b, config, max_tokens=comp_tokens)
        except Exception as e:
            print(f"[stage 2.9] LLM call failed: {e}")
            response_29b = ""
        if response_29b:
            blocks_29b = parse_file_blocks(response_29b)
            if blocks_29b:
                print(f"[stage 2.9] {len(blocks_29b)} comparison page(s)")
                for path, _ in blocks_29b:
                    print(f"  → {path}")
                all_blocks.extend(blocks_29b)
            else:
                print("[stage 2.9] no comparison pairs found")
    else:
        if verbose:
            print("[stage 2.9] skipped — fewer than 2 concepts")

    if all_blocks:
        print(f"[stage 2.9] Total: {len(all_blocks)} comparison page(s)")
    else:
        print("[stage 2.9] No comparisons generated")

    return all_blocks, response_29b
