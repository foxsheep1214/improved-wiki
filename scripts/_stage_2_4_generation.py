from __future__ import annotations

import json
import re
import time
from pathlib import Path

from _config import Config
from _core import (
    canonical_source_path,
    is_query_bridge_source,
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

# Soft cap on the displayed Linkable-pages list. Must-link candidate and
# association targets are always kept; only the background fill is bounded.
_LINKABLE_TOTAL_CAP = 400

# Stage 2.4 always emits the source page; this sentinel lets the model state
# that no optional key/typed page qualifies after that mandatory FILE block.
_NO_KEY_PAGES_SENTINEL = "NO_KEY_PAGES"


def _stage_2_4_generation_max_tokens(config: Config) -> int:
    """NashSU 0.6.6 generation-token ladder for the single final call."""
    context_size = int(getattr(config, "context_size", 0) or 0)
    if context_size >= 512_000:
        return 32_768
    if context_size >= 256_000:
        return 24_576
    if context_size >= 128_000:
        return 16_384
    if context_size > 0:
        return 8_192
    # Minimal test/programmatic configs may not have run the live context
    # probe. Preserve their configured compatibility behavior.
    return config.compute_max_tokens(16_384)


def _is_key_concept_candidate(item: dict) -> bool:
    """Whether a Stage 2.2 concept is eligible for standalone generation.

    `mentioned` is retained only as optional analysis context. NashSU generates
    pages for key ideas identified in analysis, not for every term encountered.
    Missing importance defaults to eligible for backward-compatible checkpoints.
    """
    return str(item.get("importance", "core")).strip().lower() != "mentioned"


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
    pages vanish first as the wiki grows. Instead, score
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

# ── Audit 2026-07-02 三/B additions to the consolidated generation prompt ──

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
# 11-13 = NashSU subject-attribution rules from ingest.ts:2218-2220.
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


def source_page_rel_stem(file_path: Path, config: Config) -> str:
    """Raw-relative stem of this source: ``Book/Some Book - 2020 - Author``.

    The source page lives at ``wiki/sources/<stem>.md`` and its ``sources:``
    entry is ``raw/<stem><ext>``, so both the write path and the frontmatter
    are derived from this one value.
    """
    try:
        return str(file_path.relative_to(config.raw_root).with_suffix(""))
    except ValueError:
        return file_path.stem


def _source_page_slug(file_path: Path, config: Config) -> str:
    """Wikilink stem of this source's page: sources/<raw-rel-sans-ext>."""
    return f"sources/{source_page_rel_stem(file_path, config)}"


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


def _final_digest_from_analyses(chunk_analyses: list[dict]) -> dict:
    """Final rolling digest = the last chunk's ``updated_global_digest``.

    Stage 2.2 rolls the digest forward chunk by chunk, so the last analysis
    carries the whole-source view. Used only to pre-fill the source page's
    bibliographic frontmatter; the full digest is already inside the
    consolidated context.
    """
    for analysis in reversed(chunk_analyses or []):
        if not isinstance(analysis, dict):
            continue
        digest = analysis.get("updated_global_digest")
        if isinstance(digest, dict) and digest:
            return digest
    return {}


def _source_bibliographic_fields(global_digest: dict, stem: str) -> dict:
    """Pre-filled `authors/year/url/venue` YAML for the source page.

    A type-specific ``*_meta`` block (paper_meta, ...)
    overrides ``book_meta``, a bare DOI becomes a URL, and a book's
    ``publisher`` folds into ``venue`` (NashSU has no publisher field).
    """
    global_digest = global_digest if isinstance(global_digest, dict) else {}
    book_meta = global_digest.get("book_meta") or {}
    if not isinstance(book_meta, dict):
        book_meta = {}
    bib_meta = dict(book_meta)
    specific_meta = next(
        (
            v for k, v in global_digest.items()
            if k != "book_meta" and k.endswith("_meta") and isinstance(v, dict)
        ),
        {},
    )
    for key, value in specific_meta.items():
        if value not in ("", None, [], {}):
            bib_meta[key] = value

    authors = bib_meta.get("authors", [])
    if not isinstance(authors, list):
        authors = [authors] if authors else []
    year = bib_meta.get("year", "")
    url = bib_meta.get("url", "")
    doi = str(bib_meta.get("doi", "") or "").strip()
    if not url and doi:
        url = doi if doi.startswith(("http://", "https://")) else (
            "https://doi.org/" + re.sub(r"^doi:\s*", "", doi, flags=re.I))
    venue = bib_meta.get("venue", "") or bib_meta.get("publisher", "")

    return {
        "title": bib_meta.get("title") or book_meta.get("title") or stem,
        "authors": "[" + ", ".join(f'"{a}"' for a in authors) + "]" if authors else "[]",
        "year": str(year) if year not in ("", None) else '""',
        "url": f'"{url}"' if url else '""',
        "venue": f'"{venue}"' if venue else '""',
    }


def _source_page_guidance_section(stem: str) -> str:
    """Describe the mandatory source-summary block."""
    return f"""# MANDATORY Source Page — wiki/sources/{stem}.md
Exactly one source summary page is REQUIRED in this response, at that exact
path. It is not a candidate and is never optional.

Write a concise, grounded source summary in the source's
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
relevant pages you are generating in this same response, or existing wiki pages
from the linkable list above."""


def _source_page_output_section(stem: str, suffix: str, global_digest: dict) -> str:
    """Exact FILE block template for the mandatory source page."""
    bib = _source_bibliographic_fields(global_digest, stem)
    today = time.strftime("%Y-%m-%d")
    return f"""---FILE:wiki/sources/{stem}.md---
---
type: source
title: "{bib['title']}"
created: {today}
updated: {today}
tags: [tag1, tag2, tag3]
related: []
sources: ["raw/{stem}{suffix}"]
authors: {bib['authors']}
year: {bib['year']}
url: {bib['url']}
venue: {bib['venue']}
---

(source summary body; choose the useful structure described above)

---END FILE---
Source-page frontmatter notes: authors is a list, year a number, url/venue
strings. The values above are pre-filled from the digest where available —
verify them and complete any left empty (`[]` for authors, `""` for url/venue
when genuinely unknown). Evidence anchors cite chapter/section/equation/figure
numbers (式(5-10), 图2.6, Table 8.1); a value read off a figure's curve is
marked "据图X.X"."""


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


def _stage_2_4_build_all_prompt(
    chunk_analyses: list[dict],
    file_path: Path,
    config: Config,
    template: str = "",
    existing_refs: dict | None = None,
    related_pages: list[dict] | None = None,
    source_context: str = "",
    consolidated_context: str = "",
) -> str:
    """Build ONE generation prompt covering ALL chunks (NashSU single-shot parity).

    Aggregates key concepts/entities and schema-typed candidates across every
    chunk analysis, dedups by slug, and asks the LLM to emit FILE blocks for
    all of them in a single response.
    Replaces the former per-chunk generation loop (Stage 2.4 × N calls → 1 call).

    ``consolidated_context`` is the active ingest path: the final rolling digest,
    every chunk analysis, and bounded raw evidence from every chunk. The legacy
    ``source_context`` argument remains for direct callers and tests.
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

    context_text = consolidated_context or source_context
    if context_text.strip():
        context_label = (
            "Consolidated Stage 2 Context"
            if consolidated_context
            else "Source Context"
        )
        source_section = (
            f"\n# {context_label} "
            "(GROUND EVERY PAGE IN THIS — do not write from memory)\n"
            "The context includes the final digest, all chunk analyses, and "
            "bounded raw evidence. For every page:\n"
            "- Use the SOURCE'S OWN definitions, formulas, notation, variable names,\n"
            "  and worked examples — NOT the popular/textbook version from your memory.\n"
            "- If the source frames a concept a specific way (e.g. a particular formula\n"
            "  or set of variables), reproduce THAT framing; do not substitute a\n"
            "  generic equivalent.\n"
            "- Prefer the source's concrete numbers/examples over invented ones.\n"
            "- Use cross-chunk evidence to keep synthesis, thesis, comparison, and\n"
            "  terminology coherent across the whole source.\n"
            "<stage2-context>\n"
            f"{context_text}\n"
            "</stage2-context>\n"
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
    # NashSU parity: the source summary is one more FILE block of this call,
    # not a second LLM round-trip.
    # A deep-research query bridge (wiki/queries/<slug>.md) is NOT a real
    # source document: the query page itself is the artifact, so it must never
    # be asked for — nor given — a wiki/sources/ digest page (see
    # is_query_bridge_source). Before the Stage 2.6 merge this was enforced by
    # skipping the whole call; the prompt must carry the same condition, or the
    # model dutifully emits a source block that reaches file_blocks before the
    # post-write skip can apply (regression 2026-08-01: four bogus
    # wiki/sources/research-*.md pages on RadarWiki, each citing a
    # raw/research-*.md that does not exist because source_page_rel_stem falls
    # back to the bare stem outside raw_root).
    if is_query_bridge_source(file_path, config):
        source_page_section = ""
        source_page_output_section = ""
        _no_pages_clause = (
            f"output exactly `{_NO_KEY_PAGES_SENTINEL}` and nothing else.")
        _first_line_rule = (
            "- If at least one candidate qualifies, your FIRST line MUST start "
            "with `---FILE:wiki/`.\n"
            f"- Otherwise output exactly `{_NO_KEY_PAGES_SENTINEL}`.")
        _closing_rule = (
            "Generate only qualifying new and UPDATE EXISTING key pages that "
            "are not marked\n[ALREADY COVERED]/[SKIP]/CROSS-TYPE, in one "
            "response. Start with the first FILE\nblock, or output exactly "
            f"`{_NO_KEY_PAGES_SENTINEL}` if none qualifies. This source is a "
            "deep-research\nquery page — do NOT emit any wiki/sources/ page "
            "for it.")
    else:
        source_rel_stem = source_page_rel_stem(file_path, config)
        _source_digest = _final_digest_from_analyses(chunk_analyses)
        source_page_section = _source_page_guidance_section(source_rel_stem)
        source_page_output_section = _source_page_output_section(
            source_rel_stem, file_path.suffix, _source_digest)
        _no_pages_clause = (
            "emit ONLY the mandatory source page below, then the exact\n"
            f"  line `{_NO_KEY_PAGES_SENTINEL}` and nothing else.")
        _first_line_rule = (
            "- Your FIRST line MUST start with `---FILE:wiki/`. The source "
            "page is mandatory,\n  so there is always at least one FILE "
            "block.\n- If no key/schema-typed candidate qualifies, emit the "
            f"source page block and\n  then the exact line "
            f"`{_NO_KEY_PAGES_SENTINEL}`.")
        _closing_rule = (
            "Generate the MANDATORY source page plus every qualifying "
            "new/UPDATE EXISTING key\npage not marked [ALREADY "
            "COVERED]/[SKIP]/CROSS-TYPE, in one response. Start with\nthe "
            f"first FILE block. `{_NO_KEY_PAGES_SENTINEL}` means \"no "
            "key/schema-typed page\nqualifies\" — it NEVER excuses omitting "
            "the source page.")

    language_sample = context_text or json.dumps(chunk_analyses, ensure_ascii=False)
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
  material update, {_no_pages_clause}

{source_page_section}

# ⚠️ CRITICAL — START IMMEDIATELY WITH THE RESULT
{_first_line_rule}
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
5. In a Markdown table cell, escape an alias separator as
   `[[target\\|display]]`; an unescaped `|` creates a false cell boundary.
6. NEVER use `/` in filenames (macOS rejects it). Use "-" instead.
7. Math: ALWAYS write formulas in LaTeX — inline $...$, display $$...$$. Transcribe
   each formula from the source / Formulas list verbatim (same variables, same form);
   never paraphrase a formula into prose or swap in a generic textbook version.
8. Result-file integrity: every LaTeX command must retain a literal reverse-solidus
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
{source_page_output_section}

{_closing_rule}

{language_directive}
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
    consolidated_context: str = "",
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
        consolidated_context=consolidated_context,
    )
    gen_tokens = _stage_2_4_generation_max_tokens(config)

    for attempt in range(4):
        try:
            t0 = time.time()
            if attempt == 0:
                print("  [generate-all] single-shot generating (all chunks)...", flush=True)
            response, stop_reason = call_anthropic_protocol(
                prompt, config, max_tokens=gen_tokens)
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
                raise RuntimeError(
                    "Stage 2.4 produced 0 FILE blocks; the mandatory source "
                    "page cannot be omitted.")
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
