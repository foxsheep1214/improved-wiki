#!/usr/bin/env python3
"""
_enrich_wikilinks.py — Post-save wikilink enrichment (NashSU enrich-wikilinks.ts parity).

After a new page is saved, asks the LLM to suggest [[wikilinks]] for terms
in the body that match existing wiki pages. The LLM returns (term→target)
JSON; this module does the actual string replacement — the LLM never
rewrites page content.

Uses the direct HTTP API (``call_anthropic_direct``) unconditionally, even in
``--conversation`` mode: enrichment is high-volume / low-value per call, so
the conversation handoff (which made it ~8× slower) is bypassed. If no API
key is configured the call fails soft — the page is returned unchanged.

Usage:
    from _enrich_wikilinks import enrich_wikilinks
    enriched = enrich_wikilinks(content, existing_slugs, config)
"""

import json, re
from pathlib import Path
from _frontmatter import parse_frontmatter, write_frontmatter
from _llm_api import call_anthropic_direct


def enrich_wikilinks(
    content: str,
    existing_slugs: list[str],
    config,
    *,
    max_terms: int = 15,
) -> str:
    """Scan page body for terms matching existing wiki slugs, insert [[wikilinks]].

    Only replaces the FIRST occurrence of each term. Never touches frontmatter.
    """
    if not existing_slugs:
        return content

    fm, body = parse_frontmatter(content)
    if len(body) < 100:
        return content  # too short to benefit

    # Build slug→display_name map
    slug_map = {}
    for s in existing_slugs[:500]:
        parts = s.split("/")
        name = parts[-1] if parts else s
        # Convert slug to readable form
        readable = name.replace("-", " ").replace("_", " ")
        slug_map[readable.lower()] = name
        slug_map[name.lower()] = name

    # Build prompt
    body_sample = body[:3000]
    slugs_str = "\n".join(f"- [[{s}]]" for s in sorted(existing_slugs[:200]))

    prompt = f"""Scan the wiki page body below and identify up to {max_terms} terms
that SHOULD be wikilinks to existing wiki pages. Only suggest terms
with an EXACT slug match below. Output ONLY a JSON array:

[{{"term": "exact body text", "target": "slug"}}]

# Existing Wiki Pages ([[slug]])
{slugs_str}

# Page Body
{body_sample}"""

    try:
        response, _ = call_anthropic_direct(prompt, config, max_tokens=2048)
        text = response.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
        suggestions = json.loads(text)
    except Exception:
        return content

    if not isinstance(suggestions, list):
        return content

    # Apply replacements (first occurrence only, body-only)
    changed = False
    for s in suggestions[:max_terms]:
        term = s.get("term", "")
        target = s.get("target", "")
        if not term or not target:
            continue
        # Only replace if term exists in body and isn't already a wikilink
        escaped = re.escape(term)
        if re.search(rf'\[\[{escaped}\]\]|\[\[{escaped}\|', body):
            continue  # already linked
        if escaped in body:
            body = body.replace(escaped, f"[[{target}]]", 1)
            changed = True

    if changed:
        return write_frontmatter(fm, body)
    return content
