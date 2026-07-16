from __future__ import annotations

from _stage_2_base import *
from _language import build_language_directive
from _frontmatter_array import parse_frontmatter_array
from _paths import iter_wiki_pages

# NashSU parity: the accumulating "already generated" context fed into each
# chunk's prompt is BOUNDED, mirroring NashSU's trimLongText(globalDigest). The
# full generated_slugs list still drives per-concept SKIP membership checks and
# the (independently capped) Linkable list, so dedup quality is unaffected — only
# the displayed "SKIP these" block is windowed to the most-recent N. Matches the
# per-concept fallback's existing generated_slugs[:50] cap.
GENERATED_DISPLAY_MAX = 50

# Soft cap on the displayed Linkable-pages list. Must-link targets (this chunk's
# slugs, prior-chunk pages, Stage 2.3 existing_refs, related pages) are always
# kept; only the background fill of other existing wiki pages is bounded by this.
_LINKABLE_TOTAL_CAP = 400


def _linkable_relevance_tokens(text: str) -> set:
    """Token set for linkable-fill relevance ranking: ASCII content words ∪ CJK
    character bigrams (reuses the Stage 2.3 tokenizers). Folder prefixes are
    dropped and -/_ split into words so a slug ("concepts/matched-filter") and
    a title ("Matched Filter") tokenize alike."""
    stem = text.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
    return _stage_2_title_words(stem) | _stage_2_title_cjk_bigrams(stem)


def _rank_linkable_fill(candidates: list[str], reference_texts: list[str]) -> list[str]:
    """Rank background-fill slugs by relevance to THIS book, best first.

    When the fill candidate set exceeds its cap, an ALPHABETICAL cut
    systematically drops late-sorting slugs — CJK sorts after ASCII, so Chinese
    pages vanish first as the wiki grows (observed live 2026-07-02 on the 2.6
    [:1500] cap; same disease as the fixed [:200]/[:300] caps). Instead, score
    each candidate by its best token/CJK-bigram Jaccard overlap against the
    book's own generated slugs/titles and keep the most relevant.

    Deterministic and cheap (pure token math, no LLM/network): order is
    (score desc, slug asc). Determinism matters for prompt-hash stability
    within one ingest — the existing_slugs snapshot is stable during a book's
    run, so the ranked prefix (and hence the conversation-handoff cache key)
    never thrashes between resumes.
    """
    ref_sets: list[frozenset] = []
    seen: set[frozenset] = set()
    for ref in reference_texts:
        toks = frozenset(_linkable_relevance_tokens(ref))
        if toks and toks not in seen:
            seen.add(toks)
            ref_sets.append(toks)

    def _score(slug: str) -> float:
        cand = _linkable_relevance_tokens(slug)
        if not cand or not ref_sets:
            return 0.0
        return max(len(cand & ref) / len(cand | ref) for ref in ref_sets)

    return sorted(candidates, key=lambda s: (-_score(s), s))

# ── Audit 2026-07-02 三/B prompt-text additions (injected into BOTH the
# per-chunk and single-shot generation prompts) ─────────────────────────────

# B1 (H4): the Stage 2.2 entity tie-breaker was never restated at generation
# time, so drifted candidates (named methods, multi-author strings, ISBNs)
# became entity pages with no downstream correction.
_ENTITY_RULES_SECTION = """
# Entity Rules (restated from Stage 2.2 — enforce at generation time)
- Tie-breaker: a named *model/method/technique* (Swerling model, matched filter,
  JPDA…) is a CONCEPT, not an entity — if mislisted above, emit it under concepts/.
- ONE page per entity: a multi-person candidate ("A, B and C") must be SPLIT into
  individual person pages — never one merged page.
- Bibliography entries, citation strings, and ISBNs are NOT entities — skip them.
"""

# B6 (M6/M11): 30-64% of concept pages had no ## structure at all.
_CONCEPT_SKELETON_SECTION = """
# Concept Page Skeleton (recommended — trim sections the source doesn't support)
Structure each concept page as `##` sections in the source language:
定义 (definition) → 原理/公式 (principle & formulas) → 要点 (key points) → 参见 (see also).
Short pages may merge or drop sections, but never emit one undifferentiated paragraph.
"""

# B5+B6 (M9/M6): appended to the numbered Rules list of both prompts.
# 9 = D1 slug-language ruling (2026-07-02): slug follows the SOURCE language;
# 10 = D4 figure-reference ruling (2026-07-02): cited figure numbers link to
#      the book's source page (needs the per-book source-page slug, hence a
#      builder function instead of a constant).
def _extra_rules(source_page_slug: str) -> str:
    return f"""7. related frontmatter — EXACT format: prefixed bare slugs, comma-separated,
   NO [[ ]] and NO .md — e.g. related: [concepts/matched-filter, entities/bell-labs].
8. Evidence anchors: formulas/data cite the source's chapter/section/equation/
   figure number (式(5-10), 图2.6, Table 8.1); a value read off a figure's curve
   must be marked "据图X.X".
9. slug uses the SOURCE language (中文书→中文slug, English book→English kebab);
   English terms belong in title, not slug, EXCEPT established acronyms
   (mti, cfar, dds) which may stay; never mixed 中英双拼 slugs.
10. When body text cites a figure number (图2.6 / Fig. 3-1), link it to the
    source page: [[{source_page_slug}|据图2.6]] — this source-page link is
    always valid even though it is not in the Linkable list. Never leave a
    bare figure number pointing nowhere; do NOT embed images."""


def _source_page_slug(file_path: Path, config: Config) -> str:
    """Wikilink stem of this book's source page: sources/<raw-rel-sans-ext>."""
    try:
        rel = file_path.relative_to(config.raw_root).with_suffix("")
    except ValueError:
        rel = Path(file_path.stem)
    return f"sources/{rel}"


def _top_wiki_tags(config: Config, top_n: int = 30) -> list[str]:
    """Most-used frontmatter tags across existing wiki pages (B3, audit M10).

    Injected into the generation prompts so the model can REUSE the wiki's tag
    vocabulary instead of inventing near-synonyms — singleton-tag rate ran
    69-80% because generation never saw a single existing tag. A live top-N
    list was chosen over a static "reuse tags" instruction because the model
    cannot reuse a vocabulary it never sees; the cost is one frontmatter scan
    per prompt build, the same order as the list_existing_slugs() rglob these
    builders already perform. Only tags used ≥2 times qualify (a singleton is
    not a vocabulary); "stub"/"lint" artifact tags are excluded. Deterministic
    ordering (count desc, then name) keeps the prompt cache-key stable.
    """
    counts: dict[str, int] = {}
    for _rel, content in iter_wiki_pages(config.wiki_dir):
        for tag in parse_frontmatter_array(content, "tags"):
            tag = tag.strip()
            if tag and tag not in ("stub", "lint"):
                counts[tag] = counts.get(tag, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [t for t, c in ranked[:top_n] if c >= 2]


def _tags_reuse_section(config: Config) -> str:
    """Prompt block listing the wiki's current top tags (B3, audit M10)."""
    tags = _top_wiki_tags(config)
    if not tags:
        return ""
    return (
        "\n# Tags — reuse before inventing\n"
        "Prefer frontmatter tags already used in this wiki (below); avoid inventing\n"
        "near-synonyms (e.g. do NOT add \"雷达数据处理\" when \"数据处理\" exists).\n"
        "Invent a new tag only when nothing below fits.\n"
        f"Top existing tags: {', '.join(tags)}\n"
    )


def _collect_formulas_block(analyses: list[dict], cap: int = 60) -> str:
    """Render the verbatim-LaTeX formulas Stage 2.2 transcribed as a grounding
    block for generation.

    Stage 2.2 captures each formula verbatim (single-quoted LaTeX) in a dedicated
    ``formulas:`` list, but generation otherwise only sees concept definitions +
    a budget-trimmed source excerpt. For any formula outside that excerpt the LLM
    would reconstruct it from memory — the main source of formula drift. Feeding
    the exact LaTeX back here keeps every formula anchored to the source.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for ca in analyses:
        if not isinstance(ca, dict):
            continue
        for f in ca.get("formulas", []) or []:
            if isinstance(f, dict):
                expr = str(f.get("formula", "")).strip()
                meaning = str(f.get("meaning", "")).strip()
            else:
                expr = str(f).strip()
                meaning = ""
            if not expr or expr in seen:
                continue
            seen.add(expr)
            lines.append(f"  - `{expr}`" + (f" — {meaning}" if meaning else ""))
            if len(lines) >= cap:
                break
        if len(lines) >= cap:
            break
    if not lines:
        return ""
    return (
        "\n# Formulas (transcribed verbatim from the source in Stage 2.2 — REUSE EXACTLY)\n"
        "When a page involves one of these, reproduce the LaTeX EXACTLY as written\n"
        "here — same variables, same form. Do NOT paraphrase a formula into prose and\n"
        "do NOT substitute a generic/popular textbook version. Inline as $...$, display\n"
        "as $$...$$.\n"
        + "\n".join(lines) + "\n"
    )


# Folders that may appear in schema.md but are not LLM-generated page types.
# Shared constant lives in _stage_2_base.SCHEMA_NON_PAGE_DIRS (NashSU
# schema-typed-candidates parity — used by Stage 2.2 analysis too).


def _schema_routing_block(config: Config) -> str:
    """NashSU schema-driven routing guidance.

    When schema.md declares typed folders beyond the base page types, tell the LLM
    it may route a page into the folder that fits best (a person → people/, a
    method → methods/) instead of forcing everything into concepts/entities.
    Empty when the schema adds no extra folders (so default projects see no noise).
    """
    text = load_schema_md(config)
    if not text.strip():
        return ""
    extra = schema_folders(text) - BASE_PAGE_DIRS - SCHEMA_NON_PAGE_DIRS
    if not extra:
        return ""
    return (
        "\n# Schema-Defined Folders (route pages here when they fit better) — NashSU schema parity\n"
        "This project's schema.md defines extra typed folders. When a page clearly\n"
        "belongs to one (e.g. a person → people/, a method → methods/, a decision →\n"
        "decisions/), emit it as `---FILE:wiki/<folder>/<slug>.md---` and wikilink it\n"
        "as `[[<folder>/<slug>]]`. Otherwise use concepts/ or entities/ as usual.\n"
        f"Schema folders available: {', '.join(sorted(extra))}\n"
        "<schema>\n"
        f"{text.strip()[:1500]}\n"
        "</schema>\n"
    )


def _stage_2_4_build_prompt(
    chunk_analysis: dict,
    chunk_text: str,
    chunk_index: int,
    file_path: Path,
    config: Config,
    template: str = "",
    generated_slugs: list[str] | None = None,
    existing_refs: dict | None = None,
    related_pages: list[dict] | None = None,
) -> str:
    """Build prompt to generate concept/entity pages from ONE chunk's analysis.

    Accepts generated_slugs from previously-processed chunks so the LLM can:
      - Skip concepts already covered by earlier chunks
      - Use [[wikilinks]] to reference existing pages
      - Avoid duplicate slug generation
    (NashSU parity: sequential, accumulating context.)
    """
    concepts = chunk_analysis.get("concepts_found", [])
    entities = chunk_analysis.get("entities_found", [])
    existing_slugs = list_existing_slugs(config)
    if generated_slugs is None:
        generated_slugs = []
    existing_refs = existing_refs or {}

    concept_lines = []
    concept_slugs: list[tuple[str, str]] = []  # (name, slug) for wikilink reference
    concept_slug_stems: set[str] = set()  # for entity-dedup (Issue 4)
    for c in concepts:
        if isinstance(c, dict):
            name = c.get("name", "")
            imp = c.get("importance", "core")
            defn = c.get("definition", "")
            details = c.get("key_details", [])
            slug = slugify(name)
            # SKIP when generated by a prior chunk OR when Stage 2.3 found an
            # existing-page overlap. Skipping the overlap concept here keeps its
            # NEW slug out of the linkable list so the LLM links to the EXISTING
            # page instead of a never-generated new slug (broken wikilink).
            if name in existing_refs and existing_refs[name]:
                # Issue 2 fix: show the EXISTING slug as the canonical wikilink
                # target so the LLM links there instead of the never-written
                # concepts/{own-slug} (which would be a broken link).
                existing_slug = existing_refs[name][0]
                concept_lines.append(
                    f"  - {name} → ALREADY COVERED by [[{existing_slug}]]: "
                    f"do NOT generate a page; wikilink ONLY as [[{existing_slug}]] "
                    f"(never [[concepts/{slug}]])"
                )
            elif slug in generated_slugs:
                concept_lines.append(
                    f"  - {name} (slug: concepts/{slug}) [{imp}]: {defn} [ALREADY COVERED — SKIP]"
                )
            else:
                concept_lines.append(
                    f"  - {name} (slug: concepts/{slug}) [{imp}]: {defn}"
                )
                concept_slugs.append((name, f"concepts/{slug}"))
                concept_slug_stems.add(slug)
                for d in details[:3]:
                    concept_lines.append(f"      • {d}")

    # Issue 4: collect prior-chunk concept slug stems too, so an entity sharing
    # a concept's slug is deduped (concept page takes precedence over entity).
    for s in generated_slugs:
        if s.startswith("concepts/"):
            concept_slug_stems.add(s.split("/", 1)[1])

    entity_lines = []
    entity_slugs: list[tuple[str, str]] = []  # (name, slug) for wikilink reference
    for e in entities:
        if isinstance(e, dict):
            name = e.get("name", "")
            sig = e.get("significance", "")
            slug = slugify(name)
            if name in existing_refs and existing_refs[name]:
                existing_slug = existing_refs[name][0]
                entity_lines.append(
                    f"  - {name} → ALREADY COVERED by [[{existing_slug}]]: "
                    f"do NOT generate; wikilink ONLY as [[{existing_slug}]]"
                )
            elif slug in generated_slugs or slug in concept_slug_stems:
                # Issue 4: a concept page for this slug already exists (this chunk
                # or a prior one) — skip the duplicate entity page; wikilink to
                # the concept page instead.
                entity_lines.append(
                    f"  - {name} (slug: entities/{slug}): {sig} "
                    f"[DUPLICATE OF CONCEPT concepts/{slug} — SKIP]"
                )
            else:
                entity_lines.append(
                    f"  - {name} (slug: entities/{slug}): {sig}"
                )
                entity_slugs.append((name, f"entities/{slug}"))

    # NOTE: these caps are LINE counts, but each concept emits a header line PLUS
    # up to 3 key_detail bullets (~4 lines/concept). A low cap therefore silently
    # truncates the TAIL concepts of a dense chunk from the GENERATE list while
    # they remain in the linkable list (concept_slugs, uncapped) → broken links to
    # never-generated pages. The Stage 2.2 density guideline targets up to ~40
    # concepts/chunk (≈160 lines), so the cap must sit well above that; ingest is
    # not token-sensitive and the chunk text already dominates the prompt. Bumped
    # 100→480 / 30→160 (2026-06-30) so realistic high-density chunks are never cut.
    concept_str = "\n".join(concept_lines[:480]) if concept_lines else "(none)"
    entity_str = "\n".join(entity_lines[:160]) if entity_lines else "(none)"

    # NashSU parity: schema-typed candidates pre-identified by Stage 2.2.
    # Surface them explicitly so generation routes a page into the candidate's
    # folder instead of re-deriving the type from concepts/entities. Skip any
    # whose slug is already covered (existing/prior-chunk) — wikilink only.
    schema_candidate_slugs: list[tuple[str, str]] = []  # (name, folder/slug)
    schema_candidate_lines: list[str] = []
    for cand in chunk_analysis.get("schema_typed_candidates", []) or []:
        if not isinstance(cand, dict):
            continue
        name = str(cand.get("name", "")).strip()
        folder = str(cand.get("folder", "")).strip()
        if not name or not folder:
            continue
        slug = slugify(name)
        full_slug = f"{folder}/{slug}"
        if name in existing_refs and existing_refs[name]:
            schema_candidate_lines.append(
                f"  - {name} → ALREADY COVERED by [[{existing_refs[name][0]}]]: "
                f"do NOT generate; wikilink ONLY as [[{existing_refs[name][0]}]]"
            )
        elif full_slug in generated_slugs:
            schema_candidate_lines.append(f"  - {name} (slug: {full_slug}) [ALREADY COVERED — SKIP]")
        else:
            cand_type = str(cand.get("type") or folder)
            cand_rationale = str(cand.get("rationale", ""))
            schema_candidate_lines.append(
                f"  - {name} (slug: {full_slug}) [{cand_type}]: {cand_rationale}"
            )
            schema_candidate_slugs.append((name, full_slug))
    schema_candidates_str = (
        "\n".join(schema_candidate_lines[:40]) if schema_candidate_lines else "(none)"
    )

    # Display only the most-recent window (NashSU-bounded); the full list is still
    # used for SKIP membership (above) and the Linkable list (below), so older
    # pages remain linkable and are never regenerated.
    if not generated_slugs:
        generated_str = "(none yet — you are the first chunk)"
    else:
        shown = generated_slugs[-GENERATED_DISPLAY_MAX:]
        generated_lines = [f"  - {s}" for s in shown]
        omitted = len(generated_slugs) - len(shown)
        if omitted > 0:
            generated_lines.insert(
                0,
                f"  (… {omitted} earlier page(s) omitted — they remain in the "
                f"Linkable pages list below and must NOT be regenerated)",
            )
        generated_str = "\n".join(generated_lines)

    # Build the linkable-slugs list in two tiers. MUST-LINK targets are slugs the
    # prompt EXPLICITLY instructs the LLM to wikilink to — this chunk's own
    # concepts/entities, prior-chunk pages, Stage 2.3 existing_refs (ALREADY
    # COVERED targets), and related pages. These must NEVER be dropped: the old
    # code merged everything into one set, sorted, then truncated to 300, so an
    # ALREADY-COVERED target that sorted late vanished from the list while the
    # ALREADY-COVERED instruction still referenced it (book-2 re-ingest bug).
    # The background FILL (other existing wiki pages) is what the cap bounds.
    must_link = set()
    for _, s in concept_slugs:
        must_link.add(s)
    for _, s in entity_slugs:
        must_link.add(s)
    for _, s in schema_candidate_slugs:
        must_link.add(s)
    for s in generated_slugs:
        if "/" in s:
            must_link.add(s)
        else:
            must_link.add(f"concepts/{s}")
            must_link.add(f"entities/{s}")
    # existing_refs values are bare stems for EXISTING pages the LLM links to
    # instead of regenerating.
    for slugs in existing_refs.values():
        for s in slugs:
            must_link.add(s)
    for rp in (related_pages or []):
        slug = rp.get("slug") if isinstance(rp, dict) else None
        if slug:
            must_link.add(slug)
    # Background fill: other existing wiki pages, bounded so the prompt stays a
    # reasonable size. Never displaces a must-link target. When candidates
    # exceed the room, keep the most RELEVANT to this book (token/CJK-bigram
    # overlap with this chunk's names + prior generated slugs) instead of an
    # alphabetical prefix, which systematically dropped late-sorting (CJK)
    # slugs — see _rank_linkable_fill (deterministic, prompt-hash stable).
    fill = sorted(s for s in set(existing_slugs) if s not in must_link)
    room = max(0, _LINKABLE_TOTAL_CAP - len(must_link))
    if len(fill) > room:
        refs = ([n for n, _s in concept_slugs] + [n for n, _s in entity_slugs]
                + [n for n, _s in schema_candidate_slugs] + list(generated_slugs))
        fill = sorted(_rank_linkable_fill(fill, refs)[:room])
    linkable_list = sorted(must_link) + fill
    linkable_str = "\n".join(f"  - {s}" for s in linkable_list) if linkable_list else "(none)"

    template_section = ""
    if template:
        template_section = f"\n# Document Type\n<template>\n{template[:1500]}\n</template>\n"

    # Stage 2.3 feed-back: concepts in this source that already exist in the wiki.
    # The LLM should wikilink to these instead of regenerating them.
    if existing_refs:
        ref_lines = []
        # Sort for deterministic prompt text → stable conversation-handoff cache
        # key. Without sorting, set/dict iteration order (Python hash randomization)
        # varies across runs, changing the prompt hash and re-prompting Stage 2.4
        # forever (cache never hits on resume).
        for name, slugs in sorted(existing_refs.items()):
            links = ", ".join("[[{}]]".format(s) for s in slugs)
            ref_lines.append("  - {} → already exists as: {}".format(name, links))
        existing_refs_str = "\n".join(ref_lines)
    else:
        existing_refs_str = "(none — this source has no overlap with existing wiki)"

    # Stage 2.2's self-reported connections_to_existing_wiki, resolved against
    # real pages by Stage 2.3 (stage_2_3_resolve_proposed_connections). These
    # are RELATED pages, not duplicates of the new concepts — still generate
    # full new pages, just wikilink to these where relevant in the body.
    if related_pages:
        rel_lines = [
            "  - [[{}]] (relationship: {})".format(rp["slug"], rp.get("relationship", "related"))
            for rp in related_pages
        ]
        related_pages_str = "\n".join(rel_lines)
    else:
        related_pages_str = "(none)"

    # P1 (2026-06-27): ground every page in THIS chunk's raw source text. This is
    # what gives full-concept fidelity for books of ANY size — each chunk's
    # concepts are generated with their exact source passage present, so the model
    # uses the source's own formulas/notation/examples, not training-memory.
    if chunk_text.strip():
        chunk_source_section = (
            "# Source Text for THIS chunk (GROUND EVERY PAGE IN THIS — do not write from memory)\n"
            "Use the source's OWN definitions, formulas, notation, variable names, and\n"
            "worked examples — never substitute a generic/popular version from memory.\n"
            "<source>\n"
            f"{chunk_text}\n"
            "</source>\n\n"
        )
    else:
        chunk_source_section = ""

    formulas_section = _collect_formulas_block([chunk_analysis])
    schema_section = _schema_routing_block(config)
    tags_section = _tags_reuse_section(config)
    extra_rules = _extra_rules(_source_page_slug(file_path, config))
    raw_rel = canonical_source_path(file_path, config)

    language_directive = build_language_directive(chunk_text)
    return f"""{language_directive}

# Role
You are generating wiki pages for ONE chunk of a book. Previous chunks have
already been processed — their pages are listed below. Do NOT regenerate them.

# Source
Book: {file_path.stem}
Chunk: {chunk_index + 1}

{template_section}
{chunk_source_section}{formulas_section}{schema_section}# Pages already generated by previous chunks (SKIP these):
{generated_str}

# Existing wiki pages that overlap (Stage 2.3 — DO NOT regenerate; wikilink to them):
{existing_refs_str}

# Related (not duplicate) existing pages — wikilink to these where relevant, but still generate full new pages for the concepts/entities below:
{related_pages_str}

# Concepts found in this chunk (generate a page for each — skip ALREADY COVERED):
{concept_str}

# Entities found in this chunk (generate a page for key ones — skip ALREADY COVERED):
{entity_str}
{_ENTITY_RULES_SECTION}
# Schema-typed pages found in this chunk (NashSU parity — generate at wiki/<folder>/<slug>.md when NOT already covered; skip ALREADY COVERED):
{schema_candidates_str}

# Supplementary foundational pages (use sparingly)
If THIS chunk clearly defines or depends on a foundational concept that is NOT in
the lists above AND is NOT in the Linkable pages list below (i.e. no existing page
covers it), you MAY generate a page for it at a new `wiki/concepts/<kebab-slug>.md`
path. Only do this for genuinely page-worthy building blocks the source actually
explains — never for passing mentions. Give it a short, specific kebab-case slug
and a `type: concept` frontmatter like the others. Do NOT [[wikilink]] to any slug
that is not either in the Linkable list or a page you generate in THIS response.

# ⚠️ CRITICAL — START IMMEDIATELY WITH FILE BLOCKS
- Your FIRST line of output MUST be `---FILE:wiki/concepts/...`
- Do NOT write any preamble, introduction, or commentary. IGNORED by parser.

# [[wikilink]] Rules — STRICT
Each concept/entity above includes a slug like (slug: concepts/foo-bar).
This is the EXACT [[wikilink]] you must use — kebab-case with type prefix.

Correct format:
  [[concepts/natural-convection-heat-sink]]  ← kebab-case + concepts/ prefix
  [[entities/bell-labs]]                      ← kebab-case + entities/ prefix

WRONG formats (DO NOT use — these create broken links):
  [[Natural Convection Heat Sink]]  ← Title Case, no prefix = BROKEN
  [[convection]]                    ← missing prefix = BROKEN
  [[concepts/litz-wire.md]]         ← includes .md = BROKEN
  [[cooling technique]]             ← not in linkable list = BROKEN

# Linkable pages (ONLY these [[wikilinks]] are valid):
{linkable_str}

Rules:
1. ONLY use [[wikilinks]] from the "Linkable pages" list above.
2. Use the EXACT slug shown. Do not change case, add words, or invent new ones.
3. For concepts/entities IN THIS CHUNK: use the slug from its "(slug: ...)" label.
4. If no matching slug exists, write the term as PLAIN TEXT with NO [[]].
5. NEVER use `/` in filenames (macOS rejects it). Use "-" instead.
6. Math: ALWAYS write formulas in LaTeX — inline $...$, display $$...$$. Transcribe
   each formula from the source / Formulas list verbatim (same variables, same form);
   never paraphrase a formula into prose or swap in a generic textbook version.
{extra_rules}
{_CONCEPT_SKELETON_SECTION}{tags_section}
# Output Format — EXACT
---FILE:wiki/concepts/<slug>.md---
---
type: concept
title: "..."
tags: [...]
related: [...]
sources: ["{raw_rel}"]
created: {time.strftime('%Y-%m-%d')}
updated: {time.strftime('%Y-%m-%d')}
---

# Title

(content)

---END FILE---
---FILE:wiki/entities/<slug>.md---
---
type: entity
title: "<entity name>"
tags: [...]
related: [...]
sources: ["{raw_rel}"]
created: {time.strftime('%Y-%m-%d')}
updated: {time.strftime('%Y-%m-%d')}
---

# <entity name>

(content)

---END FILE---

Generate a page for EVERY concept listed above that is NOT marked [ALREADY COVERED]. Go!
"""


# ── Per-concept fallback (when per-chunk returns 0 blocks) ──

# Maximum concepts per LLM call in fallback mode.
# Above this, concepts are split into multiple calls.
PER_CONCEPT_BATCH_MAX = 4


def _stage_2_4_per_concept_fallback(
    chunk_analyses: list[dict],
    global_digest: dict,
    file_path: Path,
    config: Config,
    template: str = "",
    verbose: bool = False,
    pre_existing_slugs: list[str] | None = None,
) -> tuple[dict, str, list[tuple[str, str]]]:
    """Generate each concept in its own small LLM call.

    Used when per-chunk generation returns 0 FILE blocks (e.g. chunk has
    too many concepts for a single call to complete within max_tokens/timeout).
    Each concept gets a focused prompt with just its definition + context from
    the chunk that found it.

    pre_existing_slugs: slugs already generated by prior per-chunk calls.
    When provided (barrier-free fallback), these concepts are skipped so the
    fallback only generates what was missed — avoids duplicate LLM calls.
    """
    unique_concepts, unique_entities = _stage_2_4_extract_names(chunk_analyses)
    all_file_blocks: list[tuple[str, str]] = []
    all_responses: list[str] = []
    generated_slugs: list[str] = list(pre_existing_slugs) if pre_existing_slugs else []
    gen_tokens = config.compute_max_tokens(8192)
    t0 = time.time()
    total = len(unique_concepts) + len(unique_entities)

    # Build concept→chunk_analysis map for targeted context
    concept_to_chunk: dict[str, int] = {}
    for idx, analysis in enumerate(chunk_analyses):
        for c in analysis.get("concepts_found", []):
            name = c.get("name", c) if isinstance(c, dict) else str(c)
            concept_to_chunk[name] = idx

    print(f"[stage 2.4] Per-concept fallback: {len(unique_concepts)} concepts + "
          f"{len(unique_entities)} entities, {PER_CONCEPT_BATCH_MAX} per batch, "
          f"max_tokens={gen_tokens}")

    n = 0
    existing_slugs = list_existing_slugs(config)

    def _generate_one(kind: str, name: str, prompt: str) -> None:
        """Generate ONE fallback page (concept or entity).

        Single-item failure is tolerated by design (print ❌; the coverage
        stats record the gap) — the fallback IS the remedial layer, so a
        failed item must not kill the backfill of the remaining ones.
        """
        nonlocal n
        t_call = time.time()
        try:
            response, stop_reason = call_with_retry(
                lambda: call_anthropic_protocol(prompt, config, max_tokens=gen_tokens),
                max_retries=3, base_wait=2.0, label=f"fallback {kind}")
        except Exception as e:
            print(f"  [{kind} {n+1}/{total}] ❌ {e}")
            return
        all_responses.append(response)
        blocks = parse_file_blocks(response)
        all_file_blocks.extend(blocks)
        n += 1
        pct = n * 100 // total
        dt = time.time() - t_call
        print(f"  [{kind} {n}/{total}] {name[:50]} → "
              f"{len(blocks)} blocks ({len(response):,} chars, {stop_reason}) "
              f"{dt:.0f}s [{pct}%]")
        for path, _content in blocks:
            s = file_block_slug(path)
            if s not in generated_slugs:
                generated_slugs.append(s)

    for concept_name in unique_concepts:
        chunk_idx = concept_to_chunk.get(concept_name, 0)
        analysis = chunk_analyses[chunk_idx] if chunk_idx < len(chunk_analyses) else chunk_analyses[0]

        # Extract concept details from the chunk analysis
        concept_info = None
        for c in analysis.get("concepts_found", []):
            name = c.get("name", c) if isinstance(c, dict) else str(c)
            if name == concept_name:
                concept_info = c if isinstance(c, dict) else {"name": c}
                break

        slug = slugify(concept_name)
        if slug in generated_slugs:
            continue

        prompt = _stage_2_4_build_per_concept_prompt(
            concept_info, slug, file_path, config, global_digest,
            analysis, generated_slugs, existing_slugs, template,
        )
        _generate_one("concept", concept_name, prompt)

    for entity_name in unique_entities[:min(len(unique_entities), 20)]:
        slug = slugify(entity_name)
        if slug in generated_slugs:
            continue
        prompt = _stage_2_4_build_per_entity_prompt(
            entity_name, slug, file_path, config, global_digest,
            existing_slugs, template,
        )
        _generate_one("entity", entity_name, prompt)

    # NOTE: no source-page generation here (removed 2026-07-12). The caller
    # (_generate_from_analyses) filters "sources/" blocks out of the fallback
    # output, and Stage 2.6 generates the real source page — the block this
    # segment produced was always discarded (one wasted LLM call per fallback).

    combined = "\n".join(all_responses)
    concept_blocks = [b for b in all_file_blocks if "concepts/" in b[0]]
    entity_blocks = [b for b in all_file_blocks if "entities/" in b[0]]

    print(f"[stage 2.4] Per-concept fallback done — {time.time()-t0:.0f}s, "
          f"{len(all_file_blocks)} blocks ({len(concept_blocks)}c/{len(entity_blocks)}e)")

    analysis = {
        "book_meta": global_digest.get("book_meta", {}),
        "outline": global_digest.get("outline", []),
        "concepts_identified": len(unique_concepts),
        "concepts_generated": len(concept_blocks),
        "entities_generated": len(entity_blocks),
        "coverage_pct": round(len(concept_blocks) / max(len(unique_concepts), 1), 2),
        "total_chunks": len(chunk_analyses),
        "method": "per-concept-fallback",
    }
    return analysis, combined, all_file_blocks


def _stage_2_4_build_per_concept_prompt(
    concept_info: dict,
    slug: str,
    file_path: Path,
    config: Config,
    global_digest: dict,
    chunk_analysis: dict,
    generated_slugs: list[str],
    existing_slugs: list[str],
    template: str = "",
) -> str:
    """Build a focused prompt for generating ONE concept page."""
    name = concept_info.get("name", slug)
    definition = concept_info.get("definition", "")
    importance = concept_info.get("importance", "core")
    details = concept_info.get("key_details", [])[:5]

    raw_rel = canonical_source_path(file_path, config)

    # Sibling concepts from same chunk (for wikilinks)
    siblings = []
    for c in chunk_analysis.get("concepts_found", []):
        cn = c.get("name", c) if isinstance(c, dict) else str(c)
        if cn != name:
            siblings.append(cn)

    template_section = ""
    if template:
        template_section = f"\n# Document Type\n<template>\n{template[:800]}\n</template>\n"

    language_directive = build_language_directive(
        " ".join([str(name), str(definition), *map(str, details)]))
    return f"""{language_directive}

# Role
Generate ONE wiki concept page. Output ONLY this one page, then stop.

{template_section}
# Concept to Generate
- Name: {name}
- Importance: {importance}
- Definition: {definition}
{f"- Key Details: " + "; ".join(details) if details else ""}

# Context from Source
Source: {file_path.stem}
{f"Sibling concepts in this section (use [[wikilinks]]): {', '.join(siblings[:10])}" if siblings else ""}

# Already Generated (skip these — use [[wikilinks]]):
{', '.join(generated_slugs[:50]) or "(none yet)"}

# Existing Wiki Pages (avoid duplicates):
{', '.join(existing_slugs[:50]) or "(none)"}

# ⚠️ CRITICAL — START IMMEDIATELY WITH FILE BLOCK
Your FIRST line MUST be `---FILE:wiki/concepts/{slug}.md---`
Do NOT write preamble, analysis, or commentary. Parser IGNORES non-FILE text.

# Output Format — EXACT
---FILE:wiki/concepts/{slug}.md---
---
type: concept
title: "{name}"
tags: [...]
related: []
sources: ["{raw_rel}"]
created: {time.strftime('%Y-%m-%d')}
updated: {time.strftime('%Y-%m-%d')}
---

# {name}

(Detailed content — explain the concept, include key details, use [[wikilinks]])

---END FILE---

Generate the page NOW. Start with ---FILE:...
"""


def _stage_2_4_build_per_entity_prompt(
    entity_name: str,
    slug: str,
    file_path: Path,
    config: Config,
    global_digest: dict,
    existing_slugs: list[str],
    template: str = "",
) -> str:
    """Build a focused prompt for generating ONE entity page."""
    raw_rel = canonical_source_path(file_path, config)

    language_directive = build_language_directive(
        f"{entity_name} " + json.dumps(global_digest, ensure_ascii=False))
    return f"""{language_directive}

# Role
Generate ONE wiki entity page. Output ONLY this one page, then stop.

# Entity to Generate
- Name: {entity_name}

# Source
Document: {file_path.stem}

# Existing Wiki Pages (avoid duplicates):
{', '.join(existing_slugs[:50]) or "(none)"}

# ⚠️ CRITICAL — START IMMEDIATELY WITH FILE BLOCK
Your FIRST line MUST be `---FILE:wiki/entities/{slug}.md---`
Do NOT write preamble, analysis, or commentary.

# Output Format — EXACT
---FILE:wiki/entities/{slug}.md---
---
type: entity
title: "{entity_name}"
tags: [...]
related: []
sources: ["{raw_rel}"]
created: {time.strftime('%Y-%m-%d')}
updated: {time.strftime('%Y-%m-%d')}
---

# {entity_name}

(Description, significance, key attributes, related concepts using [[wikilinks]])

---END FILE---

Generate the page NOW. Start with ---FILE:...
"""


def _stage_2_4_build_all_prompt(
    chunk_analyses: list[dict],
    file_path: Path,
    config: Config,
    template: str = "",
    existing_refs: dict | None = None,
    related_pages: list[dict] | None = None,
    source_context: str = "",
) -> str:
    """Build ONE generation prompt covering ALL chunks (NashSU single-shot parity).

    Aggregates concepts/entities across every chunk analysis, dedups by slug,
    and asks the LLM to emit FILE blocks for all of them in a single response.
    Replaces the former per-chunk generation loop (Stage 2.4 × N calls → 1 call).

    ``source_context`` (P1, 2026-06-27): raw source text, already trimmed to the
    caller's budget. When present it is injected so the LLM grounds each page in
    the source's OWN wording/formulas/examples instead of generic training-memory
    knowledge — NashSU parity (buildGenerationPrompt feeds trimmed sourceContext).
    Verified via the Hennessy A/B: analysis-only produced a wrong Amdahl's-Law
    formula (the popular p/n form) instead of the book's Fraction_enhanced form.
    """
    existing_refs = existing_refs or {}
    existing_slugs = list_existing_slugs(config)

    seen_concept_slugs: set[str] = set()
    concept_lines: list[str] = []
    concept_slugs: list[tuple[str, str]] = []
    concept_slug_stems: set[str] = set()
    for ca in chunk_analyses:
        if not isinstance(ca, dict) or "error" in ca:
            continue
        for c in ca.get("concepts_found", []):
            if not isinstance(c, dict):
                continue
            name = c.get("name", "")
            slug = slugify(name)
            if not name or slug in seen_concept_slugs:
                continue
            seen_concept_slugs.add(slug)
            imp = c.get("importance", "core")
            defn = c.get("definition", "")
            details = c.get("key_details", [])
            if name in existing_refs and existing_refs[name]:
                existing_slug = existing_refs[name][0]
                concept_lines.append(
                    f"  - {name} → ALREADY COVERED by [[{existing_slug}]]: "
                    f"do NOT generate a page; wikilink ONLY as [[{existing_slug}]] "
                    f"(never [[concepts/{slug}]])"
                )
            else:
                concept_lines.append(f"  - {name} (slug: concepts/{slug}) [{imp}]: {defn}")
                concept_slugs.append((name, f"concepts/{slug}"))
                concept_slug_stems.add(slug)
                for d in details[:3]:
                    concept_lines.append(f"      • {d}")

    seen_entity_slugs: set[str] = set()
    entity_lines: list[str] = []
    entity_slugs: list[tuple[str, str]] = []
    for ca in chunk_analyses:
        if not isinstance(ca, dict) or "error" in ca:
            continue
        for e in ca.get("entities_found", []):
            if not isinstance(e, dict):
                continue
            name = e.get("name", "")
            slug = slugify(name)
            if not name or slug in seen_entity_slugs:
                continue
            seen_entity_slugs.add(slug)
            sig = e.get("significance", "")
            if name in existing_refs and existing_refs[name]:
                existing_slug = existing_refs[name][0]
                entity_lines.append(
                    f"  - {name} → ALREADY COVERED by [[{existing_slug}]]: "
                    f"do NOT generate; wikilink ONLY as [[{existing_slug}]]"
                )
            elif slug in concept_slug_stems:
                entity_lines.append(
                    f"  - {name} (slug: entities/{slug}): {sig} "
                    f"[DUPLICATE OF CONCEPT concepts/{slug} — SKIP]"
                )
            else:
                entity_lines.append(f"  - {name} (slug: entities/{slug}): {sig}")
                entity_slugs.append((name, f"entities/{slug}"))

    # Same line-vs-concept caveat as _stage_2_4_build_prompt: these are LINE caps
    # and each concept emits ~4 lines, so a low cap silently drops tail concepts
    # (which stay linkable → broken links). Single-shot covers a whole small book
    # in one prompt, so allow generous headroom. Bumped 200→800 / 60→200 (2026-06-30).
    concept_str = "\n".join(concept_lines[:800]) if concept_lines else "(none)"
    entity_str = "\n".join(entity_lines[:200]) if entity_lines else "(none)"

    # Must-link targets (this book's slugs, Stage 2.3 existing_refs, related
    # pages) are always kept; the background fill of other existing wiki pages
    # is bounded — ranked by relevance to this book when over the room, not
    # cut alphabetically (which systematically dropped late-sorting CJK slugs;
    # see _rank_linkable_fill — deterministic, prompt-hash stable).
    must_link = set()
    for _, s in concept_slugs:
        must_link.add(s)
    for _, s in entity_slugs:
        must_link.add(s)
    for slugs in existing_refs.values():
        for s in slugs:
            must_link.add(s)
    for rp in (related_pages or []):
        slug = rp.get("slug") if isinstance(rp, dict) else None
        if slug:
            must_link.add(slug)
    fill = sorted(s for s in set(existing_slugs) if s not in must_link)
    room = max(0, 300 - len(must_link))
    if len(fill) > room:
        refs = [n for n, _s in concept_slugs] + [n for n, _s in entity_slugs]
        fill = sorted(_rank_linkable_fill(fill, refs)[:room])
    linkable_list = sorted(must_link) + fill
    linkable_str = "\n".join(f"  - {s}" for s in linkable_list) if linkable_list else "(none)"

    template_section = ""
    if template:
        template_section = f"\n# Document Type\n<template>\n{template[:1500]}\n</template>\n"

    if existing_refs:
        ref_lines = []
        for name, slugs in sorted(existing_refs.items()):
            links = ", ".join("[[{}]]".format(s) for s in slugs)
            ref_lines.append("  - {} → already exists as: {}".format(name, links))
        existing_refs_str = "\n".join(ref_lines)
    else:
        existing_refs_str = "(none — this source has no overlap with existing wiki)"

    if related_pages:
        rel_lines = [
            "  - [[{}]] (relationship: {})".format(rp["slug"], rp.get("relationship", "related"))
            for rp in related_pages
        ]
        related_pages_str = "\n".join(rel_lines)
    else:
        related_pages_str = "(none)"

    if source_context.strip():
        source_section = (
            "\n# Source Text (GROUND EVERY PAGE IN THIS — do not write from memory)\n"
            "The following is the raw source text (trimmed to budget). For every page:\n"
            "- Use the SOURCE'S OWN definitions, formulas, notation, variable names,\n"
            "  and worked examples — NOT the popular/textbook version from your memory.\n"
            "- If the source frames a concept a specific way (e.g. a particular formula\n"
            "  or set of variables), reproduce THAT framing; do not substitute a\n"
            "  generic equivalent.\n"
            "- Prefer the source's concrete numbers/examples over invented ones.\n"
            "- If a concept below is not covered by this excerpt, generate it from its\n"
            "  analysis entry as usual.\n"
            "<source>\n"
            f"{source_context}\n"
            "</source>\n"
        )
    else:
        source_section = ""

    formulas_section = _collect_formulas_block(chunk_analyses)
    schema_section = _schema_routing_block(config)
    tags_section = _tags_reuse_section(config)
    extra_rules = _extra_rules(_source_page_slug(file_path, config))
    raw_rel = canonical_source_path(file_path, config)

    language_sample = source_context or json.dumps(chunk_analyses, ensure_ascii=False)
    language_directive = build_language_directive(language_sample)
    return f"""{language_directive}

# Role
You are generating wiki pages for ALL chunks of a book in ONE pass. The complete
concept/entity lists aggregated across every chunk are below. Generate a page for
each one that is NOT marked ALREADY COVERED — in a single response.

# Source
Book: {file_path.stem}
Chunks: {len(chunk_analyses)}
{template_section}{source_section}{formulas_section}{schema_section}
# Existing wiki pages that overlap (Stage 2.3 — DO NOT regenerate; wikilink to them):
{existing_refs_str}

# Related (not duplicate) existing pages — wikilink to these where relevant, but still generate full new pages for the concepts/entities below:
{related_pages_str}

# Concepts found across ALL chunks (generate a page for each — skip ALREADY COVERED):
{concept_str}

# Entities found across ALL chunks (generate a page for key ones — skip ALREADY COVERED / DUPLICATE):
{entity_str}
{_ENTITY_RULES_SECTION}
# Supplementary foundational pages (use sparingly)
If the source clearly defines a foundational concept NOT in the lists above AND NOT
in the Linkable pages list below, you MAY generate a page for it at a new
`wiki/concepts/<kebab-slug>.md`. Only for genuinely page-worthy building blocks the
source actually explains — never for passing mentions. Do NOT [[wikilink]] to any
slug that is not either in the Linkable list or a page you generate in THIS response.

# ⚠️ CRITICAL — START IMMEDIATELY WITH FILE BLOCKS
- Your FIRST line of output MUST be `---FILE:wiki/concepts/...`
- Do NOT write any preamble, introduction, or commentary. IGNORED by parser.

# [[wikilink]] Rules — STRICT
Each concept/entity above includes a slug like (slug: concepts/foo-bar).
This is the EXACT [[wikilink]] you must use — kebab-case with type prefix.

Correct:  [[concepts/natural-convection-heat-sink]]  [[entities/bell-labs]]
WRONG:    [[Natural Convection Heat Sink]]  [[convection]]  [[concepts/litz-wire.md]]

# Linkable pages (ONLY these [[wikilinks]] are valid):
{linkable_str}

Rules:
1. ONLY use [[wikilinks]] from the "Linkable pages" list above.
2. Use the EXACT slug shown. Do not change case, add words, or invent new ones.
3. For concepts/entities below: use the slug from its "(slug: ...)" label.
4. If no matching slug exists, write the term as PLAIN TEXT with NO [[]].
5. NEVER use `/` in filenames (macOS rejects it). Use "-" instead.
6. Math: ALWAYS write formulas in LaTeX — inline $...$, display $$...$$. Transcribe
   each formula from the source / Formulas list verbatim (same variables, same form);
   never paraphrase a formula into prose or swap in a generic textbook version.
{extra_rules}
{_CONCEPT_SKELETON_SECTION}{tags_section}
# Output Format — EXACT
---FILE:wiki/concepts/<slug>.md---
---
type: concept
title: "..."
tags: [...]
related: [...]
sources: ["{raw_rel}"]
created: {time.strftime('%Y-%m-%d')}
updated: {time.strftime('%Y-%m-%d')}
---

# Title

(content)

---END FILE---
---FILE:wiki/entities/<slug>.md---
---
type: entity
title: "<entity name>"
tags: [...]
related: [...]
sources: ["{raw_rel}"]
created: {time.strftime('%Y-%m-%d')}
updated: {time.strftime('%Y-%m-%d')}
---

# <entity name>

(content)

---END FILE---

Generate a page for EVERY concept listed above that is NOT marked [ALREADY COVERED], in ONE response. Go!
"""


def stage_2_4_generate_all(
    chunk_analyses: list[dict],
    file_path: Path,
    config: Config,
    template: str = "",
    verbose: bool = False,
    existing_refs: dict | None = None,
    related_pages: list[dict] | None = None,
    source_context: str = "",
) -> tuple[list[tuple[str, str]], list[str], str | None]:
    """Single-shot generation: ONE LLM call for all chunks (NashSU parity, 2026-06-27).

    Replaces the per-chunk generation loop. Returns (file_blocks, generated_slugs,
    stop_reason). The per-concept fallback (caller-side) catches any concepts the
    single shot missed, including output-truncation gaps.
    """
    valid = [ca for ca in chunk_analyses if isinstance(ca, dict) and "error" not in ca]
    has_concepts = any(ca.get("concepts_found") for ca in valid)
    has_entities = any(ca.get("entities_found") for ca in valid)
    if not has_concepts and not has_entities:
        print("  [generate-all] no concepts or entities across all chunks — skipped")
        return [], [], None

    prompt = _stage_2_4_build_all_prompt(
        chunk_analyses, file_path, config, template,
        existing_refs=existing_refs, related_pages=related_pages,
        source_context=source_context,
    )
    gen_tokens = config.compute_max_tokens(16384)

    for attempt in range(4):
        try:
            t0 = time.time()
            if attempt == 0:
                print("  [generate-all] single-shot generating (all chunks)...", flush=True)
            response, stop_reason = call_anthropic_protocol(
                prompt, config, max_tokens=gen_tokens, label="single-shot generation")
            blocks = parse_file_blocks(response)
            dt = time.time() - t0
            generated_slugs: list[str] = []
            for path, _ in blocks:
                slug = file_block_slug(path)
                if slug not in generated_slugs:
                    generated_slugs.append(slug)
            tag = f" (retry #{attempt})" if attempt > 0 else ""
            print(f"  [generate-all] OK{tag} — {len(blocks)} blocks "
                  f"({len(response):,} chars, {stop_reason}) {dt:.0f}s")
            sr = str(stop_reason).lower()
            if "length" in sr or "max_tokens" in sr:
                print(f"  [generate-all] ⚠️ response truncated ({stop_reason}) — "
                      f"some pages may be missing; per-concept fallback will fill gaps "
                      f"if zero concept blocks result.")
            if verbose:
                print(f"    response: {response[:500]}...")
            return blocks, generated_slugs, stop_reason
        except Exception as e:
            if attempt < 3 and _is_retryable_exception(e):
                wait = _retry_jitter(2.0, attempt)
                print(f"  [generate-all] {type(e).__name__} retry {attempt+1}/4 — {wait:.1f}s...")
                time.sleep(wait)
                continue
            print(f"  [generate-all] FAILED: {e}")
            return [], [], None
    return [], [], None


def stage_2_4_generate_chunk(
    analysis: dict,
    chunk_idx: int,
    generated_slugs: list[str],
    file_path: Path,
    config: Config,
    template: str = "",
    verbose: bool = False,
    chunk_text: str = "",
    existing_refs: dict | None = None,
    related_pages: list[dict] | None = None,
) -> list[tuple[str, str]]:
    """Generate FILE blocks for a single chunk (extracted from stage_2_per_chunk_generation).

    Used by the analyze→generate pipeline in _do_prepare. ``existing_refs``
    (Stage 2.3 output: {concept_name: [wiki_slugs]}) is forwarded to the prompt
    so the LLM wikilinks to existing pages instead of regenerating them.
    ``related_pages`` (Stage 2.3's stage_2_3_resolve_proposed_connections
    output: [{"slug": ..., "relationship": ...}]) is forwarded so the LLM
    wikilinks new pages to genuinely *related* (not duplicate) existing pages.

    Returns list of (path, content) tuples.  Caller should append slugs to
    generated_slugs from the returned paths.
    """
    concepts_n = len(analysis.get("concepts_found", []))
    entities_n = len(analysis.get("entities_found", []))
    if concepts_n == 0 and entities_n == 0:
        print(f"  [chunk {chunk_idx+1}] (no concepts or entities — skipped)")
        return []

    prompt = _stage_2_4_build_prompt(
        analysis, chunk_text, chunk_idx, file_path, config, template,
        generated_slugs=generated_slugs, existing_refs=existing_refs,
        related_pages=related_pages,
    )
    gen_tokens = config.compute_max_tokens(16384)

    for attempt in range(4):
        try:
            t0 = time.time()
            if attempt == 0:
                print(f"  [chunk {chunk_idx+1}] generating ({concepts_n}c/{entities_n}e)...",
                      flush=True)
            response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=gen_tokens, label=f"chunk {chunk_idx+1} generation")
            blocks = parse_file_blocks(response)
            dt = time.time() - t0
            tag = f" (retry #{attempt})" if attempt > 0 else ""
            print(f"  [chunk {chunk_idx+1}] generate OK{tag} — "
                  f"{concepts_n}c/{entities_n}e → {len(blocks)} blocks "
                  f"({len(response):,} chars, {stop_reason}) {dt:.0f}s")
            if verbose:
                print(f"    response: {response[:500]}...")
            return blocks

        except Exception as e:
            if attempt < 3 and _is_retryable_exception(e):
                wait = _retry_jitter(2.0, attempt)
                err_label = type(e).__name__
                print(f"  [chunk {chunk_idx+1}] generate {err_label} retry {attempt+1}/4"
                      f" — {wait:.1f}s...")
                time.sleep(wait)
                continue
            print(f"  [chunk {chunk_idx+1}] generate FAILED: {e}")
            # No []-sentinel: returning [] let the gap be cached as "done"
            # downstream. Raise so the ingest pauses; cached chunks make the
            # resume cheap (no-silent-fallback).
            raise RuntimeError(
                f"Stage 2.4 chunk {chunk_idx+1} generation failed after "
                f"{attempt+1} attempt(s): {type(e).__name__}: {e}") from e



def _stage_2_4_extract_names(chunk_analyses: list[dict]) -> tuple[list[str], list[str]]:
    """Extract deduplicated concept and entity names from chunk analyses."""
    all_concepts: list[str] = []
    all_entities: list[str] = []
    for a in chunk_analyses:
        for c in a.get("concepts_found") or []:
            name = c.get("name", c) if isinstance(c, dict) else str(c)
            all_concepts.append(name)
        for e in a.get("entities_found") or []:
            name = e.get("name", e) if isinstance(e, dict) else str(e)
            all_entities.append(name)
    seen_c: set[str] = set()
    unique_concepts = [x for x in all_concepts if not (x in seen_c or seen_c.add(x))]  # type: ignore[func-returns-value]
    seen_e: set[str] = set()
    unique_entities = [x for x in all_entities if not (x in seen_e or seen_e.add(x))]  # type: ignore[func-returns-value]
    return unique_concepts, unique_entities


