
from _stage_2_base import *

def stage_2_6_source_page(
    global_digest: dict,
    file_path: Path,
    config: Config,
    template: str = "",
    current_domain: str = "general",
    verbose: bool = False,
    linkable_slugs: list[str] | None = None,
    source_context: str = "",
) -> tuple[str, str]:
    """Stage 2.6: Dedicated source page generation.

    Separated from concept/entity generation so the LLM can focus entirely
    on producing a high-quality source page from the global digest.

    NOTE — divergence from NashSU 0.5.2 (intentional): NashSU's ingest is a
    two-step Analysis→Generation flow where the *Generation* step is a SINGLE
    combined LLM call that emits the source summary page AND all concept/entity
    FILE blocks together (ingest.ts ~L835-868, buildGenerationPrompt). improved-
    wiki instead splits the source page into this dedicated call for higher
    source-page quality. Granularity is aligned with NashSU: one source summary
    page per raw file at wiki/sources/<slug>.md, built from the accumulated
    digest; long sources are chunked at the analysis stage.
    """
    try:
        source_rel = str(file_path.relative_to(config.raw_root).with_suffix(""))
    except ValueError:
        source_rel = file_path.stem

    book_meta = global_digest.get("book_meta", {})
    if not isinstance(book_meta, dict):
        book_meta = {}
    title = book_meta.get("title", file_path.stem) if isinstance(book_meta, dict) else file_path.stem
    authors = book_meta.get("authors", []) if isinstance(book_meta, dict) else []
    year = book_meta.get("year", "") if isinstance(book_meta, dict) else ""
    publisher = book_meta.get("publisher", "") if isinstance(book_meta, dict) else ""

    digest_str = json.dumps(global_digest, ensure_ascii=False, indent=2)
    if len(digest_str) > 8000:
        digest_str = digest_str[:8000] + "\n... (truncated)"

    outline = global_digest.get("outline", [])
    key_claims = global_digest.get("key_claims", [])
    key_concepts = global_digest.get("key_concepts", [])
    key_entities = global_digest.get("key_entities", [])

    template_section = ""
    if template:
        template_section = f"\n# Document Type\n<template>\n{template[:2000]}\n</template>\n"

    # Source-page body shape is doctype-aware: papers are not books — they have
    # no chapter outline, so forcing "Table of Contents / EACH chapter" distorts
    # the structure and the "Book Summary" heading mislabels them. Branch on the
    # detected template; keep Key Takeaways + the dedicated call (better than
    # NashSU's free-form same-call source page) for all doctypes.
    is_paper = template.lstrip().startswith("# digest-paper")
    if is_paper:
        source_kind = "paper"
        info_header = "Paper Information (from Global Digest)"
        body_sections = """## Paper Summary

2-4 sentences: the problem the paper addresses, its approach, the main result, and who it's for.

## Methodology & Results

Write a focused technical summary from the digest. Cover:
- **Problem & motivation:** the gap it addresses.
- **Core idea / method:** the technical approach and key equations ($inline$, $$display$$).
- **Main results:** the principal findings, with numbers where available.
- **Comparison to prior work:** how it differs from or improves on prior methods.

Papers are not books — do NOT impose a chapter-by-chapter outline. Write flowing prose with [[wikilinks]] to concepts/entities.

## Key Takeaways

5-10 most important claims, formulas, design rules, or conclusions. Each ONE sentence."""
    else:
        source_kind = "book"
        info_header = "Book Information (from Global Digest)"
        body_sections = """## Book Summary

2-4 sentences summarizing what this book covers, its approach, and who it's for.

## Table of Contents & Key Concepts

For EACH chapter in the outline, write one comprehensive line:
1. **Chapter Title:** list ALL key topics — aim for 5-15 items, comma-separated.

Example:
1. **DC-DC Converters:** buck, boost, buck-boost, CCM vs DCM, voltage-mode control, PWM, synchronous rectification.

## Key Takeaways

5-10 most important claims, formulas, design rules, or conclusions. Each ONE sentence."""

    # Issue 2 fix: constrain source-page wikilinks to a known-linkable set so the
    # LLM cannot link to a concept's own (never-written) slug when that concept
    # was ALREADY COVERED by an existing page under a different slug. Without
    # this, the source page emitted [[concepts/system-concept]] etc. → broken
    # links, because the concept was skipped in Stage 2.4 and no such file exists.
    linkable = sorted(set(linkable_slugs or []))
    if len(linkable) > 300:
        linkable = linkable[:300]
    linkable_str = "\n".join(f"  - [[{s}]]" for s in linkable) if linkable else "(none — write concepts as plain text, do NOT invent [[wikilinks]])"
    linkable_rule = (
        "\n# Wikilink Rule — STRICT\n"
        "ONLY use [[wikilinks]] that appear in the Linkable pages list below. "
        "A concept marked ALREADY COVERED in Stage 2.4 was NOT written under its "
        "own slug — link to its EXISTING slug from the list, never to "
        "[[concepts/<its-own-name>]]. If a concept is not in the list, write it "
        "as PLAIN TEXT with no [[ ]].\n"
        f"# Linkable pages\n{linkable_str}\n"
    )

    # P1 parity with Stage 2.4/2.7/2.9 (2026-06-27): ground the summary/TOC/
    # takeaways in the raw source (trimmed to budget) so the page uses the source's
    # own wording, formulas, numbers, and chapter structure — not training memory.
    if source_context.strip():
        source_section = (
            "\n# Source Text (ground the summary in THIS — do not write from memory)\n"
            "Base the summary, TOC, and takeaways on what the source ACTUALLY says:\n"
            "use its own wording, formulas, numbers, and chapter structure. Do not\n"
            "fabricate takeaways or topics the source does not contain.\n"
            "<source>\n"
            f"{source_context}\n"
            "</source>\n"
        )
    else:
        source_section = ""

    prompt = f"""# Role
You are writing a **source page** for a Karpathy-pattern wiki knowledge base.
This page will be the authoritative entry for a {source_kind} in the wiki.
{template_section}{linkable_rule}{source_section}
# {info_header}
```yaml
{digest_str}
```

# Task
Write a comprehensive source page. Wrap it in FILE block format.

# ⚠️  CRITICAL — OUTPUT FORMAT
Your ENTIRE response MUST be wrapped in EXACTLY ONE file block:

---FILE:wiki/sources/{source_rel}.md---
---
type: source
title: "{title}"
domain: {current_domain}
created: {time.strftime('%Y-%m-%d')}
updated: {time.strftime('%Y-%m-%d')}
tags: [tag1, tag2, tag3]
related: []
sources: ["raw/{source_rel}{file_path.suffix}"]
---

{body_sections}
---END FILE---

# Instructions
- Your FIRST line MUST be `---FILE:wiki/sources/{source_rel}.md---`, immediately followed by `---` (frontmatter start) on the NEXT line with NO blank line in between
- Your LAST line MUST be `---END FILE---`
- The frontmatter MUST use real data from the digest. NO ``` fences. NO blank lines before frontmatter.
- Do NOT add extra sections beyond the 3 listed above. Link to concepts via [[wikilinks]].
- tags: 3-8 relevant tags (do NOT leave empty)
- related: 2-5 related wiki page slugs
- Math: $inline$ $$display$$
"""

    gen_tokens = config.compute_max_tokens(8192)
    response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=gen_tokens, label="source page")
    if verbose:
        print(f"[stage 2.6] Source page generated ({len(response):,} chars, stop={stop_reason})")
    else:
        print(f"[stage 2.6] Source page ready ({len(response):,} chars)")

    return response, stop_reason


# ---------- Stage 2.7: Query generation ----------
