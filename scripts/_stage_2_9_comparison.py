
from _stage_2_base import *
from _language import build_language_directive, get_output_language

# Stage 2.9: in-source concept comparison pages.

# D6 (user ruling 2026-07-02): section headings follow the CONTENT language.
# Fixed vocabularies (machine-parsable) — Chinese sources use the Chinese set,
# everything else keeps the English set. Downstream heading greps must accept
# BOTH vocabularies (see references/comparison-generation.md 验证命令).
_COMPARISON_HEADINGS_EN = ("Why Compare", "Comparison Table", "Selection Guide", "See Also")
_COMPARISON_HEADINGS_ZH = ("为何对比", "对比表", "选型指南", "参见")


def _stage_2_9_headings(language_sample: str) -> tuple[str, str, str, str]:
    """(why, table, guide, see-also) headings for the sample's output language."""
    if get_output_language(language_sample) == "Chinese":
        return _COMPARISON_HEADINGS_ZH
    return _COMPARISON_HEADINGS_EN


def _stage_2_9_comparison_cap(chapter_count: int) -> int:
    """Per-book comparison-page cap, scaled with chapter count (A6, audit H2).

    The flat "at most 3" cap starved big books — a 26-chapter handbook got the
    same budget as a 5-chapter booklet (13 calls hit the cap 7 times)."""
    return min(8, 3 + chapter_count // 8)


def _existing_comparisons(config: Config) -> list[tuple[str, str]]:
    """(slug, title) pairs for existing wiki/comparisons/ pages (B4, audit H1).

    2.9 never saw the wiki's existing comparisons, so cross-book/cross-language
    twins accumulated nightly (mti-vs-pulse-doppler vs mti-vs-脉冲多普勒, 4 live
    duplicate groups). Injecting slug+title lets the prompt forbid same-topic
    re-creation. Sorted glob → deterministic prompt (stable handoff cache key).
    """
    comp_dir = config.wiki_dir / "comparisons"
    if not comp_dir.is_dir():
        return []
    out: list[tuple[str, str]] = []
    for f in sorted(comp_dir.glob("*.md")):
        if f.stem == "index" or f.stem.startswith("_"):
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        title = _stage_2_frontmatter_title(content)
        out.append((f.stem, title or f.stem))
    return out


def _stage_2_9_build_prompt_in_source(
    concept_titles: list[str],
    file_path: Path,
    config: Config,
    source_context: str = "",
    comp_cap: int = 3,
) -> str:
    """Build prompt for Stage 2.9: in-source concept comparisons."""
    # [:60] hid most of a large book's concepts from comparison candidates
    # (alphabetical cut, 2026-07-02). Titles are cheap; 300 covers real books.
    concepts_with_desc = '\n'.join(f"- {c}" for c in concept_titles[:300])
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

    # B4 (audit H1): show existing comparisons so same-topic twins are refused.
    existing_comps = _existing_comparisons(config)
    if existing_comps:
        comp_lines = "\n".join(f"  - comparisons/{s} — {t}" for s, t in existing_comps[:100])
        existing_comps_section = (
            "\n# Existing comparison pages already in the wiki (do NOT create twins)\n"
            f"{comp_lines}\n"
            "If a comparison you would build ALREADY exists above (same items, even in\n"
            "another language), output 0 for it — or emit it ONLY to add genuinely\n"
            "missing dimensions. If a generated concept page above already IS a full\n"
            "comparison page, reference that page instead of creating a twin.\n"
        )
    else:
        existing_comps_section = ""

    language_sample = source_context or concepts_with_desc
    language_directive = build_language_directive(language_sample)
    h_why, h_table, h_guide, h_see = _stage_2_9_headings(language_sample)
    return f"""{language_directive}

# Role
You are maintaining a wiki knowledge base. Review the concepts just generated for a book.

# Source
{file_path.stem} (raw/{raw_rel})

# Generated Concepts
{concepts_with_desc}
{source_section}{existing_comps_section}
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

Generate at most {comp_cap} comparison pages. Output 0 if no genuine comparison exists.

Evidence anchors: every number/figure in the comparison table cites its chapter/
section/equation/figure number (式(5-10), 图2.6, Table 8.1); a value read off a
figure's curve must be marked "据图X.X".

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

## {h_why}
{{1-2 sentences: why these benefit from side-by-side understanding}}

## {h_table}
| Dimension | {{Concept A}} | {{Concept B}} |
|-----------|---------------|---------------|
| {{dim 1: e.g. operating principle}} | | |
| {{dim 2: e.g. key characteristic}} | | |
| {{dim 3: e.g. typical application}} | | |
| {{dim 4: e.g. advantages/disadvantages}} | | |

## {h_guide}
{{When to choose each — 2-3 specific recommendations}}

## {h_see}
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
    chapter_count: int = 0,
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
        comp_cap = _stage_2_9_comparison_cap(chapter_count)
        if verbose:
            print(f"[stage 2.9] In-source comparison — {len(concept_titles)} concepts, "
                  f"cap {comp_cap} ({chapter_count} chapters)...")
        prompt = _stage_2_9_build_prompt_in_source(
            concept_titles, file_path, config,
            source_context=source_context, comp_cap=comp_cap,
        )
        _stop = ""
        try:
            response, _stop = call_anthropic_protocol(prompt, config, max_tokens=comp_tokens)
        except Exception as e:
            print(f"[stage 2.9] LLM call failed: {e}")
            response = ""
        # A6: a max_tokens stop means the tail comparison block was cut and
        # would be silently dropped by parse_file_blocks — warn and retry once.
        if response and _stop == "max_tokens":
            print("[stage 2.9] ⚠️  response truncated (stop=max_tokens) — retrying once")
            try:
                response, _stop = call_anthropic_protocol(prompt, config, max_tokens=comp_tokens)
            except Exception as e:
                print(f"[stage 2.9] retry failed: {e} — keeping truncated response")
            if _stop == "max_tokens":
                print("[stage 2.9] ⚠️  still truncated — keeping complete blocks only")
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


def stage_2_9_append_source_backlinks(
    file_blocks: list[tuple[str, str]],
    comp_blocks: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """A7 (audit H5): comparisons were a zero-inlink island — 2.9 runs after
    the 2.6 source page has been generated, so no page ever linked to them.
    Append a `## Comparisons` section (prefixed wikilinks) to the source page
    block while it is still in memory. Returns a new list (no mutation);
    no-op without comp_blocks; warns when no source block is present."""
    if not comp_blocks:
        return list(file_blocks)
    links = []
    for path, content in comp_blocks:
        stem = Path(path).stem
        title = _stage_2_frontmatter_title(content) or stem
        links.append(f"- [[comparisons/{stem}]] — {title}")
    section = "\n\n## Comparisons\n\n" + "\n".join(links) + "\n"
    result: list[tuple[str, str]] = []
    appended = False
    for path, content in file_blocks:
        norm = path[len("wiki/"):] if path.startswith("wiki/") else path
        if not appended and norm.startswith("sources/"):
            content = content.rstrip("\n") + section
            appended = True
        result.append((path, content))
    if not appended:
        print("[stage 2.9] ⚠️  no source page block found — Comparisons backlink skipped")
    return result
