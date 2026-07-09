#!/usr/bin/env python3
"""
_enrich_wikilinks.py — Post-save wikilink enrichment (NashSU enrich-wikilinks.ts parity).

After all pages from an ingest are saved, asks the LLM to suggest [[wikilinks]]
for terms in each page's body that match existing wiki pages (or sibling pages
from the same ingest). The LLM returns (path -> [{term, target}, ...]) JSON;
this module does the actual string replacement — the LLM never rewrites page
content.

Round iv (2026-06-22): batched and conversation-mode only. One LLM call covers
every page written by an ingest, instead of one call per page — under
conversation mode that is one manual round-trip per ingest, not one per page.
Failures are no longer swallowed: a malformed/missing response raises and the
caller (ingest.py) does not catch it — enrichment failure now visibly fails
the ingest, same as any other stage.

Usage:
    from _enrich_wikilinks import enrich_wikilinks_batch
    enriched = enrich_wikilinks_batch(pages, existing_slugs, config)
    # enriched: {rel_path: new_content} for pages that changed
"""

import json, re
from pathlib import Path
from _frontmatter import parse_frontmatter, write_frontmatter
from _llm_api import call_anthropic_protocol


_LINK_SPAN_RE = re.compile(r'(\[\[.*?\]\])', re.DOTALL)


def _replace_first_outside_links(body: str, term: str, replacement: str):
    """Replace the first occurrence of `term` in `body` that is NOT inside an
    existing ``[[...]]`` wikilink span. Returns the new body, or None if every
    occurrence is inside a link (or there is no occurrence at all).

    Without this guard, a term that appears as a substring of an existing
    link's slug (e.g. ``lead`` inside ``[[concepts/lead-(pd)-...-design]]``)
    gets re-wrapped, producing malformed nested links such as
    ``[[concepts/[[lead-(pd)-...]]-(pd)-...]]``.
    """
    if term not in body:
        return None
    # With a capture group, re.split interleaves: [text, link, text, link, ...].
    # Odd indices are link spans and must never be touched.
    parts = _LINK_SPAN_RE.split(body)
    for i, seg in enumerate(parts):
        if i % 2 == 1:
            continue  # link span — leave intact
        if term in seg:
            parts[i] = seg.replace(term, replacement, 1)
            return "".join(parts)
    return None


def enrich_wikilinks_batch(
    pages: list[tuple[str, str]],
    existing_slugs: list[str],
    config,
    *,
    max_terms_per_page: int = 15,
) -> dict[str, str]:
    """Suggest and insert [[wikilinks]] across every page written in one ingest.

    ``pages`` is a list of (rel_path, content) for all just-written,
    non-listing pages. ``existing_slugs`` is the pre-ingest wiki snapshot;
    pages within this same batch are also valid link targets for each other
    (but never for themselves).

    Only replaces the FIRST occurrence of each suggested term per page.
    Never touches frontmatter. Returns {rel_path: enriched_content} for pages
    that actually changed — unchanged pages are omitted.
    """
    # Zero-outlink gate (redundancy fix 2026-07-09): Stage 2.4 generation
    # already mandates inline [[wikilinks]] in every page it writes, so
    # enriching pages that ALREADY carry outlinks was a no-op round-trip in
    # practice (the documented safe answer to the handoff was often `{}`).
    # Keep the NashSU-parity bailout only for pages with ZERO outgoing links
    # (merge leftovers, legacy pages); when no page qualifies, the whole LLM
    # round-trip is skipped.
    candidates = []
    for rel_path, content in pages:
        _, body = parse_frontmatter(content)
        if len(body) >= 100 and "[[" not in body:
            candidates.append((rel_path, content))
    skipped = len(pages) - len(candidates)
    if skipped:
        print(f"  [enrich] {skipped}/{len(pages)} page(s) already carry inline "
              f"[[wikilinks]] — enriching {len(candidates)} zero-outlink page(s)")
    if not candidates:
        return {}

    batch_slugs = [Path(rel_path).stem for rel_path, _ in candidates]
    # Exclude this batch's own slugs from the "existing" snapshot so the target
    # list is identical whether or not these pages are already on disk. On a
    # conversation-mode resume, list_existing_slugs rescans the wiki and now
    # includes the just-written pages; without this filter the [:200] window
    # shifts, changing the prompt hash and spuriously issuing a SECOND
    # enrichment handoff for the same ingest. batch_slugs are re-added below, so
    # they remain valid targets. (Also honors the documented "pre-ingest wiki
    # snapshot" intent of existing_slugs.)
    _batch_set = set(batch_slugs)
    existing_pre = [s for s in existing_slugs if s not in _batch_set]
    all_targets = list(dict.fromkeys(list(existing_pre[:200]) + batch_slugs))
    if not all_targets:
        return {}

    sections = []
    for rel_path, content in candidates:
        _, body = parse_frontmatter(content)
        sections.append(f"## PAGE: {rel_path}\n{body[:3000]}")

    slugs_str = "\n".join(f"- [[{s}]]" for s in sorted(all_targets))
    pages_str = "\n\n".join(sections)

    prompt = f"""For each PAGE below, identify up to {max_terms_per_page} terms in its body
that SHOULD be wikilinks to other pages — either existing wiki pages or other
pages in this same batch. Only suggest terms with an EXACT slug match below.
A page must never link to itself. Output ONLY a JSON object keyed by the
page's path, each value a list of {{"term": "exact body text", "target": "slug"}}.
Pages with no suggestions may be omitted from the object.

{{"path/to/page.md": [{{"term": "...", "target": "..."}}], ...}}

# Wiki Pages ([[slug]])
{slugs_str}

# Pages To Enrich
{pages_str}"""

    response, _ = call_anthropic_protocol(
        prompt, config, max_tokens=4096, label="wikilink enrichment (batch)")
    text = response.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    suggestions_by_path = json.loads(text)

    if not isinstance(suggestions_by_path, dict):
        raise ValueError(
            f"enrich_wikilinks_batch: expected a JSON object keyed by path, "
            f"got {type(suggestions_by_path).__name__}")

    enriched: dict[str, str] = {}
    for rel_path, content in candidates:
        suggestions = suggestions_by_path.get(rel_path, [])
        if not suggestions:
            continue
        fm, body = parse_frontmatter(content)
        this_slug = Path(rel_path).stem
        changed = False
        for s in suggestions[:max_terms_per_page]:
            term = s.get("term", "")
            target = s.get("target", "")
            if not term or not target or target == this_slug:
                continue
            escaped = re.escape(term)
            if re.search(rf'\[\[{escaped}\]\]|\[\[{escaped}\|', body):
                continue  # already linked
            # Replace only an occurrence NOT inside an existing [[...]] span,
            # otherwise we produce malformed nested links like
            # [[concepts/[[slug]]-suffix]] (bug found 2026-06-24).
            new_body = _replace_first_outside_links(body, term, f"[[{target}]]")
            if new_body is not None:
                body = new_body
                changed = True
        if changed:
            enriched[rel_path] = write_frontmatter(fm, body)

    return enriched
