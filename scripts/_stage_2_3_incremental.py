"""Stage 2.3: Incremental Association Detection

Detects overlap between a new source's concepts/entities/schema-typed
candidates and existing wiki pages, so downstream stages can update same-type
pages while avoiding cross-type duplicates. Deterministic: word-level title
Jaccard + exact slug match. ``stage_2_3_semantic_candidates`` adds vector
nominations for the candidates this lexical pass leaves unmatched; Stage 2.4's
model judges them.
"""
from pathlib import Path
import re

from _core import slugify
from _schema import schema_candidate_routes
from _stage_2_base import (
    _stage_2_frontmatter_title,
    _stage_2_title_words,
    _stage_2_title_cjk_bigrams,
)

# Cross-domain acronym guard: shared tokens no longer than this are treated as
# bare acronyms ("ram", "mti") rather than full words.
_STAGE_2_3_ACRONYM_MAX_LEN = 4
# Same-route associations are destructive update targets downstream, so an
# ASCII fuzzy-title match must be substantially closer than a mere majority.
# A 0.8 floor still accepts spelling/order variants with the same content
# words, while rejecting live collisions such as "Transmission Line RLGC
# Model" vs "Patch Transmission Line Model" (3/5), "Transmission Line Wave
# Parameters" vs "Transmission Line Loss Parameters" (3/5), and "Lange
# Coupler" vs "Unfolded Lange Coupler" (2/3).  Exact slug matches remain
# unconditional.  CJK bigrams keep their separate >0.5 threshold because one
# suffix character commonly adds two bigrams to an otherwise identical term.
_STAGE_2_3_ASCII_UPDATE_JACCARD = 0.8
# Semantic nominations (see stage_2_3_semantic_candidates). The floor keeps
# about three quarters of known duplicate partners (cosine quartiles
# 0.65/0.68/0.73 on HardwareWiki's unmerged dedup pairs); two nominations
# per candidate bound the prompt.
_STAGE_2_3_SEMANTIC_MIN_COSINE = 0.65
_STAGE_2_3_SEMANTIC_MAX_NOMINATIONS = 2
_STAGE_2_3_CJK_RE = re.compile("[\\u3400-\\u4dbf\\u4e00-\\u9fff]")


def _stage_2_3_acronym_only_mismatch(name: str, slug: str, shared_tokens: set) -> bool:
    """True when a title-Jaccard match rests solely on short ASCII tokens
    (<=4 chars, i.e. bare acronyms) while the two names carry disjoint CJK
    parts — a cross-domain acronym collision, not a real association.

    Live failure (2026-07-02, 《直升机多普勒导航雷达原理》): _stage_2_title_words
    strips CJK characters entirely, so "RAM 片选信号软件控制" (computer memory)
    and the existing page 雷达吸波材料-ram (radar absorbing material) both
    tokenized to {"ram"} → Jaccard 1.0 → the new concept was flagged ALREADY
    COVERED and generation linked memory pages to the radar page. Exact
    slug-form matches, matches carrying at least one longer shared token, and
    names without CJK on both sides are unaffected.
    """
    if not shared_tokens:
        return False
    if any(len(tok) > _STAGE_2_3_ACRONYM_MAX_LEN for tok in shared_tokens):
        return False
    name_cjk = set(_STAGE_2_3_CJK_RE.findall(name))
    slug_cjk = set(_STAGE_2_3_CJK_RE.findall(slug))
    return bool(name_cjk) and bool(slug_cjk) and not (name_cjk & slug_cjk)


_STAGE_2_3_ALNUM_RUN_RE = re.compile(r"[a-z0-9]+")


def _stage_2_3_initials(text: str) -> set:
    """Single-letter alphanumeric segments of a name/slug — person initials."""
    return {seg for seg in _STAGE_2_3_ALNUM_RUN_RE.findall(text.lower())
            if len(seg) == 1}


def _stage_2_3_initials_mismatch(name: str, slug: str) -> bool:
    """True when both sides carry single-letter initials and the sets are
    disjoint — a different-person collision on a shared surname.

    Live failure (2026-07-09, Phased Array Antennas re-ingest):
    _stage_2_title_words drops single-letter tokens, so "W. W. Hansen" (the
    1938 Hansen-Woodyard co-originator) and the existing page "J. P. Hansen"
    (an NRL sea-clutter researcher from the Skolnik handbook) both tokenized
    to {hansen} → Jaccard 1.0 → ALREADY COVERED → generation wikilinked the
    wrong person from the Hansen-Woodyard concept page. Same guard shape as
    _stage_2_3_acronym_only_mismatch. One side having NO initials (bare
    surname) is left alone — that ambiguity needs semantics, not initials.
    """
    a = _stage_2_3_initials(name)
    b = _stage_2_3_initials(slug)
    return bool(a) and bool(b) and not (a & b)


def _stage_2_3_bare_surname_mismatch(name: str, existing_title: str) -> bool:
    """True when the EXISTING page's title is a bare single-word surname
    (zero disambiguating tokens) while the NEW name is strictly more
    specific (multiple parts, at least one single-letter initial).

    A bare-surname page provides no evidence that it represents the same
    specific person as a fully-initialed new name. The guard is intentionally
    one-directional: a bare new name against an initialed existing page, or
    bare-vs-bare, still requires semantic judgment.
    """
    existing_words = _stage_2_title_words(existing_title)
    if len(existing_words) != 1:
        return False  # existing title has its own disambiguating detail
    if _stage_2_3_initials(existing_title):
        # Existing title DOES carry initials (e.g. "T. T. Taylor") — it just
        # ALSO reduces to one word under _stage_2_title_words (which strips
        # single-letter tokens same as it does for the new name). Not bare.
        return False
    return bool(_stage_2_3_initials(name))


def _stage_2_3_existing_pages(
    wiki_root: Path,
    routes: list[str],
) -> dict[str, tuple[str, set, set, str]]:
    """Load title-match metadata for the requested wiki routes.

    Targets are always type-prefixed. Stage 2.4 needs the real route to
    distinguish a same-type page that should be updated from a cross-type
    association that should only be linked. Bare stems erased that distinction
    for generic concepts/entities and made existing-page updates impossible.
    """
    existing: dict[str, tuple[str, set, set, str]] = {}
    for route in routes:
        page_dir = wiki_root / route
        if not page_dir.is_dir():
            continue
        for f in page_dir.glob("*.md"):
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
                title = _stage_2_frontmatter_title(content)
                if not title:
                    continue
                target = f"{route}/{f.stem}"
                existing[target] = (
                    f.stem,
                    _stage_2_title_words(title),
                    _stage_2_title_cjk_bigrams(title),
                    title,
                )
            except Exception as e:
                print(f"[2.3] warn: skip {f}: {type(e).__name__}: {e}")
    return existing


def _stage_2_3_matching_targets(
    name: str,
    existing: dict[str, tuple[str, set, set, str]],
    *,
    canonical_slug: bool,
) -> list[str]:
    """Return deterministic exact/fuzzy matches from one allowed page scope."""
    name_words = _stage_2_title_words(name)
    name_cjk = _stage_2_title_cjk_bigrams(name)
    slug_form = slugify(name) if canonical_slug else name.lower().replace(" ", "-")
    matches: list[str] = []
    for target, (stem, words, cjk, title) in existing.items():
        # Exact slug-form match first: a pure-CJK title tokenizes to an empty
        # ASCII word set. Only the Jaccard branch needs non-empty words.
        if slug_form == stem.lower():
            matches.append(target)
        elif (words and name_words
              and len(name_words & words) / len(name_words | words)
              >= _STAGE_2_3_ASCII_UPDATE_JACCARD
              and not _stage_2_3_acronym_only_mismatch(
                  name, stem, name_words & words)
              and not _stage_2_3_initials_mismatch(name, stem)
              and not _stage_2_3_bare_surname_mismatch(name, title)):
            matches.append(target)
        # CJK bigram Jaccard is separate so mixed titles do not dilute either
        # side. The acronym guard remains symmetric with the ASCII branch.
        elif (cjk and name_cjk
              and len(name_cjk & cjk) / len(name_cjk | cjk) > 0.5
              and not _stage_2_3_acronym_only_mismatch(
                  name, stem, name_cjk & cjk)):
            matches.append(target)
    return matches


def stage_2_3_detect_incremental_associations(
    wiki_root: Path,
    chunk_analyses: list[dict],
    schema_text: str = "",
) -> dict:
    """Match new candidates while preserving the existing page's real route.

    A schema-typed candidate takes precedence over a same-name generic
    concept/entity and is compared only with its declared route. Thus an
    existing ``concepts/foo`` cannot incorrectly suppress a new
    ``findings/foo`` page, while an existing ``findings/foo`` can. Generic
    concept/entity candidates scan both base routes: same-route matches become
    update targets and cross-route matches remain link-only associations.
    """
    associations: dict[str, list[str]] = {}
    generic_found: set[str] = set()
    typed_found: dict[str, str] = {}
    typed_routes = schema_candidate_routes(schema_text)
    for chunk in chunk_analyses:
        for concept in chunk.get("concepts_found", []):
            name = concept.get("name", "").strip() if isinstance(concept, dict) else str(concept).strip()
            if name:
                generic_found.add(name)
        for ent in chunk.get("entities_found", []):
            name = ent.get("name", "").strip() if isinstance(ent, dict) else str(ent).strip()
            if name:
                generic_found.add(name)
        for candidate in chunk.get("schema_typed_candidates", []) or []:
            if not isinstance(candidate, dict):
                continue
            name = str(candidate.get("name", "")).strip()
            route = typed_routes.get(str(candidate.get("type", "")).strip())
            if name and route:
                # Match Stage 2.4's first-candidate-wins behavior for duplicate
                # names/types across chunks.
                typed_found.setdefault(name, route)

    generic_existing = _stage_2_3_existing_pages(
        wiki_root, ["concepts", "entities"])
    for name in generic_found - typed_found.keys():
        matches = _stage_2_3_matching_targets(
            name, generic_existing, canonical_slug=False)
        if matches:
            associations[name] = matches

    typed_existing_by_route: dict[str, dict] = {}
    for name, route in typed_found.items():
        if route not in typed_existing_by_route:
            typed_existing_by_route[route] = _stage_2_3_existing_pages(
                wiki_root, [route])
        existing = typed_existing_by_route[route]
        matches = _stage_2_3_matching_targets(
            name, existing, canonical_slug=True)
        if matches:
            associations[name] = matches
    return associations


def _stage_2_3_semantic_queries(
    chunk_analyses: list[dict],
    associations: dict,
    schema_text: str,
) -> list[tuple[str, str, str]]:
    """(name, route, "name: description") for each candidate Stage 2.4 would
    list as a new page and the lexical pass did not associate."""
    from _stage_2_4_generation import _is_key_concept_candidate

    typed_routes = schema_candidate_routes(schema_text)
    typed_names = {
        str(c.get("name", "")).strip()
        for chunk in chunk_analyses if isinstance(chunk, dict)
        for c in chunk.get("schema_typed_candidates", []) or []
        if isinstance(c, dict)
    }
    queries: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def add(name: str, route: str | None, description: str) -> None:
        if name and route and name not in seen and name not in associations:
            seen.add(name)
            queries.append((name, route, f"{name}: {description}".strip()))

    for chunk in chunk_analyses:
        if not isinstance(chunk, dict) or "error" in chunk:
            continue
        for c in chunk.get("concepts_found", []) or []:
            if isinstance(c, dict) and _is_key_concept_candidate(c):
                name = str(c.get("name", "")).strip()
                if name not in typed_names:
                    add(name, "concepts", str(c.get("definition", "")))
        for e in chunk.get("entities_found", []) or []:
            if isinstance(e, dict):
                name = str(e.get("name", "")).strip()
                if name not in typed_names:
                    add(name, "entities", str(e.get("significance", "")))
        for c in chunk.get("schema_typed_candidates", []) or []:
            if isinstance(c, dict):
                add(str(c.get("name", "")).strip(),
                    typed_routes.get(str(c.get("type", "")).strip()),
                    str(c.get("rationale", "")))
    return queries


def stage_2_3_semantic_candidates(
    config,
    chunk_analyses: list[dict],
    associations: dict,
    schema_text: str = "",
) -> dict[str, list[dict]]:
    """Existing same-route pages close in meaning to an unmatched candidate.

    The lexical pass only sees titles, so a paraphrase or another language
    ("降压变换器" vs "Buck Converter") passes as new and is merged much later
    by cross-source dedup. Each unmatched candidate's "name: description" is
    embedded and compared with the first chunk (title + opening section) of
    pages in the candidate's route in the vector index Stage 3.7 maintains.

    These are nominations, not associations. On HardwareWiki's 69 unmerged
    dedup pairs the partner page ranked first only 52 % of the time (top-3:
    71 %), and its cosine (median 0.68) overlapped with the nearest unrelated
    page's (median 0.66), so no threshold can decide identity. Stage 2.4
    shows them as POSSIBLY ALREADY EXISTS and the model decides: same subject
    → update the existing path; merely related → new page plus a wikilink.

    No vector index yet → no nominations. An embedding failure raises: the
    ingest pauses here as it would at Stage 3.7, and resumes cleanly.
    """
    lance_dir = Path(config.runtime_dir) / "lancedb"
    if not lance_dir.is_dir():
        print("  [stage 2.3] no vector index — semantic nominations skipped")
        return {}
    queries = _stage_2_3_semantic_queries(chunk_analyses, associations, schema_text)
    if not queries:
        return {}

    import lancedb
    from build_embeddings import embed_with_config, embedding_config_from_env

    try:
        table = lancedb.connect(str(lance_dir)).open_table("wiki_chunks")
    except Exception as exc:
        print(f"  [stage 2.3] vector index unreadable ({exc}) — "
              "semantic nominations skipped")
        return {}
    vectors = embed_with_config([text for *_, text in queries],
                                embedding_config_from_env())
    nominations: dict[str, list[dict]] = {}
    for (name, route, _text), vector in zip(queries, vectors):
        frame = (table.search(vector)
                 .where(f"chunk_index = 0 AND page_id LIKE '{route}/%'",
                        prefilter=True)
                 .limit(_STAGE_2_3_SEMANTIC_MAX_NOMINATIONS)
                 .to_pandas())
        picks = []
        for row in frame.to_dict("records"):
            # unit vectors, squared L2: cosine = 1 - d/2
            cosine = round(1.0 - float(row["_distance"]) / 2.0, 2)
            if cosine >= _STAGE_2_3_SEMANTIC_MIN_COSINE:
                picks.append({"slug": str(row["page_id"]),
                              "title": str(row.get("title") or ""),
                              "cosine": cosine})
        if picks:
            nominations[name] = picks
    return nominations


def stage_2_3_resolve_proposed_connections(
    wiki_root: Path,
    chunk_analyses: list[dict],
    schema_text: str = "",
) -> list[dict]:
    """Resolve each chunk's self-reported ``connections_to_existing_wiki``
    entries against real wiki pages.

    Stage 2.2 asks the LLM to propose relationships (extends/applies/cites/
    contrasts) to existing pages, but nothing downstream ever read this field
    — it was silently discarded. This validates each proposed page actually
    exists (exact slug or title-Jaccard >=0.5, same method as
    ``stage_2_3_detect_incremental_associations``) and resolves it to a
    type-prefixed slug, so Stage 2.4 can wikilink new pages to genuinely
    related (not duplicate) existing pages instead of dropping the field.
    """
    proposed: list[tuple[str, str]] = []
    for chunk in chunk_analyses:
        for conn in chunk.get("connections_to_existing_wiki", []) or []:
            if not isinstance(conn, dict):
                continue
            page = (conn.get("existing_page") or "").strip()
            rel = (conn.get("relationship") or "related").strip()
            if page:
                proposed.append((page, rel))
    if not proposed:
        return []

    existing: dict[str, tuple[str, set]] = {}
    type_dirs = ["concepts", "entities", "sources", "queries", "comparisons"]
    for route in schema_candidate_routes(schema_text).values():
        if route not in type_dirs:
            type_dirs.append(route)
    for type_dir in type_dirs:
        page_dir = wiki_root / type_dir
        if not page_dir.is_dir():
            continue
        for f in page_dir.glob("*.md"):
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
                title = _stage_2_frontmatter_title(content)
                existing[f.stem] = (type_dir, _stage_2_title_words(title) if title else set())
            except Exception as e:
                print(f"[2.3] warn: skip {f}: {type(e).__name__}: {e}")

    resolved = []
    seen = set()
    for page, rel in proposed:
        slug_form = page.lower().replace(" ", "-")
        match = slug_form if slug_form in existing else None
        if not match:
            page_words = _stage_2_title_words(page)
            best_ratio, best_slug = 0.0, None
            for stem, (_, words) in existing.items():
                if not words or not page_words:
                    continue
                if _stage_2_3_acronym_only_mismatch(page, stem, page_words & words):
                    continue
                ratio = len(page_words & words) / len(page_words | words)
                if ratio > best_ratio:
                    best_ratio, best_slug = ratio, stem
            if best_ratio > 0.5:
                match = best_slug
        if match and match not in seen:
            seen.add(match)
            type_dir = existing[match][0]
            resolved.append({"slug": f"{type_dir}/{match}", "relationship": rel})
    return resolved
