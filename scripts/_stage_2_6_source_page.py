
from _stage_2_base import *

def stage_2_6_source_page(
    global_digest: dict,
    file_path: Path,
    config: Config,
    template: str = "",
    current_domain: str = "general",
    verbose: bool = False,
) -> tuple[str, str]:
    """Stage 2.6: Dedicated source page generation (NashSU two-step).

    Separated from concept/entity generation so the LLM can focus entirely
    on producing a high-quality source page from the global digest.
    This matches NashSU ingest.ts which generates the source page first,
    then concept/entity pages in a separate pass.
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

    prompt = f"""# Role
You are writing a **source page** for a Karpathy-pattern wiki knowledge base.
This page will be the authoritative entry for a book in the wiki.
{template_section}
# Book Information (from Global Digest)
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

## Book Summary

2-4 sentences summarizing what this book covers, its approach, and who it's for.

## Table of Contents & Key Concepts

For EACH chapter in the outline, write one comprehensive line:
1. **Chapter Title:** list ALL key topics — aim for 5-15 items, comma-separated.

Example:
1. **DC-DC Converters:** buck, boost, buck-boost, CCM vs DCM, voltage-mode control, PWM, synchronous rectification.

## Key Takeaways

5-10 most important claims, formulas, design rules, or conclusions. Each ONE sentence.
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
