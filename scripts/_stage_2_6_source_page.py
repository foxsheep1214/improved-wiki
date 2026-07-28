from __future__ import annotations

import json
import re
import time
from pathlib import Path

from _config import Config
from _core import canonical_source_path
from _file_block_repair import repair_truncated_file_blocks
from _llm_api import call_anthropic_protocol
from _language import build_language_directive
from _schema import load_purpose_md, load_schema_md, schema_prompt_text
from _stage_2_4_generation import _rank_linkable_fill


def _normalize_source_frontmatter(
    response: str, authors_yaml: str, year_yaml: str, url_yaml: str, venue_yaml: str,
) -> str:
    """Normalize the source-page FILE block's frontmatter when the agent's
    Stage 2.6 response ignored the pre-filled template:

    Inject any missing NashSU-parity bibliographic fields
       (authors/year/url/venue) using the values already computed from the
       digest — root cause of the Strauss/Witte pages lacking them.

    The pipeline writes the FILE block verbatim, so a dropped field or empty
    bibliographic field would otherwise persist to disk. A well-formed,
    already-complete block is left untouched (no-op on parse failure or nothing
    to fill). ``related: []`` is valid; NashSU does not impose a related-count
    quota.
    """
    lines = response.split("\n")
    # Locate the FILE block's frontmatter: the `---FILE:...---` line, then the
    # opening `---`, then the next standalone `---` closes the frontmatter.
    file_idx = next((i for i, ln in enumerate(lines) if ln.startswith("---FILE:")), None)
    if file_idx is None or file_idx + 1 >= len(lines) or lines[file_idx + 1].strip() != "---":
        return response
    fm_open = file_idx + 1
    fm_close = next((i for i in range(fm_open + 1, len(lines)) if lines[i].strip() == "---"), None)
    if fm_close is None:
        return response

    fm = lines[fm_open + 1:fm_close]
    present = {ln.split(":", 1)[0].strip() for ln in fm if ":" in ln}

    # Inject missing bibliographic fields before the frontmatter close.
    additions = [
        f"{key}: {val}"
        for key, val in (("authors", authors_yaml), ("year", year_yaml),
                         ("url", url_yaml), ("venue", venue_yaml))
        if key not in present
    ]
    if additions:
        lines[fm_close:fm_close] = additions

    return "\n".join(lines)


def _stage_2_6_validate_source_file_block(
    response: str,
    source_rel: str,
) -> None:
    """Require one well-formed, non-empty source FILE block at the exact path.

    This is the NashSU-aligned structural gate: it protects parser/write
    integrity without prescribing body headings or the number of concepts,
    entities, or claims in the summary.
    """
    header_pattern = r"^---\s*FILE:\s*(.*?)\s*---\s*$"
    header_matches = list(re.finditer(
        header_pattern,
        response,
        re.MULTILINE | re.IGNORECASE,
    ))
    headers = [match.group(1) for match in header_matches]
    expected = f"wiki/sources/{source_rel}.md"
    normalized = [path.strip() for path in headers]
    if normalized != [expected]:
        raise RuntimeError(
            "Stage 2.6 must emit exactly one source FILE block at "
            f"{expected}; got {normalized or 'none'}."
        )
    if len(re.findall(
            r"^---\s*END\s+FILE\s*---\s*$",
            response,
            re.MULTILINE | re.IGNORECASE,
    )) != 1:
        raise RuntimeError(
            "Stage 2.6 source FILE block must have exactly one END FILE marker."
        )

    start = header_matches[0].end()
    if start < len(response) and response[start] == "\n":
        start += 1
    content = response[start:]
    end_match = re.search(
        r"^---\s*END\s+FILE\s*---\s*$",
        content,
        re.MULTILINE | re.IGNORECASE,
    )
    if not end_match:
        raise RuntimeError("Stage 2.6 source FILE block is not closed.")
    file_content = content[:end_match.start()]
    lines = file_content.splitlines()
    if not lines or lines[0].strip() != "---":
        raise RuntimeError(
            "Stage 2.6 source FILE block must start with YAML frontmatter."
        )
    fm_close = next(
        (i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"),
        None,
    )
    if fm_close is None or not "\n".join(lines[fm_close + 1:]).strip():
        raise RuntimeError(
            "Stage 2.6 source FILE block must contain a non-empty body."
        )


def source_analysis_text(
    global_digest: dict,
    chunk_analyses: list[dict] | None = None,
    chunk_claims: list | None = None,
) -> str:
    """Serialize the complete Stage 2 analysis for deterministic recovery.

    NashSU's fallback source page preserves its full analysis rather than
    cutting it to a summary-sized prefix.  improved-wiki's equivalent analysis
    is the rolled-up digest plus every per-chunk analysis.  ``chunk_claims`` is
    retained as a compatibility fallback for older callers that do not carry
    the full chunk list.
    """
    payload: dict = {"global_digest": global_digest}
    if chunk_analyses is not None:
        payload["chunk_analyses"] = chunk_analyses
    elif chunk_claims is not None:
        payload["chunk_claims"] = chunk_claims
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def build_fallback_source_summary_content(
    source_identity: str,
    analysis_text: str,
    date: str,
) -> str:
    """Build NashSU's deterministic minimum source-summary page."""
    source_yaml = json.dumps(source_identity, ensure_ascii=False)
    title_yaml = json.dumps(
        f"Source: {source_identity}",
        ensure_ascii=False,
    )
    return "\n".join([
        "---",
        "type: source",
        f"title: {title_yaml}",
        f"created: {date}",
        f"updated: {date}",
        f"sources: [{source_yaml}]",
        "tags: []",
        "related: []",
        "---",
        "",
        f"# Source: {source_identity}",
        "",
        analysis_text or "(Analysis not available)",
        "",
    ])


def build_fallback_source_summary(
    source_rel: str,
    source_identity: str,
    analysis_text: str,
    date: str,
) -> str:
    """Wrap the deterministic source summary in one exact FILE block."""
    content = build_fallback_source_summary_content(
        source_identity,
        analysis_text,
        date,
    )
    return (
        f"---FILE:wiki/sources/{source_rel}.md---\n"
        f"{content.rstrip()}\n"
        "---END FILE---\n"
    )


def _serialize_file_blocks(blocks: list[tuple[str, str]]) -> str:
    return "\n\n".join(
        f"---FILE:{path if path.startswith('wiki/') else f'wiki/{path}'}---\n"
        f"{content.rstrip()}\n"
        "---END FILE---"
        for path, content in blocks
    )


def stage_2_6_source_page(
    global_digest: dict,
    file_path: Path,
    config: Config,
    template: str = "",
    verbose: bool = False,
    linkable_slugs: list[str] | None = None,
    source_context: str = "",
    associations: dict | None = None,
    generated_concepts: list[str] | None = None,
    generated_entities: list[str] | None = None,
    chunk_claims: list | None = None,
    chunk_analyses: list[dict] | None = None,
    generated_pages: list[str] | None = None,
) -> tuple[str, str]:
    """Stage 2.6: generate one NashSU-style source summary page.

    improved-wiki keeps this as a dedicated, resumable call, but its observable
    content contract matches NashSU: one free-form source summary, pages/links
    only for key items, and no fixed heading or entry-count quota.
    """
    try:
        source_rel = str(file_path.relative_to(config.raw_root).with_suffix(""))
    except ValueError:
        source_rel = file_path.stem

    book_meta = global_digest.get("book_meta", {})
    if not isinstance(book_meta, dict):
        book_meta = {}
    title = book_meta.get("title", file_path.stem) if isinstance(book_meta, dict) else file_path.stem
    # Bibliographic metadata for the source-page frontmatter (NashSU source-page
    # parity: authors/year/url/venue). Pull from whichever *_meta block the digest
    # carries — book_meta (books), paper_meta (papers; has venue/doi), part_meta /
    # clip_meta / deck_meta (datasheets/news/decks may carry url/venue).
    bib_meta = book_meta if book_meta else next(
        (v for k, v in global_digest.items()
         if k.endswith("_meta") and isinstance(v, dict)),
        {},
    )
    bib_authors = bib_meta.get("authors", []) if isinstance(bib_meta, dict) else []
    if not isinstance(bib_authors, list):
        bib_authors = [bib_authors] if bib_authors else []
    bib_year = bib_meta.get("year", "") if isinstance(bib_meta, dict) else ""
    bib_url = bib_meta.get("url", "") if isinstance(bib_meta, dict) else ""
    # NashSU has no `publisher` field; fold a book's publisher into `venue`.
    bib_venue = (bib_meta.get("venue", "") or bib_meta.get("publisher", "")) if isinstance(bib_meta, dict) else ""

    authors_yaml = "[" + ", ".join(f'"{a}"' for a in bib_authors) + "]" if bib_authors else "[]"
    year_yaml = str(bib_year) if bib_year not in ("", None) else '""'
    url_yaml = f'"{bib_url}"' if bib_url else '""'
    venue_yaml = f'"{bib_venue}"' if bib_venue else '""'

    digest_str = json.dumps(global_digest, ensure_ascii=False, indent=2)
    # 8000 silently cut the outline of large books (observed live 2026-07-02:
    # a 26-chapter handbook's source-page prompt lost chapters 24-26 and the
    # agent had to reconstruct them from the raw TOC). 24K chars is still lean.
    if len(digest_str) > 24000:
        digest_str = digest_str[:24000] + "\n... (truncated)"

    key_concepts = global_digest.get("key_concepts", [])
    key_entities = global_digest.get("key_entities", [])

    template_section = ""
    if template:
        template_section = f"\n# Document Type\n<template>\n{template[:2000]}\n</template>\n"

    # NashSU does not prescribe a source-page body template, heading set, or
    # entry count. Keep doctype only as a writing hint; the model chooses the
    # smallest useful structure for the source.
    is_paper = template.lstrip().startswith("# digest-paper")
    source_kind = "paper" if is_paper else "book"
    info_header = (
        "Paper Information (from Global Digest)"
        if is_paper else
        "Book Information (from Global Digest)"
    )
    body_guidance = """Write a concise, grounded source summary in the source's
language. Choose headings and structure that fit this source; no fixed H2 set is
required. Emphasize only what is genuinely important:

- the source's scope, approach, and intended audience;
- key named things and key ideas that materially shape the source;
- core arguments/findings and the evidence that supports them;
- meaningful connections, contradictions, caveats, or open questions.

Do not reproduce the analysis as an exhaustive inventory. Do not list every
generated page, every chapter topic, every entity mention, or every per-chunk
claim. Select and synthesize the core material, merge overlap, preserve exact
subject attribution, and retain specific evidence anchors when useful. There is
no heading-count, concept-count, or claim-count target. Link only the most
relevant existing/generated pages."""

    # Issue 2 fix: constrain source-page wikilinks to a known-linkable set so the
    # LLM cannot link to a concept's own (never-written) slug when that concept
    # was ALREADY COVERED by an existing page under a different slug. Without
    # this, the source page emitted [[concepts/system-concept]] etc. → broken
    # links, because the concept was skipped in Stage 2.4 and no such file exists.
    linkable = sorted(set(linkable_slugs or []))
    # 300 cut the sorted list mid-alphabet (observed live 2026-07-02: entities/*
    # never made it into a source-page prompt's Linkable list). 1500 covers the
    # current wiki scale; slugs are ~30 chars each so this stays <50K chars.
    # Above the cap, an ALPHABETICAL cut has the same disease at the tail: CJK
    # sorts last, so Chinese pages systematically vanish as the wiki grows
    # (observed live: 4 valid CJK slugs fell outside the cap). Rank by
    # relevance to THIS book instead (token/CJK-bigram overlap with the book's
    # own generated slugs + digest concept/entity names). Ranking is
    # deterministic (score desc, slug asc) so the prompt hash stays stable
    # within one ingest — the linkable snapshot is stable during a book's run.
    if len(linkable) > 1500:
        _ref_names = [
            str(x.get("name", "") if isinstance(x, dict) else x).strip()
            for x in list(key_concepts) + list(key_entities)
        ]
        _refs = (list(generated_pages or [])
                 + list(generated_concepts or []) + list(generated_entities or [])
                 + [n for n in _ref_names if n])
        linkable = sorted(_rank_linkable_fill(linkable, _refs)[:1500])
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

    # P1 parity with Stage 2.4 (2026-06-27): ground the source summary in
    # the raw source (trimmed to budget) so it uses the source's own wording,
    # formulas, numbers, and structure — not training memory.
    if source_context.strip():
        source_section = (
            "\n# Source Text (ground the summary in THIS — do not write from memory)\n"
            "Base the summary on what the source ACTUALLY says: use its own wording,\n"
            "formulas, numbers, and structure. Do not fabricate claims or topics the\n"
            "source does not contain.\n"
            "<source>\n"
            f"{source_context}\n"
            "</source>\n"
        )
    else:
        source_section = ""

    # Stage 2.3 association facts are link hints, not a source-page inventory.
    # Include only actual matches. Do not label every unmatched digest item as
    # "new" or imply that the source page must enumerate it.
    assoc = associations or {}
    existing_lines = [
        f"- {name} → exists as [[{slugs[0]}]]"
        for name, slugs in sorted(assoc.items())
        if slugs
    ]
    if existing_lines:
        assoc_section = (
            "\n# Existing-wiki association facts\n"
            "Use these exact targets only when they are materially relevant to "
            "the summary; do not enumerate them merely because they are listed:\n"
            + "\n".join(existing_lines) + "\n"
        )
    else:
        assoc_section = ""

    # Generated slugs remain available through the Linkable pages universe.
    # NashSU does not require the source summary to dump that universe into its
    # body, so there is intentionally no "Generated pages" checklist here.

    language_sample = source_context or json.dumps(global_digest, ensure_ascii=False)
    language_directive = build_language_directive(language_sample)
    schema_context = schema_prompt_text(load_schema_md(config))
    purpose_context = load_purpose_md(config).strip()[:6000]
    project_context_section = ""
    if schema_context:
        project_context_section += (
            "\n# Project Schema and Routing (AUTHORITATIVE)\n"
            "<schema>\n"
            f"{schema_context}\n"
            "</schema>\n"
            "The source page path and frontmatter must comply with this schema.\n"
        )
    if purpose_context:
        project_context_section += (
            "\n# Wiki Purpose\n"
            "<purpose>\n"
            f"{purpose_context}\n"
            "</purpose>\n"
            "Use this purpose to prioritize the summary.\n"
        )
    # Per-chunk claims are candidates, not a completeness ledger. NashSU passes
    # the consolidated analysis to generation and asks for core claims/results;
    # the source summary therefore selects and synthesizes only the important
    # claims while retaining source-wide context.
    chunk_claims_section = ""
    if chunk_claims:
        _cc_lines = []
        for c in chunk_claims[:400]:
            if isinstance(c, dict):
                _claim = c.get("claim", "")
                _ev = c.get("evidence", "")
                _conf = c.get("confidence", "")
                _cc_lines.append(f"- {_claim}" + (f" (evidence: {_ev})" if _ev else "") + (f" [{_conf}]" if _conf else ""))
            else:
                _cc_lines.append(f"- {c}")
        chunk_claims_section = (
            "\n# Claim candidates from per-chunk analysis\n"
            "Use these as source-wide context. Select only core arguments/findings,\n"
            "merge overlap, preserve exact subject attribution and evidence, and\n"
            "do not reproduce the list wholesale or pad to a count.\n"
            + "\n".join(_cc_lines) + "\n"
        )

    prompt = f"""{language_directive}

# Role
You are writing a **source page** for a Karpathy-pattern wiki knowledge base.
This page will be the authoritative entry for a {source_kind} in the wiki.
    {template_section}{project_context_section}{linkable_rule}{source_section}{assoc_section}
# {info_header}
```yaml
{digest_str}
```
{chunk_claims_section}

# Task
Write one concise, grounded source summary page. Wrap it in FILE block format.

{body_guidance}

# ⚠️  CRITICAL — OUTPUT FORMAT
Your ENTIRE response MUST be wrapped in EXACTLY ONE file block:

---FILE:wiki/sources/{source_rel}.md---
---
type: source
title: "{title}"
created: {time.strftime('%Y-%m-%d')}
updated: {time.strftime('%Y-%m-%d')}
tags: [tag1, tag2, tag3]
related: []
sources: ["raw/{source_rel}{file_path.suffix}"]
authors: {authors_yaml}
year: {year_yaml}
url: {url_yaml}
venue: {venue_yaml}
---

(source summary body; choose the useful structure described above)
---END FILE---

# Instructions
- Your FIRST line MUST be `---FILE:wiki/sources/{source_rel}.md---`, immediately followed by `---` (frontmatter start) on the NEXT line with NO blank line in between
- Your LAST line MUST be `---END FILE---`
- The frontmatter MUST use real data from the digest. NO ``` fences. NO blank lines before frontmatter.
- Choose only useful sections; no fixed heading set is required. Link only
  genuinely relevant concepts/entities via allowed [[wikilinks]].
- tags: relevant tags; do not pad to a target count
- related: relevant wiki page slugs; `[]` is valid when none are useful
- authors/year/url/venue: bibliographic fields for this source (NashSU source-page parity). The template is pre-filled from the digest where available — verify against the "{info_header}" block above and complete any left empty; use `[]` for authors and `""` for url/venue if genuinely unknown. authors is a list, year a number, url/venue strings.
- Evidence anchors: every claim's **Evidence** cites chapter/section/equation/figure numbers (式(5-10), 图2.6, Table 8.1); a value read off a figure's curve must be marked "据图X.X".
- Math: $inline$ $$display$$
"""

    gen_tokens = config.compute_max_tokens(8192)
    response, stop_reason = call_anthropic_protocol(
        prompt,
        config,
        max_tokens=gen_tokens,
        label="source page",
    )
    repair = repair_truncated_file_blocks(
        response,
        original_prompt=prompt,
        source_identity=canonical_source_path(file_path, config),
        config=config,
        max_tokens=config.compute_max_tokens(8192),
        label="stage 2.6",
        llm_call=call_anthropic_protocol,
    )
    candidate = _serialize_file_blocks(repair.blocks)
    candidate = _normalize_source_frontmatter(
        candidate, authors_yaml, year_yaml, url_yaml, venue_yaml,
    )
    fallback_reason = ""
    if repair.unrecovered_paths:
        fallback_reason = (
            "targeted repair did not recover "
            + ", ".join(repair.unrecovered_paths)
        )
    else:
        try:
            _stage_2_6_validate_source_file_block(candidate, source_rel)
        except RuntimeError as exc:
            fallback_reason = str(exc)

    if fallback_reason:
        analysis_text = source_analysis_text(
            global_digest,
            chunk_analyses=chunk_analyses,
            chunk_claims=chunk_claims,
        )
        candidate = build_fallback_source_summary(
            source_rel,
            canonical_source_path(file_path, config),
            analysis_text,
            time.strftime("%Y-%m-%d"),
        )
        _stage_2_6_validate_source_file_block(candidate, source_rel)
        stop_reason = "fallback-source-summary"
        print(
            "  [stage 2.6] Generated deterministic source-summary fallback "
            f"from complete Stage 2 analysis ({fallback_reason})"
        )

    response = candidate
    if verbose:
        print(f"[stage 2.6] Source page generated ({len(response):,} chars, stop={stop_reason})")
    else:
        print(f"[stage 2.6] Source page ready ({len(response):,} chars)")

    return response, stop_reason
