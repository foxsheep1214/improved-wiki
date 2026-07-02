from __future__ import annotations

from _stage_2_base import *
from _language import build_language_directive


def _normalize_source_frontmatter(
    response: str, authors_yaml: str, year_yaml: str, url_yaml: str, venue_yaml: str,
    related_fallback: list[str] | None = None,
) -> str:
    """Normalize the source-page FILE block's frontmatter when the agent's
    Stage 2.6 response ignored the pre-filled template, in two ways:

    1. Inject any missing NashSU-parity bibliographic fields
       (authors/year/url/venue) using the values already computed from the
       digest — root cause of the Strauss/Witte pages lacking them.
    2. Fill an empty ``related: []`` with up to five of this ingest's own
       generated concept/entity slugs, matching the 18 conforming source
       pages (the template asks for 2-5 related slugs, never empty).

    The pipeline writes the FILE block verbatim, so a dropped field or empty
    ``related`` would otherwise persist to disk. A well-formed, already-complete
    block is left untouched (no-op on parse failure or nothing to fill).
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

    # (2) Fill an empty related: [] with generated slugs (concepts first).
    if related_fallback:
        for i in range(fm_open + 1, fm_close):
            if lines[i].startswith("related:"):
                if lines[i].split(":", 1)[1].strip() in ("", "[]"):
                    picks = related_fallback[:5]
                    lines[i] = "related: [" + ", ".join(f'"{s}"' for s in picks) + "]"
                break

    # (1) Inject missing bibliographic fields before the frontmatter close.
    additions = [
        f"{key}: {val}"
        for key, val in (("authors", authors_yaml), ("year", year_yaml),
                         ("url", url_yaml), ("venue", venue_yaml))
        if key not in present
    ]
    if additions:
        lines[fm_close:fm_close] = additions

    return "\n".join(lines)


# ── A9 (audit 2026-07-02): Main-Arguments coverage validator ────────────────
# The Main Arguments section is the wiki's claim ledger; H2 showed front-prefix
# truncation historically produced ledgers covering only the opening chapters
# (何友 baseline: 13 claims for a 19+-chapter book). Non-fatal: warn loudly
# when the entry count falls below one per technical chapter of the outline.

_STAGE_2_6_NON_TECH_CHAPTER_RE = re.compile(
    r"前言|序言|序章|绪言|目录|致谢|附录|参考文献|索引|符号表|缩略语|"
    r"preface|foreword|contents|acknowledg|appendix|references|bibliography|"
    r"glossary|about the author",
    re.IGNORECASE,
)
_STAGE_2_6_MAIN_ARGS_HEADING_RE = re.compile(
    r"^#{2,3}\s*.*(?:Main Arguments|主要论[点断]|核心论[点断]|主要观点)",
    re.MULTILINE | re.IGNORECASE,
)
_STAGE_2_6_CLAIM_LABEL_RE = re.compile(
    r"\*\*\s*(?:Claim|论点|主张|断言)\s*(?:[:：]\s*\*\*|\*\*\s*[:：])",
    re.IGNORECASE,
)


def _stage_2_6_technical_chapter_count(outline) -> int:
    """Chapters in the 2.1 outline minus obvious non-technical front/back
    matter (前言/目录/附录/references …). Tolerates dict or plain-string
    outline entries; unparseable outlines count as 0 (validator stays quiet)."""
    if not isinstance(outline, list):
        return 0
    count = 0
    for ch in outline:
        title = ch.get("title", "") if isinstance(ch, dict) else str(ch)
        title = str(title).strip()
        if not title or _STAGE_2_6_NON_TECH_CHAPTER_RE.search(title):
            continue
        count += 1
    return count


def _stage_2_6_main_arguments_count(response: str) -> int:
    """Count claim entries in the response's Main Arguments section.

    Primary signal: `**Claim:**` / `**论点：**`-style labels (the template's
    per-claim shape). Fallback when the model skipped labels: top-level list
    items (column-0 `-`/`1.` lines — indented Evidence/Strength sub-bullets
    are excluded). Section ends at the next `## ` heading."""
    m = _STAGE_2_6_MAIN_ARGS_HEADING_RE.search(response)
    if not m:
        return 0
    section = response[m.end():]
    nxt = re.search(r"^##\s", section, re.MULTILINE)
    if nxt:
        section = section[:nxt.start()]
    labels = len(_STAGE_2_6_CLAIM_LABEL_RE.findall(section))
    if labels:
        return labels
    return len(re.findall(r"^(?:-|\*|\d+\.)\s+\S", section, re.MULTILINE))


def _stage_2_6_validate_main_arguments(response: str, outline) -> None:
    """Post-generation stage validator (A9): warn — never raise — when the
    claim ledger has fewer entries than technical chapters (coverage target:
    every technical chapter surfaces ≥1 claim, per the prompt's own rule)."""
    chapters = _stage_2_6_technical_chapter_count(outline)
    if chapters <= 0:
        return
    entries = _stage_2_6_main_arguments_count(response)
    if entries < chapters:
        print(f"  [stage 2.6][WARN] Main Arguments coverage LOW: {entries} "
              f"claim entr{'y' if entries == 1 else 'ies'} < {chapters} "
              f"technical chapter(s) — claim ledger may be under-sampled "
              f"(check chunk-claims injection / source front-truncation)")


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
) -> tuple[str, str]:
    """Stage 2.6: Dedicated source page generation.

    Separated from concept/entity generation so the LLM can focus entirely
    on producing a high-quality source page from the global digest.

    NOTE — divergence from NashSU (intentional): NashSU's ingest is a
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

## Key Entities

List **EVERY entity page from the "Generated pages" block above** (do NOT omit any), one bullet per entity, each with:
- **Name + type** — briefly, what kind of thing it is.
- **Role in this paper** — central vs. peripheral, one sentence.

Wikilink each entity to its slug.

## Main Arguments & Findings

The paper's core claims — the wiki's claim ledger for this source (feeds
overview Strong/Weak claims, contradiction review, query grounding). Include
**EVERY substantive claim** (falsifiable/actionable: quantitative result,
design rule, comparative verdict, limit, mechanism — not scope descriptions or
bare definitions), covering problem, method, main results and limits; YOU
judge substantiveness — no numeric quota, no padding. For EACH:
- **Claim:** the assertion (one sentence).
- **Evidence:** which figure / table / section supports it.
- **Strength:** high / medium / low.
- **Subject:** which entity or method the claim attaches to — do NOT transfer claims across subjects just because they share keywords.

## Connections to Existing Wiki

Which existing wiki pages does this source relate to? For each, does it **strengthen**, **challenge**, or **extend** existing knowledge? Wikilink each. If none, state "None identified."

## Contradictions & Tensions

Does anything in this source conflict with existing wiki content? Any internal tensions or caveats? If none, state "None identified."

## Recommendations

Which wiki pages should be created or updated based on this source? What should be emphasized vs. de-emphasized? Any open questions worth flagging for the user?"""
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

Then list **EVERY concept page from the "Generated pages" block above** (this ingest created a page for each — do NOT omit any), one bullet per concept, each with:
- **Name + brief definition** — the concept's definition as stated in the book.
- **Why it matters in this book** — one sentence.

Wikilink each concept to its slug.

## Key Entities

List **EVERY entity page from the "Generated pages" block above** (do NOT omit any), one bullet per entity, each with:
- **Name + type** — briefly, what kind of thing it is.
- **Role in this book** — central vs. peripheral, one sentence.

Wikilink each entity to its slug.

## Main Arguments & Findings

The book's core claims, results, or design rules — this section is the wiki's
claim ledger (it feeds overview Strong/Weak claims, contradiction review, and
query grounding), so it must be COMPLETE, not sampled:
- Include **EVERY substantive claim** from the full-book per-chunk claims list
  above, deduplicating near-identical claims repeated across chunk overlaps
  (keep the best-evidenced version).
- **Substantive** = a falsifiable or actionable assertion: a quantitative
  result, design rule, comparative verdict, limit, or mechanism explanation.
  NOT substantive: chapter-scope descriptions, bare definitions, restatements.
  YOU judge each claim against this test — no numeric quota either way.
- Coverage check: every technical chapter should surface ≥1 claim; if a
  chapter yields none, that must be because it genuinely asserts nothing
  (e.g. pure front matter), never because it was skipped.
For EACH:
- **Claim:** the assertion (one sentence).
- **Evidence:** which chapter / case / equation supports it.
- **Strength:** high / medium / low.
- **Subject:** which entity or concept the claim attaches to — do NOT transfer claims, limits, or evaluations from one subject to another just because they share keywords.

## Connections to Existing Wiki

Which existing wiki pages does this source relate to? For each, does it **strengthen**, **challenge**, or **extend** existing knowledge? Wikilink each. If none, state "None identified."

## Contradictions & Tensions

Does anything in this source conflict with existing wiki content? Any internal tensions or caveats? If none, state "None identified."

## Recommendations

Which wiki pages should be created or updated based on this source? What should be emphasized vs. de-emphasized? Any open questions worth flagging for the user?"""

    # Issue 2 fix: constrain source-page wikilinks to a known-linkable set so the
    # LLM cannot link to a concept's own (never-written) slug when that concept
    # was ALREADY COVERED by an existing page under a different slug. Without
    # this, the source page emitted [[concepts/system-concept]] etc. → broken
    # links, because the concept was skipped in Stage 2.4 and no such file exists.
    linkable = sorted(set(linkable_slugs or []))
    # 300 cut the sorted list mid-alphabet (observed live 2026-07-02: entities/*
    # never made it into a source-page prompt's Linkable list). 1500 covers the
    # current wiki scale; slugs are ~30 chars each so this stays <50K chars.
    if len(linkable) > 1500:
        linkable = linkable[:1500]
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

    # NashSU parity: Stage 2.3 association already answered "does this concept/
    # entity already exist in the wiki?" — feed those FACTS into the prompt so
    # the LLM wikilinks to the correct existing slug instead of guessing or
    # duplicating (NashSU feeds the same facts internally; it does not surface
    # them as a per-item output field).
    # associations = {name: [existing_slug, ...]} (only names that matched).
    assoc = associations or {}
    existing_lines: list[str] = []
    new_lines: list[str] = []
    for c in key_concepts:
        name = c.get("name", "").strip() if isinstance(c, dict) else str(c).strip()
        if not name:
            continue
        m = assoc.get(name)
        if m:
            existing_lines.append(f"- {name} → exists as [[{m[0]}]]")
        else:
            new_lines.append(f"- {name} (new)")
    for e in key_entities:
        name = e.get("name", "").strip() if isinstance(e, dict) else str(e).strip()
        if not name:
            continue
        m = assoc.get(name)
        if m:
            existing_lines.append(f"- {name} → exists as [[{m[0]}]]")
        else:
            new_lines.append(f"- {name} (new)")
    if existing_lines or new_lines:
        assoc_section = (
            "\n# Existing-wiki associations (Stage 2.3 FACTS — wikilink to the "
            "correct existing slug, do NOT guess or duplicate)\n"
            "Already exist in wiki (wikilink to the listed slug; do NOT create new):\n"
            + "\n".join(existing_lines or ["(none)"]) + "\n"
            "New (not yet in wiki — new pages created this ingest):\n"
            + "\n".join(new_lines or ["(none)"]) + "\n"
        )
    else:
        assoc_section = ""

    # Option A (NashSU single-tier): Key Concepts / Key Entities list EVERY
    # page generated this ingest (Stage 2.4 file_blocks), NOT the curated 2.1
    # key_concepts. Exists status comes from the 2.3 association facts above
    # (a slug is "exists (merged)" if 2.3 matched it to an existing page).
    _assoc_slugs: set[str] = set()
    for _slugs in (assoc or {}).values():
        _assoc_slugs.update(_slugs)
    def _exists_mark(slug: str) -> str:
        return "exists (merged)" if slug in _assoc_slugs else "new"
    _gen_c = generated_concepts or []
    _gen_e = generated_entities or []
    if _gen_c or _gen_e:
        _gp = ["# Generated pages (list EVERY one in Key Concepts / Key Entities — do NOT omit any)"]
        if _gen_c:
            _gp.append("Concept pages generated this ingest:")
            _gp.extend(f"- [[{s}]] ({_exists_mark(s)})" for s in _gen_c)
        if _gen_e:
            _gp.append("Entity pages generated this ingest:")
            _gp.extend(f"- [[{s}]] ({_exists_mark(s)})" for s in _gen_e)
        generated_pages_section = "\n".join(_gp) + "\n"
    else:
        generated_pages_section = ""

    language_sample = source_context or json.dumps(global_digest, ensure_ascii=False)
    language_directive = build_language_directive(language_sample)
    # Full-book claims from the per-chunk analyses (fix 2026-07-02): the 2.1
    # digest is built from a front-weighted sample, so its key_claims skew to
    # the opening chapters (observed live: a 9-chapter book's Main Arguments
    # covered only ch.1-2). The 2.2 chunk claims cover the whole book by
    # construction — feed them as the authoritative claim source.
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
            "\n# Claims from per-chunk analysis (FULL-BOOK coverage)\n"
            "Base **Main Arguments & Findings** primarily on THESE — they span every\n"
            "chapter, unlike the digest above (built from a front-weighted sample).\n"
            "Select the most important across ALL chapters; do not limit to the\n"
            "opening chapters.\n"
            + "\n".join(_cc_lines) + "\n"
        )

    prompt = f"""{language_directive}

# Role
You are writing a **source page** for a Karpathy-pattern wiki knowledge base.
This page will be the authoritative entry for a {source_kind} in the wiki.
{template_section}{linkable_rule}{source_section}{assoc_section}{generated_pages_section}
# {info_header}
```yaml
{digest_str}
```
{chunk_claims_section}

# Task
Write a comprehensive source page. Wrap it in FILE block format.

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

{body_sections}
---END FILE---

# Instructions
- Your FIRST line MUST be `---FILE:wiki/sources/{source_rel}.md---`, immediately followed by `---` (frontmatter start) on the NEXT line with NO blank line in between
- Your LAST line MUST be `---END FILE---`
- The frontmatter MUST use real data from the digest. NO ``` fences. NO blank lines before frontmatter.
- Do NOT add extra sections beyond those listed above. Link to concepts via [[wikilinks]].
- tags: 3-8 relevant tags (do NOT leave empty)
- related: 2-5 related wiki page slugs
- authors/year/url/venue: bibliographic fields for this source (NashSU source-page parity). The template is pre-filled from the digest where available — verify against the "{info_header}" block above and complete any left empty; use `[]` for authors and `""` for url/venue if genuinely unknown. authors is a list, year a number, url/venue strings.
- Math: $inline$ $$display$$
"""

    gen_tokens = config.compute_max_tokens(8192)
    response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=gen_tokens, label="source page")
    response = _normalize_source_frontmatter(
        response, authors_yaml, year_yaml, url_yaml, venue_yaml,
        related_fallback=(_gen_c + _gen_e),
    )
    _stage_2_6_validate_main_arguments(response, outline)
    if verbose:
        print(f"[stage 2.6] Source page generated ({len(response):,} chars, stop={stop_reason})")
    else:
        print(f"[stage 2.6] Source page ready ({len(response):,} chars)")

    return response, stop_reason


# ---------- Stage 2.7: Query generation ----------
