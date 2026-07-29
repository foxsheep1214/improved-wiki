from __future__ import annotations

import json
import time
from pathlib import Path

from _config import Config
from _core import (
    canonical_source_path,
    slugify,
)
from _schema import (
    list_existing_slugs,
    load_purpose_md,
    load_schema_md,
    parse_wiki_schema_routing,
    schema_candidate_routes,
    schema_prompt_text,
)
from _llm_api import (
    _is_retryable_exception,
    _retry_jitter,
    call_anthropic_protocol,
)
from _file_block_repair import repair_truncated_file_blocks
from _stage_2_base import (
    _stage_2_title_cjk_bigrams,
    _stage_2_title_words,
    file_block_slug,
)
from _language import build_language_directive
from _frontmatter_array import parse_frontmatter_array
from _paths import iter_wiki_pages

# The accumulating "already generated" context fed into each chunk's prompt is
# bounded, mirroring NashSU's trimLongText(globalDigest). The full
# generated_slugs list still drives SKIP membership and the independently capped
# Linkable list; only the displayed context is windowed.
GENERATED_DISPLAY_MAX = 50

# Soft cap on the displayed Linkable-pages list. Must-link targets (this chunk's
# slugs, prior-chunk pages, Stage 2.3 existing_refs, related pages) are always
# kept; only the background fill of other existing wiki pages is bounded by this.
_LINKABLE_TOTAL_CAP = 400

# Stage 2.4 emits only optional key/typed pages; the mandatory source page is
# generated separately in Stage 2.6. This exact sentinel lets the model abstain
# when analysis candidates are real but none is important and substantively
# developed enough to deserve a standalone page or material update. Exact
# matching preserves the hard failure for empty/truncated/malformed responses.
_NO_KEY_PAGES_SENTINEL = "NO_KEY_PAGES"


def _is_key_concept_candidate(item: dict) -> bool:
    """Whether a Stage 2.2 concept is eligible for standalone generation.

    `mentioned` is retained only as optional analysis context. NashSU generates
    pages for key ideas identified in analysis, not for every term encountered.
    Missing importance defaults to eligible for backward-compatible checkpoints.
    """
    return str(item.get("importance", "core")).strip().lower() != "mentioned"


def _is_no_key_pages_response(response: str) -> bool:
    """Whether Stage 2.4 explicitly and cleanly selected zero optional pages."""
    return response.strip() == _NO_KEY_PAGES_SENTINEL


def _linkable_relevance_tokens(text: str) -> set:
    """Token set for linkable-fill relevance ranking: ASCII content words ∪ CJK
    character bigrams (reuses the Stage 2.3 tokenizers). Folder prefixes are
    dropped and -/_ split into words so a slug ("concepts/matched-filter") and
    a title ("Matched Filter") tokenize alike."""
    stem = text.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
    return _stage_2_title_words(stem) | _stage_2_title_cjk_bigrams(stem)


def _rank_linkable_fill(candidates: list[str], reference_texts: list[str]) -> list[str]:
    """Rank background-fill slugs by relevance to THIS source, best first.

    When the fill candidate set exceeds its cap, an ALPHABETICAL cut
    systematically drops late-sorting slugs — CJK sorts after ASCII, so Chinese
    pages vanish first as the wiki grows (observed live 2026-07-02 on the 2.6
    [:1500] cap; same disease as the fixed [:200]/[:300] caps). Instead, score
    each candidate by its best token/CJK-bigram Jaccard overlap against the
    source's own generated slugs/titles and keep the most relevant.

    Deterministic and cheap (pure token math, no LLM/network): order is
    (score desc, slug asc). Determinism matters for prompt-hash stability
    within one ingest — the existing_slugs snapshot is stable during a source's
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
#      the source page (needs the per-source source-page slug, hence a
#      builder function instead of a constant).
# 11-13 = NashSU subject-attribution rules, verbatim from ingest.ts:2218-2220.
#      Stage 2.6 carried a weaker paraphrase of these while 2.4 — which
#      produces nearly every concept/entity page — had none, so a book covering
#      several devices could silently attach one part's limits or benchmark
#      numbers to a neighbouring entity's page.
def _extra_rules(source_page_slug: str) -> str:
    return f"""7. related frontmatter — EXACT format: prefixed bare slugs, comma-separated,
   NO [[ ]] and NO .md — e.g. related: [concepts/matched-filter, entities/bell-labs].
8. Evidence anchors: formulas/data cite the source's chapter/section/equation/
   figure number (式(5-10), 图2.6, Table 8.1); a value read off a figure's curve
   must be marked "据图X.X".
9. slug uses the SOURCE language (中文源→中文slug, English source→English kebab);
   English terms belong in title, not slug, EXCEPT established acronyms
   (mti, cfar, dds) which may stay; never mixed 中英双拼 slugs.
10. When body text cites a figure number (图2.6 / Fig. 3-1), link it to the
    source page: [[{source_page_slug}|据图2.6]] — this source-page link is
    always valid even though it is not in the Linkable list. Never leave a
    bare figure number pointing nowhere; do NOT embed images.
11. Preserve subject boundaries: when a source discusses multiple entities/
    models/products/methods, keep claims, evaluations, limitations, benchmark
    results, and recommendations attached to the exact subject they describe.
12. Do not merge or generalize a claim about one subject into another
    subject's page solely because they share terms (for example context window
    size, benchmark name, dataset, architecture, or feature name).
13. If a page needs to mention another subject for comparison, write it
    explicitly as a comparison and cite which source/frontmatter `sources`
    entry supports that statement."""


def _source_page_slug(file_path: Path, config: Config) -> str:
    """Wikilink stem of this source's page: sources/<raw-rel-sans-ext>."""
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


def _schema_routing_block(config: Config) -> str:
    """Inject NashSU's authoritative schema routing and optional purpose."""
    text = load_schema_md(config)
    schema_context = schema_prompt_text(text)
    purpose_context = load_purpose_md(config).strip()[:6000]
    if not schema_context and not purpose_context:
        return ""

    routing = parse_wiki_schema_routing(text)
    route_lines = "\n".join(
        f"- `{page_type}` → `wiki/{route}/`"
        if route else f"- `{page_type}` → `wiki/`"
        for page_type, route in sorted(routing.items())
    )
    schema_block = (
        "\n# Project Schema and Routing (AUTHORITATIVE)\n"
        "<schema>\n"
        f"{schema_context}\n"
        "</schema>\n"
        "Use this schema as the primary routing and frontmatter contract. Prefer a\n"
        "schema-defined type over forcing content into entity/concept when a more\n"
        "specific declared type genuinely fits. Every generated page's frontmatter\n"
        "`type` MUST match its FILE directory. Do not invent schema-typed content\n"
        "that the source does not support.\n"
        f"Exact type→directory mapping:\n{route_lines}\n"
    ) if schema_context else ""
    lifecycle_lines: list[str] = []
    if "synthesis" in routing:
        lifecycle_lines.append(
            "- Generate a recommended `synthesis` as a cross-cutting summary "
            "or conclusion, not as a duplicate source summary. A current source "
            "may seed it when the schema permits; future source ingests merge "
            "additional evidence into the same page."
        )
    if "thesis" in routing:
        lifecycle_lines.append(
            "- Generate a recommended `thesis` as a falsifiable working "
            "hypothesis. It may begin with `status: speculative`; future source "
            "ingests update confidence/status and supporting or refuting links."
        )
    lifecycle_block = (
        "\n# NashSU Synthesis / Thesis Lifecycle\n"
        "Stage 2.2 has already selected these schema-typed candidates from "
        "source evidence. Do not add a second consensus or source-count gate "
        "that the project schema does not require.\n"
        + "\n".join(lifecycle_lines)
        + "\n"
    ) if lifecycle_lines else ""
    purpose_block = (
        "\n# Wiki Purpose\n"
        "<purpose>\n"
        f"{purpose_context}\n"
        "</purpose>\n"
        "Use the purpose to prioritize what matters; never let it override source\n"
        "evidence or the schema routing contract.\n"
    ) if purpose_context else ""
    return schema_block + lifecycle_block + purpose_block


def _schema_typed_output_section(raw_rel: str) -> str:
    """Generic FILE-block contract for every schema-declared candidate type."""
    today = time.strftime("%Y-%m-%d")
    return f"""
# Schema-Typed Page Output
For every schema-typed candidate, use the EXACT `(slug: <folder>/<stem>)` path
and `[type]` shown above. Follow that type's semantic and frontmatter rules from
the authoritative schema; include every type-specific required field. Do not
coerce a comparison, synthesis, thesis, methodology, finding, or custom type
into a generic concept/entity page.

For `synthesis`, integrate the material cross-cutting conclusion supported by
this source, keep it distinct from the source summary, and cite every
contributing source/page actually available. A first source may seed the page
when the schema permits; do not fabricate other sources from index titles.

For `thesis`, state a falsifiable working hypothesis, distinguish direct
evidence from inference, and include the schema-required confidence/status
fields. A first source may establish `status: speculative`; later ingests update
the same page with supporting or refuting evidence.

---FILE:wiki/<schema-folder>/<slug>.md---
---
type: <schema-declared-type>
title: "..."
tags: [...]
related: [...]
sources: ["{raw_rel}"]
created: {today}
updated: {today}
# plus every field required for this type by schema.md
---

# Title

(source-grounded content that satisfies this type's schema semantics)

---END FILE---
"""


def _schema_typed_context_section(analyses: list[dict]) -> str:
    """Compact rolling context for typed candidates that span source chunks."""
    digest = next(
        (
            analysis.get("updated_global_digest")
            for analysis in reversed(analyses)
            if isinstance(analysis, dict)
            and analysis.get("updated_global_digest")
        ),
        None,
    )
    if isinstance(digest, dict):
        text = json.dumps(digest, ensure_ascii=False, indent=2)
    else:
        text = str(digest or "").strip()
    if not text:
        return ""
    if len(text) > 15_000:
        text = text[:15_000] + "\n... (truncated)"
    return (
        "\n# Rolling Source Digest for Schema-Typed Candidates\n"
        "Use this compact, source-derived cross-chunk context only when a typed "
        "candidate spans chunks. The raw source context remains the primary "
        "grounding; never turn digest shorthand into unsupported detail.\n"
        "<rolling_digest>\n"
        f"{text}\n"
        "</rolling_digest>\n"
    )


def _normalize_existing_ref(value: object) -> str:
    """Normalize a Stage 2.3 target to a wiki-relative bare slug path."""
    ref = str(value or "").strip().replace("\\", "/").strip("/")
    if ref.startswith("wiki/"):
        ref = ref[len("wiki/"):]
    if ref.endswith(".md"):
        ref = ref[:-3]
    return ref.strip("/")


def _existing_targets(name: str, existing_refs: dict) -> list[str]:
    """Return normalized, stable-deduplicated association targets for ``name``."""
    values = existing_refs.get(name, []) or []
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        ref = _normalize_existing_ref(value)
        if ref and ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


def _same_route_existing_target(
    name: str,
    folder: str,
    existing_refs: dict,
) -> str | None:
    """Resolve an association that is safe to update in its declared route.

    Legacy bare-stem associations deliberately remain link-only: without their
    directory, we cannot prove that the existing page has the same type.
    Fresh Stage 2.3 output is always prefixed.
    """
    prefix = f"{folder.strip('/')}/"
    return next(
        (ref for ref in _existing_targets(name, existing_refs)
         if ref.startswith(prefix)),
        None,
    )


def _candidate_was_generated(full_slug: str, generated_slugs: list[str]) -> bool:
    """Whether another chunk already owns/generated this deterministic target."""
    normalized = {_normalize_existing_ref(slug) for slug in generated_slugs}
    stem = full_slug.rsplit("/", 1)[-1]
    return full_slug in normalized or stem in normalized


def _candidate_requires_file(
    name: str,
    folder: str,
    existing_refs: dict,
    generated_slugs: list[str] | None = None,
) -> bool:
    """True for a new page or a same-type existing page that needs an update."""
    canonical = f"{folder}/{slugify(name)}"
    if generated_slugs and _candidate_was_generated(canonical, generated_slugs):
        return False
    refs = _existing_targets(name, existing_refs)
    return not refs or _same_route_existing_target(name, folder, existing_refs) is not None


def _schema_candidate_targets_by_name(
    analyses: list[dict],
    config: Config,
) -> dict[str, str]:
    """First eligible schema route for each candidate name."""
    routes = schema_candidate_routes(load_schema_md(config))
    targets: dict[str, str] = {}
    for analysis in analyses:
        if not isinstance(analysis, dict):
            continue
        for candidate in analysis.get("schema_typed_candidates", []) or []:
            if not isinstance(candidate, dict):
                continue
            name = str(candidate.get("name", "")).strip()
            folder = routes.get(str(candidate.get("type", "")).strip())
            stem = slugify(name)
            if name and folder and stem:
                targets.setdefault(name, f"{folder}/{stem}")
    return targets


def _schema_candidate_inventory(
    analyses: list[dict],
    config: Config,
    existing_refs: dict,
    generated_slugs: list[str],
) -> tuple[list[str], list[tuple[str, str]]]:
    """Normalize Stage 2.2 candidates against the authoritative type→dir map."""
    routes = schema_candidate_routes(load_schema_md(config))
    if not routes:
        return [], []

    lines: list[str] = []
    slugs: list[tuple[str, str]] = []
    seen_stems: set[str] = set()
    for analysis in analyses:
        for cand in analysis.get("schema_typed_candidates", []) or []:
            if not isinstance(cand, dict):
                continue
            name = str(cand.get("name", "")).strip()
            cand_type = str(cand.get("type", "")).strip()
            folder = routes.get(cand_type)
            if not name or not folder:
                continue
            stem = slugify(name)
            if not stem:
                continue
            full_slug = f"{folder}/{stem}"
            if stem in seen_stems:
                continue
            seen_stems.add(stem)
            if _candidate_was_generated(full_slug, generated_slugs):
                lines.append(
                    f"  - {name} (slug: {full_slug}) [ALREADY COVERED — SKIP]"
                )
                continue

            target = _same_route_existing_target(
                name, folder, existing_refs)
            refs = _existing_targets(name, existing_refs)
            rationale = str(cand.get("rationale", "")).strip()
            if target:
                lines.append(
                    f"  - {name} (slug: {target}) "
                    f"[{cand_type}; UPDATE EXISTING PAGE]: {rationale}"
                )
                slugs.append((name, target))
            elif refs:
                target = refs[0]
                lines.append(
                    f"  - {name} → CROSS-TYPE ASSOCIATION [[{target}]]: "
                    f"do NOT create a duplicate page; wikilink ONLY as [[{target}]]"
                )
            else:
                lines.append(
                    f"  - {name} (slug: {full_slug}) [{cand_type}]: {rationale}"
                )
                slugs.append((name, full_slug))
    return lines, slugs


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
    """Build prompt to generate key and schema-typed pages from one chunk.

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

    # Resolve specific schema types before rendering generic concepts/entities.
    # A same-name comparison/synthesis/finding/methodology/thesis is one typed
    # page, not a second generic page with the same stem.
    schema_candidate_lines, schema_candidate_slugs = _schema_candidate_inventory(
        [chunk_analysis], config, existing_refs, generated_slugs
    )
    schema_candidate_targets = _schema_candidate_targets_by_name(
        [chunk_analysis], config)
    schema_candidates_str = (
        "\n".join(schema_candidate_lines) if schema_candidate_lines else "(none)"
    )

    concept_lines = []
    concept_slugs: list[tuple[str, str]] = []  # (name, slug) for wikilink reference
    concept_slug_stems: set[str] = set()  # for entity-dedup (Issue 4)
    for c in concepts:
        if isinstance(c, dict):
            if not _is_key_concept_candidate(c):
                continue
            name = c.get("name", "")
            imp = c.get("importance", "core")
            defn = c.get("definition", "")
            details = c.get("key_details", [])
            slug = slugify(name)
            if name in schema_candidate_targets:
                concept_lines.append(
                    f"  - {name} (slug: concepts/{slug}) [{imp}]: {defn} "
                    f"[ROUTED AS {schema_candidate_targets[name]} BY SCHEMA — "
                    "SKIP GENERIC CONCEPT]"
                )
            elif _candidate_was_generated(
                    f"concepts/{slug}", generated_slugs):
                concept_lines.append(
                    f"  - {name} (slug: concepts/{slug}) [{imp}]: {defn} "
                    "[ALREADY COVERED — SKIP]"
                )
            else:
                existing_slug = _same_route_existing_target(
                    name, "concepts", existing_refs)
                refs = _existing_targets(name, existing_refs)
                if existing_slug:
                    concept_lines.append(
                        f"  - {name} (slug: {existing_slug}) [{imp}; "
                        f"UPDATE EXISTING PAGE]: {defn}"
                    )
                    concept_slugs.append((name, existing_slug))
                    concept_slug_stems.add(slug)
                    for d in details[:3]:
                        concept_lines.append(f"      • {d}")
                elif refs:
                    existing_slug = refs[0]
                    concept_lines.append(
                        f"  - {name} → CROSS-TYPE ASSOCIATION "
                        f"[[{existing_slug}]]: do NOT create a duplicate page; "
                        f"wikilink ONLY as [[{existing_slug}]] "
                        f"(never [[concepts/{slug}]])"
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
        normalized = _normalize_existing_ref(s)
        if normalized.startswith("concepts/"):
            concept_slug_stems.add(normalized.split("/", 1)[1])

    entity_lines = []
    entity_slugs: list[tuple[str, str]] = []  # (name, slug) for wikilink reference
    for e in entities:
        if isinstance(e, dict):
            name = e.get("name", "")
            sig = e.get("significance", "")
            slug = slugify(name)
            if name in schema_candidate_targets:
                entity_lines.append(
                    f"  - {name} (slug: entities/{slug}): {sig} "
                    f"[ROUTED AS {schema_candidate_targets[name]} BY SCHEMA — "
                    "SKIP GENERIC ENTITY]"
                )
            elif (_candidate_was_generated(
                    f"entities/{slug}", generated_slugs)
                    or slug in concept_slug_stems):
                # Issue 4: a concept page for this slug already exists (this chunk
                # or a prior one) — skip the duplicate entity page; wikilink to
                # the concept page instead.
                entity_lines.append(
                    f"  - {name} (slug: entities/{slug}): {sig} "
                    f"[DUPLICATE OF CONCEPT concepts/{slug} — SKIP]"
                )
            else:
                existing_slug = _same_route_existing_target(
                    name, "entities", existing_refs)
                refs = _existing_targets(name, existing_refs)
                if existing_slug:
                    entity_lines.append(
                        f"  - {name} (slug: {existing_slug}) "
                        f"[UPDATE EXISTING PAGE]: {sig}"
                    )
                    entity_slugs.append((name, existing_slug))
                elif refs:
                    existing_slug = refs[0]
                    entity_lines.append(
                        f"  - {name} → CROSS-TYPE ASSOCIATION "
                        f"[[{existing_slug}]]: do NOT create a duplicate page; "
                        f"wikilink ONLY as [[{existing_slug}]]"
                    )
                else:
                    entity_lines.append(
                        f"  - {name} (slug: entities/{slug}): {sig}"
                    )
                    entity_slugs.append((name, f"entities/{slug}"))

    # Stage 2.2 has already curated key candidates. Do not impose a second,
    # arbitrary line/count cap here: pass every recommended candidate while
    # excluding `mentioned` concepts above.
    concept_str = "\n".join(concept_lines) if concept_lines else "(none)"
    entity_str = "\n".join(entity_lines) if entity_lines else "(none)"

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
    for name in existing_refs:
        must_link.update(_existing_targets(name, existing_refs))
    for rp in (related_pages or []):
        slug = rp.get("slug") if isinstance(rp, dict) else None
        if slug:
            must_link.add(slug)
    # Background fill: other existing wiki pages, bounded so the prompt stays a
    # reasonable size. Never displaces a must-link target. When candidates
    # exceed the room, keep the most RELEVANT to this source (token/CJK-bigram
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

    # Stage 2.3 feedback: same-type pages are update targets; cross-type pages
    # are link-only associations. The per-candidate lines carry that distinction.
    if existing_refs:
        ref_lines = []
        # Sort for deterministic prompt text → stable conversation-handoff cache
        # key. Without sorting, set/dict iteration order (Python hash randomization)
        # varies across runs, changing the prompt hash and re-prompting Stage 2.4
        # forever (cache never hits on resume).
        for name in sorted(existing_refs):
            links = ", ".join(
                "[[{}]]".format(s)
                for s in _existing_targets(name, existing_refs)
            )
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
    # what gives full-concept fidelity for sources of ANY size — each chunk's
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
    schema_context_section = (
        _schema_typed_context_section([chunk_analysis])
        if schema_candidate_slugs else ""
    )
    schema_output_section = (
        _schema_typed_output_section(raw_rel)
        if schema_candidate_slugs else ""
    )

    language_directive = build_language_directive(chunk_text)
    return f"""{language_directive}

# Role
You are generating wiki pages for ONE chunk of a source. Previous chunks have
already been processed — their pages are listed below. Do NOT regenerate them.

# Source
Source: {file_path.stem}
Chunk: {chunk_index + 1}

{template_section}
{chunk_source_section}{schema_context_section}{formulas_section}{schema_section}# Pages already generated by previous chunks (SKIP these):
{generated_str}

# Existing wiki associations (Stage 2.3):
# - same-type target → generate its exact FILE path as an UPDATE
# - cross-type target → do not duplicate; wikilink only
{existing_refs_str}

# Related (not duplicate) existing pages — wikilink to these where relevant, but still generate the recommended new pages below:
{related_pages_str}

# Key concept page candidates recommended by the analysis:
{concept_str}

# Key entity page candidates recommended by the analysis:
{entity_str}
{_ENTITY_RULES_SECTION}
# Key schema-typed page candidates recommended by the analysis:
{schema_candidates_str}

# NashSU generation policy
- Generate FILE blocks for genuinely important recommended candidates that are
  new OR marked UPDATE EXISTING PAGE. The writer will merge update blocks into
  their exact existing paths.
- Do not generate candidates marked ALREADY COVERED/SKIP or CROSS-TYPE.
- A recommended synthesis/thesis candidate has already passed Stage 2.2's
  evidence-selection gate. Generate it unless it is marked SKIP/CROSS-TYPE or
  the authoritative project schema itself rejects it; do not silently drop it
  merely because it starts from one source or remains speculative.
- Do not create pages for passing mentions, background prerequisites, or extra
  "foundational" terms that were not recommended.
- There is no page-count target. Do not pad or split one coherent topic merely
  to increase the number of FILE blocks.
- Every candidate remains optional at generation time. If NONE is genuinely
  important and substantively developed enough for a standalone page or
  material update, output exactly `{_NO_KEY_PAGES_SENTINEL}` and nothing else.
  Stage 2.6 generates the mandatory source page separately.

# ⚠️ CRITICAL — START IMMEDIATELY WITH THE RESULT
- If at least one candidate qualifies, your FIRST line MUST start with
  `---FILE:wiki/`.
- Otherwise output exactly `{_NO_KEY_PAGES_SENTINEL}`.
- Do NOT write any preamble, introduction, or commentary. IGNORED by parser.

# [[wikilink]] Rules — STRICT
Each candidate above includes an exact slug like (slug: concepts/foo-bar) or
(slug: comparisons/foo-vs-bar).
This is the EXACT [[wikilink]] you must use — kebab-case with type prefix.

Correct format:
  [[concepts/natural-convection-heat-sink]]  ← kebab-case + concepts/ prefix
  [[entities/bell-labs]]                      ← kebab-case + entities/ prefix
  [[comparisons/mti-vs-pulse-doppler]]        ← schema-typed exact path

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
3. For candidates IN THIS CHUNK: use the slug from its "(slug: ...)" label.
4. If no matching slug exists, write the term as PLAIN TEXT with NO [[]].
5. NEVER use `/` in filenames (macOS rejects it). Use "-" instead.
6. Math: ALWAYS write formulas in LaTeX — inline $...$, display $$...$$. Transcribe
   each formula from the source / Formulas list verbatim (same variables, same form);
   never paraphrase a formula into prose or swap in a generic textbook version.
7. Result-file integrity: every LaTeX command must retain a literal reverse-solidus
   (U+005C) before its command name in the final .txt file. Never emit C0 control
   characters in math (especially form-feed, carriage-return, or tab).
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
{schema_output_section}

Generate only qualifying new and UPDATE EXISTING key pages that are not marked
[ALREADY COVERED]/[SKIP]/CROSS-TYPE. Start with the first FILE block, or output
exactly `{_NO_KEY_PAGES_SENTINEL}` if none qualifies.
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

    Aggregates key concepts/entities and schema-typed candidates across every
    chunk analysis, dedups by slug, and asks the LLM to emit FILE blocks for
    all of them in a single response.
    Replaces the former per-chunk generation loop (Stage 2.4 × N calls → 1 call).

    ``source_context`` (P1, 2026-06-27): raw source text, already trimmed to the
    caller's budget. When present it is injected so the LLM grounds each page in
    the source's OWN wording/formulas/examples instead of generic training-memory
    knowledge — NashSU parity (buildGenerationPrompt feeds trimmed sourceContext).
    Verified via the Hennessy A/B: analysis-only produced a wrong Amdahl's-Law
    formula (the popular p/n form) instead of the source's Fraction_enhanced form.
    """
    existing_refs = existing_refs or {}
    existing_slugs = list_existing_slugs(config)

    schema_candidate_lines, schema_candidate_slugs = _schema_candidate_inventory(
        chunk_analyses, config, existing_refs, []
    )
    schema_candidate_targets = _schema_candidate_targets_by_name(
        chunk_analyses, config)
    schema_candidates_str = (
        "\n".join(schema_candidate_lines)
        if schema_candidate_lines else "(none)"
    )

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
            if not _is_key_concept_candidate(c):
                continue
            name = c.get("name", "")
            slug = slugify(name)
            if not name or slug in seen_concept_slugs:
                continue
            seen_concept_slugs.add(slug)
            imp = c.get("importance", "core")
            defn = c.get("definition", "")
            details = c.get("key_details", [])
            if name in schema_candidate_targets:
                concept_lines.append(
                    f"  - {name} (slug: concepts/{slug}) [{imp}]: {defn} "
                    f"[ROUTED AS {schema_candidate_targets[name]} BY SCHEMA — "
                    "SKIP GENERIC CONCEPT]"
                )
            else:
                existing_slug = _same_route_existing_target(
                    name, "concepts", existing_refs)
                refs = _existing_targets(name, existing_refs)
                if existing_slug:
                    concept_lines.append(
                        f"  - {name} (slug: {existing_slug}) [{imp}; "
                        f"UPDATE EXISTING PAGE]: {defn}"
                    )
                    concept_slugs.append((name, existing_slug))
                    concept_slug_stems.add(slug)
                    for d in details[:3]:
                        concept_lines.append(f"      • {d}")
                elif refs:
                    existing_slug = refs[0]
                    concept_lines.append(
                        f"  - {name} → CROSS-TYPE ASSOCIATION "
                        f"[[{existing_slug}]]: do NOT create a duplicate page; "
                        f"wikilink ONLY as [[{existing_slug}]] "
                        f"(never [[concepts/{slug}]])"
                    )
                else:
                    concept_lines.append(
                        f"  - {name} (slug: concepts/{slug}) [{imp}]: {defn}"
                    )
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
            if name in schema_candidate_targets:
                entity_lines.append(
                    f"  - {name} (slug: entities/{slug}): {sig} "
                    f"[ROUTED AS {schema_candidate_targets[name]} BY SCHEMA — "
                    "SKIP GENERIC ENTITY]"
                )
            elif slug in concept_slug_stems:
                entity_lines.append(
                    f"  - {name} (slug: entities/{slug}): {sig} "
                    f"[DUPLICATE OF CONCEPT concepts/{slug} — SKIP]"
                )
            else:
                existing_slug = _same_route_existing_target(
                    name, "entities", existing_refs)
                refs = _existing_targets(name, existing_refs)
                if existing_slug:
                    entity_lines.append(
                        f"  - {name} (slug: {existing_slug}) "
                        f"[UPDATE EXISTING PAGE]: {sig}"
                    )
                    entity_slugs.append((name, existing_slug))
                elif refs:
                    existing_slug = refs[0]
                    entity_lines.append(
                        f"  - {name} → CROSS-TYPE ASSOCIATION "
                        f"[[{existing_slug}]]: do NOT create a duplicate page; "
                        f"wikilink ONLY as [[{existing_slug}]]"
                    )
                else:
                    entity_lines.append(
                        f"  - {name} (slug: entities/{slug}): {sig}"
                    )
                    entity_slugs.append((name, f"entities/{slug}"))

    concept_str = "\n".join(concept_lines) if concept_lines else "(none)"
    entity_str = "\n".join(entity_lines) if entity_lines else "(none)"
    # Must-link targets (this source's slugs, Stage 2.3 existing_refs, related
    # pages) are always kept; the background fill of other existing wiki pages
    # is bounded — ranked by relevance to this source when over the room, not
    # cut alphabetically (which systematically dropped late-sorting CJK slugs;
    # see _rank_linkable_fill — deterministic, prompt-hash stable).
    must_link = set()
    for _, s in concept_slugs:
        must_link.add(s)
    for _, s in entity_slugs:
        must_link.add(s)
    for _, s in schema_candidate_slugs:
        must_link.add(s)
    for name in existing_refs:
        must_link.update(_existing_targets(name, existing_refs))
    for rp in (related_pages or []):
        slug = rp.get("slug") if isinstance(rp, dict) else None
        if slug:
            must_link.add(slug)
    fill = sorted(s for s in set(existing_slugs) if s not in must_link)
    room = max(0, 300 - len(must_link))
    if len(fill) > room:
        refs = (
            [n for n, _s in concept_slugs]
            + [n for n, _s in entity_slugs]
            + [n for n, _s in schema_candidate_slugs]
        )
        fill = sorted(_rank_linkable_fill(fill, refs)[:room])
    linkable_list = sorted(must_link) + fill
    linkable_str = "\n".join(f"  - {s}" for s in linkable_list) if linkable_list else "(none)"

    template_section = ""
    if template:
        template_section = f"\n# Document Type\n<template>\n{template[:1500]}\n</template>\n"

    if existing_refs:
        ref_lines = []
        for name in sorted(existing_refs):
            links = ", ".join(
                "[[{}]]".format(s)
                for s in _existing_targets(name, existing_refs)
            )
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
    schema_context_section = (
        _schema_typed_context_section(chunk_analyses)
        if schema_candidate_slugs else ""
    )
    schema_output_section = (
        _schema_typed_output_section(raw_rel)
        if schema_candidate_slugs else ""
    )

    language_sample = source_context or json.dumps(chunk_analyses, ensure_ascii=False)
    language_directive = build_language_directive(language_sample)
    return f"""{language_directive}

# Role
You are generating wiki pages for ALL chunks of a source in ONE pass. The analysis
recommendations below contain key page candidates, not an exhaustive term
inventory. Generate only genuinely important recommended pages that are new or
marked UPDATE EXISTING PAGE.

# Source
Source: {file_path.stem}
Chunks: {len(chunk_analyses)}
{template_section}{source_section}{schema_context_section}{formulas_section}{schema_section}
# Existing wiki associations (Stage 2.3):
# - same-type target → generate its exact FILE path as an UPDATE
# - cross-type target → do not duplicate; wikilink only
{existing_refs_str}

# Related (not duplicate) existing pages — wikilink to these where relevant, but still generate the recommended new pages below:
{related_pages_str}

# Key concept page candidates recommended across all chunks:
{concept_str}

# Key entity page candidates recommended across all chunks:
{entity_str}
{_ENTITY_RULES_SECTION}
# Key schema-typed page candidates recommended across all chunks:
{schema_candidates_str}

# NashSU generation policy
- Generate FILE blocks for genuinely important recommended candidates that are
  new OR marked UPDATE EXISTING PAGE. The writer will merge update blocks into
  their exact existing paths.
- Do not generate candidates marked ALREADY COVERED/SKIP or CROSS-TYPE.
- A recommended synthesis/thesis candidate has already passed Stage 2.2's
  evidence-selection gate. Generate it unless it is marked SKIP/CROSS-TYPE or
  the authoritative project schema itself rejects it; do not silently drop it
  merely because it starts from one source or remains speculative.
- Do not add pages for passing mentions, background prerequisites, or
  supplementary terms that were not recommended.
- There is no page-count target. Do not pad or split one coherent topic merely
  to increase the number of FILE blocks.
- Every candidate remains optional at generation time. If NONE is genuinely
  important and substantively developed enough for a standalone page or
  material update, output exactly `{_NO_KEY_PAGES_SENTINEL}` and nothing else.
  Stage 2.6 generates the mandatory source page separately.

# ⚠️ CRITICAL — START IMMEDIATELY WITH THE RESULT
- If at least one candidate qualifies, your FIRST line MUST start with
  `---FILE:wiki/`.
- Otherwise output exactly `{_NO_KEY_PAGES_SENTINEL}`.
- Do NOT write any preamble, introduction, or commentary. IGNORED by parser.

# [[wikilink]] Rules — STRICT
Each candidate above includes an exact slug like (slug: concepts/foo-bar) or
(slug: comparisons/foo-vs-bar).
This is the EXACT [[wikilink]] you must use — kebab-case with type prefix.

Correct:  [[concepts/natural-convection-heat-sink]]  [[entities/bell-labs]]
          [[comparisons/mti-vs-pulse-doppler]]
WRONG:    [[Natural Convection Heat Sink]]  [[convection]]  [[concepts/litz-wire.md]]

# Linkable pages (ONLY these [[wikilinks]] are valid):
{linkable_str}

Rules:
1. ONLY use [[wikilinks]] from the "Linkable pages" list above.
2. Use the EXACT slug shown. Do not change case, add words, or invent new ones.
3. For candidates below: use the slug from its "(slug: ...)" label.
4. If no matching slug exists, write the term as PLAIN TEXT with NO [[]].
5. NEVER use `/` in filenames (macOS rejects it). Use "-" instead.
6. Math: ALWAYS write formulas in LaTeX — inline $...$, display $$...$$. Transcribe
   each formula from the source / Formulas list verbatim (same variables, same form);
   never paraphrase a formula into prose or swap in a generic textbook version.
7. Result-file integrity: every LaTeX command must retain a literal reverse-solidus
   (U+005C) before its command name in the final .txt file. Never emit C0 control
   characters in math (especially form-feed, carriage-return, or tab).
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
{schema_output_section}

Generate only qualifying new and UPDATE EXISTING key pages that are not marked
[ALREADY COVERED]/[SKIP]/CROSS-TYPE, in one response. Start with the first FILE
block, or output exactly `{_NO_KEY_PAGES_SENTINEL}` if none qualifies.
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

    Returns (file_blocks, generated_slugs, stop_reason). An unclosed FILE path
    gets one NashSU-style targeted repair; an unrecovered or malformed response
    fails visibly. No per-item coverage backfill or page quota is used.
    """
    valid = [ca for ca in chunk_analyses if isinstance(ca, dict) and "error" not in ca]
    existing_refs = existing_refs or {}
    schema_targets = _schema_candidate_targets_by_name(valid, config)
    has_concepts = any(
        any(
            isinstance(c, dict)
            and _is_key_concept_candidate(c)
            and bool(name := str(c.get("name", "")).strip())
            and name not in schema_targets
            and _candidate_requires_file(
                name, "concepts", existing_refs)
            for c in (ca.get("concepts_found", []) or [])
        )
        for ca in valid
    )
    has_entities = any(
        any(
            isinstance(e, dict)
            and bool(name := str(e.get("name", "")).strip())
            and name not in schema_targets
            and _candidate_requires_file(
                name, "entities", existing_refs)
            for e in (ca.get("entities_found", []) or [])
        )
        for ca in valid
    )
    candidate_routes = schema_candidate_routes(load_schema_md(config))
    has_schema_candidates = any(
        any(
            isinstance(cand, dict)
            and bool(folder := candidate_routes.get(
                str(cand.get("type", "")).strip()))
            and bool(name := str(cand.get("name", "")).strip())
            and _candidate_requires_file(
                name, folder, existing_refs)
            for cand in (ca.get("schema_typed_candidates", []) or [])
        )
        for ca in valid
    )
    if not has_concepts and not has_entities and not has_schema_candidates:
        print("  [generate-all] no concepts, entities, or schema-typed candidates — skipped")
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
            repair = repair_truncated_file_blocks(
                response,
                original_prompt=prompt,
                source_identity=canonical_source_path(file_path, config),
                config=config,
                max_tokens=config.compute_max_tokens(8192),
                label="stage 2.4",
                llm_call=call_anthropic_protocol,
            )
            blocks = repair.blocks
            if repair.unrecovered_paths:
                raise RuntimeError(
                    "Stage 2.4 targeted FILE repair did not recover: "
                    + ", ".join(repair.unrecovered_paths)
                )
            dt = time.time() - t0
            generated_slugs: list[str] = []
            for path, _ in blocks:
                slug = file_block_slug(path)
                if slug not in generated_slugs:
                    generated_slugs.append(slug)
            tag = f" (retry #{attempt})" if attempt > 0 else ""
            print(f"  [generate-all] OK{tag} — {len(blocks)} blocks "
                  f"({len(response):,} chars, {stop_reason}) {dt:.0f}s")
            if not blocks:
                if _is_no_key_pages_response(response):
                    print(
                        "  [generate-all] model selected 0 optional key pages; "
                        "the mandatory source page remains Stage 2.6"
                    )
                    return [], [], stop_reason
                raise RuntimeError(
                    "Stage 2.4 produced 0 FILE blocks without the exact "
                    f"{_NO_KEY_PAGES_SENTINEL} sentinel.")
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
            raise RuntimeError(
                "Stage 2.4 single-shot generation failed after "
                f"{attempt + 1} attempt(s): {type(e).__name__}: {e}"
            ) from e
    raise RuntimeError("Stage 2.4 single-shot generation exhausted retries")


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
    (Stage 2.3 output: {candidate_name: [type-prefixed wiki slugs]}) is
    forwarded so the LLM updates same-type targets and only links cross-type
    associations.
    ``related_pages`` (Stage 2.3's stage_2_3_resolve_proposed_connections
    output: [{"slug": ..., "relationship": ...}]) is forwarded so the LLM
    wikilinks new pages to genuinely *related* (not duplicate) existing pages.

    Returns list of (path, content) tuples.  Caller should append slugs to
    generated_slugs from the returned paths.
    """
    existing_refs = existing_refs or {}
    schema_targets = _schema_candidate_targets_by_name([analysis], config)
    concepts_n = sum(
        1 for c in (analysis.get("concepts_found", []) or [])
        if isinstance(c, dict)
        and _is_key_concept_candidate(c)
        and bool(name := str(c.get("name", "")).strip())
        and name not in schema_targets
        and _candidate_requires_file(
            name, "concepts", existing_refs, generated_slugs)
    )
    entities_n = sum(
        1 for e in (analysis.get("entities_found", []) or [])
        if isinstance(e, dict)
        and bool(name := str(e.get("name", "")).strip())
        and name not in schema_targets
        and _candidate_requires_file(
            name, "entities", existing_refs, generated_slugs)
    )
    candidate_routes = schema_candidate_routes(load_schema_md(config))
    schema_candidates_n = sum(
        1 for cand in (analysis.get("schema_typed_candidates", []) or [])
        if isinstance(cand, dict)
        and bool(folder := candidate_routes.get(
            str(cand.get("type", "")).strip()))
        and bool(name := str(cand.get("name", "")).strip())
        and _candidate_requires_file(
            name, folder, existing_refs, generated_slugs)
    )
    if concepts_n == 0 and entities_n == 0 and schema_candidates_n == 0:
        print(f"  [chunk {chunk_idx+1}] (no concepts, entities, or schema candidates — skipped)")
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
                print(f"  [chunk {chunk_idx+1}] generating "
                      f"({concepts_n}c/{entities_n}e/{schema_candidates_n}s)...",
                      flush=True)
            response, stop_reason = call_anthropic_protocol(prompt, config, max_tokens=gen_tokens, label=f"chunk {chunk_idx+1} generation")
            repair = repair_truncated_file_blocks(
                response,
                original_prompt=prompt,
                source_identity=canonical_source_path(file_path, config),
                config=config,
                max_tokens=config.compute_max_tokens(8192),
                label=f"stage 2.4 chunk {chunk_idx + 1}",
                llm_call=call_anthropic_protocol,
            )
            blocks = repair.blocks
            if repair.unrecovered_paths:
                raise RuntimeError(
                    f"Stage 2.4 chunk {chunk_idx + 1} targeted FILE repair "
                    "did not recover: "
                    + ", ".join(repair.unrecovered_paths)
                )
            dt = time.time() - t0
            if not blocks:
                if _is_no_key_pages_response(response):
                    print(
                        f"  [chunk {chunk_idx+1}] generate OK — 0 optional "
                        "key pages selected; source page remains Stage 2.6"
                    )
                    return []
                raise RuntimeError(
                    f"Stage 2.4 chunk {chunk_idx + 1} produced 0 FILE blocks "
                    f"without the exact {_NO_KEY_PAGES_SENTINEL} sentinel.")
            tag = f" (retry #{attempt})" if attempt > 0 else ""
            print(f"  [chunk {chunk_idx+1}] generate OK{tag} — "
                      f"{concepts_n}c/{entities_n}e/{schema_candidates_n}s → "
                      f"{len(blocks)} blocks "
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
            if not isinstance(c, dict):
                raise RuntimeError(
                    "Stage 2.4 received an unvalidated concepts_found item; "
                    "re-run Stage 2.2.")
            if _is_key_concept_candidate(c):
                name = c.get("name", "")
                all_concepts.append(name)
        for e in a.get("entities_found") or []:
            if not isinstance(e, dict):
                raise RuntimeError(
                    "Stage 2.4 received an unvalidated entities_found item; "
                    "re-run Stage 2.2.")
            name = e.get("name", "")
            all_entities.append(name)
    seen_c: set[str] = set()
    unique_concepts = [x for x in all_concepts if not (x in seen_c or seen_c.add(x))]  # type: ignore[func-returns-value]
    seen_e: set[str] = set()
    unique_entities = [x for x in all_entities if not (x in seen_e or seen_e.add(x))]  # type: ignore[func-returns-value]
    return unique_concepts, unique_entities
