#!/usr/bin/env python3
"""_lint_suggest.py — structural wiki lint with a link-suggestion engine.

Faithful port of the structural half of NashSU `src/lib/lint.ts`:
orphan / broken-link / no-outlinks detection, each enriched with a suggested
fix computed by a deterministic similarity engine:

  - broken link  → closest existing page by slug/path/title similarity
                   (basename equality, substring, Levenshtein ratio).
  - orphan       → a related page that could link TO it (suggested_source).
  - no-outlinks  → a related page it could link to (suggested_target),
                   scored by shared-token overlap / √(|A|·|B|) + folder bonus.

NashSU's runStructuralLint reads the filesystem; this port takes the pages
in memory as `(short_name, content)` tuples (short_name relative to wiki/,
e.g. "concepts/alpha.md") so the engine is unit-testable without I/O. The
caller is responsible for walking wiki/ and excluding the aggregate anchor
files (this function also skips them defensively via ANCHOR_FILES).
"""
from __future__ import annotations

import math
import re

from _frontmatter import (
    TITLE_LINE_RE as _TITLE_LINE_RE,
    WIKILINK_RE as _WIKILINK_RE_SHARED,
)
import unicodedata
from dataclasses import dataclass, field

__all__ = [
    "run_structural_lint",
    "tokenize_for_suggestion",
    "levenshtein",
    "string_similarity",
    "extract_wikilinks",
    "ANCHOR_FILES",
    "AGGREGATE_FILES",
]

# Link-target UNIVERSE exclusion — NashSU runStructuralLint parity (lint.ts:161
# in NashSU: contentFiles drops only index.md + log.md). overview.md
# stays IN the universe so it remains valid
# wikilink targets AND their outbound links still count as inbound for the pages
# they reference — this is what prevents false "orphan" findings on pages that
# only the overview links to. Callers import this for their page-collection filter.
ANCHOR_FILES = {"index.md", "log.md"}

# Aggregate / structural files (≈ NashSU graph STRUCTURAL_IDS minus purpose).
# These ARE scanned (so their outlinks count toward inbound), but are EXEMPT from
# findings: never reported as orphan/broken/no-outlinks, so the headless auto-fixer
# never mutates them. Also serves as the write-guard + dedup/embedding exclusion
# set; keep cross_source_dedup.py / enrich_wikilinks_retroactive.py write-side
# literals in sync. (schema.md now lives at the project root like NashSU, so
# wiki/ scans won't see it; it stays listed here as a defensive/legacy guard.)
AGGREGATE_FILES = {"index.md", "log.md", "overview.md", "schema.md"}

BROKEN_LINK_SUGGESTION_MIN_SCORE = 0.74
RELATED_PAGE_SUGGESTION_MIN_SCORE = 0.08
SAME_FOLDER_SCORE_BONUS = 0.08
SINGLE_CJK_TOKEN_WEIGHT = 0.35
SUGGESTION_TOKEN_WINDOW = 4000
SAME_BASENAME_SCORE = 0.96
CONTAINS_TARGET_SCORE = 0.82

_WIKILINK_RE = _WIKILINK_RE_SHARED
_CJK_RE = re.compile(r"[㐀-鿿]")
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


# ── helpers ────────────────────────────────────────────────────────────────

def extract_wikilinks(content: str) -> list[str]:
    return [m.group(1).strip() for m in _WIKILINK_RE.finditer(content)]


def _get_file_name(path: str) -> str:
    return path.split("/")[-1] if "/" in path else path


def _relative_to_slug(relative_path: str) -> str:
    return re.sub(r"\.md$", "", relative_path)


def normalize_link_target(target: str) -> str:
    t = target.replace("\\", "/")
    t = re.sub(r"^wiki/", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\.md$", "", t, flags=re.IGNORECASE)
    return t.strip().lower()


def _extract_title(content: str, fallback_path: str) -> str:
    fm = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if fm:
        title = _TITLE_LINE_RE.search(fm.group(1))
        if title and title.group(1).strip():
            return title.group(1).strip()
    heading = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    if heading and heading.group(1).strip():
        return heading.group(1).strip()
    stem = re.sub(r"\.md$", "", _get_file_name(fallback_path), flags=re.IGNORECASE)
    return re.sub(r"[-_]+", " ", stem)


def tokenize_for_suggestion(text: str) -> set[str]:
    tokens: set[str] = set()
    normalized = unicodedata.normalize("NFKC", text).lower()
    for m in _TOKEN_RE.finditer(normalized):
        token = m.group(0)
        if len(token) >= 2:
            tokens.add(token)
        if _CJK_RE.search(token):
            for ch in token:
                tokens.add(ch)
    return tokens


def levenshtein(a: str, b: str, max_dist: int | None = None) -> int:
    """Levenshtein edit distance.

    When ``max_dist`` is given, the DP early-aborts as soon as an entire row's
    minimum exceeds it, returning ``max_dist + 1`` (a "> max_dist" sentinel).
    Callers that only care whether the distance is within a budget (the link
    suggester) get a large speedup on dissimilar pairs with no change to any
    in-budget result. The inner step avoids the 3-arg ``min()`` builtin (a
    measured hot spot) and rows are swapped instead of copied.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    current = [0] * (len(b) + 1)
    lb = len(b)
    for i in range(1, len(a) + 1):
        current[0] = i
        row_min = i
        ai = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ai == b[j - 1] else 1
            d = current[j - 1] + 1
            u = previous[j] + 1
            if u < d:
                d = u
            dl = previous[j - 1] + cost
            if dl < d:
                d = dl
            current[j] = d
            if d < row_min:
                row_min = d
        if max_dist is not None and row_min > max_dist:
            return max_dist + 1
        previous, current = current, previous
    return previous[lb]


def string_similarity(a: str, b: str, min_score: float = 0.0) -> float:
    left = normalize_link_target(a)
    right = normalize_link_target(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    left_base = _get_file_name(left)
    right_base = _get_file_name(right)
    if left_base == right_base:
        return SAME_BASENAME_SCORE
    if right in left or left in right:
        return CONTAINS_TARGET_SCORE
    if len(left_base) < 5 or len(right_base) < 5:
        return 0.0
    max_len = max(len(left_base), len(right_base))
    if max_len == 0:
        return 0.0
    if min_score > 0.0:
        # ratio = 1 - lev/max_len >= min_score  <=>  lev <= (1-min_score)*max_len.
        # Cheap length prune first (lev >= |Δlen|), then a bounded Levenshtein
        # that early-aborts past the budget. Pure speedup: result-identical for
        # any score >= min_score (all the broken-link suggester acts on). Default
        # min_score=0 keeps exact behavior for other callers / exact-ratio tests.
        budget = int((1 - min_score) * max_len)
        if abs(len(left_base) - len(right_base)) > budget:
            return 0.0
        dist = levenshtein(left_base, right_base, max_dist=budget)
        if dist > budget:
            return 0.0
        return 1 - dist / max_len
    return 1 - levenshtein(left_base, right_base) / max_len


# ── page model ─────────────────────────────────────────────────────────────

@dataclass
class _PageData:
    path: str            # short_name, relative to wiki/ (e.g. "concepts/alpha.md")
    short_name: str
    slug: str            # short_name without .md
    title: str
    content: str
    outlinks: list[str] = field(default_factory=list)
    tokens: set[str] = field(default_factory=set)


def _build_slug_map(pages: list[_PageData]) -> dict[str, str]:
    m: dict[str, str] = {}
    for p in pages:
        m[p.slug.lower()] = p.short_name
        m[re.sub(r"\.md$", "", _get_file_name(p.short_name)).lower()] = p.short_name
    return m


# ── structural lint ─────────────────────────────────────────────────────────

def run_structural_lint(pages: list[tuple[str, str]], with_suggestions: bool = True) -> list[dict]:
    """Run structural lint over in-memory pages.

    pages: list of (short_name, content), short_name relative to wiki/.
    Returns a list of finding dicts:
        {type, severity, page, detail,
         broken_target?, suggested_target?, suggested_source?}

    with_suggestions=False skips the O(n^2) suggestion engine
    (suggest_related_page / suggest_broken_target) and the per-page
    tokenization that feeds it — detection (broken-link / orphan /
    no-outlinks) still runs in O(n). Used by validate_ingest.py over the
    whole wiki; the suggestion scan is left to the dedicated wiki-lint.sh.
    """
    content_pages = [
        (name, content)
        for name, content in pages
        if _get_file_name(name) not in ANCHOR_FILES
    ]

    data: list[_PageData] = []
    for short_name, content in content_pages:
        slug = _relative_to_slug(short_name)
        title = _extract_title(content, short_name)
        outlinks = extract_wikilinks(content)
        slug_name = _get_file_name(slug)
        tokens = (
            tokenize_for_suggestion(
                f"{title}\n{slug_name}\n{content[:SUGGESTION_TOKEN_WINDOW]}"
            ) if with_suggestions else set()
        )
        data.append(_PageData(short_name, short_name, slug, title, content, outlinks, tokens))

    slug_map = _build_slug_map(data)

    def suggest_broken_target(target: str) -> "tuple[_PageData, float] | None":
        # Returns (page, score) — the score is persisted on the finding
        # (suggested_score, 2026-07-10) so the headless fixer can gate
        # auto-rewrites by confidence tier. NashSU never needs the score
        # persisted because its Fix is human-clicked per item.
        # Fast path: strip surrounding quotes that leak from YAML-formatted
        # related fields (e.g. [[concepts/foo"]] or [["concepts/foo"]]).
        # Try the clean version as an exact slug lookup before fuzzy scoring —
        # otherwise the fuzzy scorer can pick a shorter slug that merely
        # *contains* the clean target (CONTAINS_TARGET_SCORE = 0.82).
        clean = target.strip().strip('"').strip("'")
        if clean != target:
            clean_norm = normalize_link_target(clean)
            if clean_norm in slug_map:
                clean_short = slug_map[clean_norm]
                page = next((p for p in data if p.short_name == clean_short), None)
                return (page, 1.0) if page else None
            # Also try basename-only lookup (for targets like "concepts/ieee").
            clean_base = re.sub(r"\.md$", "", _get_file_name(clean)).lower()
            if clean_base in slug_map:
                clean_short = slug_map[clean_base]
                page = next((p for p in data if p.short_name == clean_short), None)
                return (page, 1.0) if page else None

        _MIN = BROKEN_LINK_SUGGESTION_MIN_SCORE
        best: tuple[_PageData, float] | None = None
        best_ties = 0  # how many candidates share the current top score
        for candidate in data:
            score = max(
                string_similarity(target, candidate.slug, _MIN),
                string_similarity(target, candidate.short_name, _MIN),
                string_similarity(target, candidate.title, _MIN),
            )
            if best is None or score > best[1]:
                best = (candidate, score)
                best_ties = 1
            elif abs(score - best[1]) <= 1e-9:
                best_ties += 1
        if best and best[1] >= _MIN:
            # Headless-apply safety (port-only; NashSU's fix is human-gated, so
            # an arbitrary tie-winner is always vetted before any write). When the
            # win came from the FUZZY tier (contains/Levenshtein, i.e.
            # score <= CONTAINS_TARGET_SCORE) and more than one page ties the top
            # score, the pick is arbitrary — e.g. [[sources/book/Microwave and RF
            # Design]] is a substring of all 5 volume pages (0.82 tie). Refuse to
            # suggest so --fix-links routes to a disambiguation stub instead of
            # silently rewriting to a wrong target. Exact (1.0) and basename (0.96)
            # wins are above CONTAINS_TARGET_SCORE and stay unaffected. (Ties are
            # counted in the single scan above — no second O(n) pass.)
            if best[1] <= CONTAINS_TARGET_SCORE and best_ties > 1:
                return None
            return best
        return None

    def suggest_related_page(page: _PageData, direction: str) -> _PageData | None:
        existing_outlinks = {normalize_link_target(o) for o in page.outlinks}
        best: tuple[_PageData, float] | None = None
        for candidate in data:
            if candidate.short_name == page.short_name:
                continue
            # Never suggest an aggregate file as a link source/target — the
            # headless auto-fixer would then append a [[wikilink]] INTO a
            # generated aggregate (overview.md/schema.md), violating the
            # AGGREGATE_FILES write-guard that exempts them from findings.
            if _get_file_name(candidate.short_name) in AGGREGATE_FILES:
                continue
            if direction == "target":
                candidate_keys = [
                    normalize_link_target(candidate.slug),
                    normalize_link_target(candidate.short_name),
                    normalize_link_target(
                        re.sub(r"\.md$", "", _get_file_name(candidate.short_name), flags=re.IGNORECASE)
                    ),
                ]
                if any(k in existing_outlinks for k in candidate_keys):
                    continue
            overlap = 0.0
            for token in page.tokens:
                if token in candidate.tokens:
                    overlap += 1 if len(token) > 1 else SINGLE_CJK_TOKEN_WEIGHT
            if overlap == 0:
                continue
            folder_bonus = (
                SAME_FOLDER_SCORE_BONUS
                if page.short_name.split("/")[0] == candidate.short_name.split("/")[0]
                else 0
            )
            score = overlap / math.sqrt(
                max(1, len(page.tokens)) * max(1, len(candidate.tokens))
            ) + folder_bonus
            if best is None or score > best[1]:
                best = (candidate, score)
        if best and best[1] >= RELATED_PAGE_SUGGESTION_MIN_SCORE:
            return best[0]
        return None

    # Inbound link counts (case-insensitive slug resolution).
    inbound_counts: dict[str, int] = {}
    for p in data:
        for link in p.outlinks:
            lookup = link.lower()
            if lookup in slug_map:
                target = _relative_to_slug(slug_map[lookup]).lower()
            else:
                target = lookup
            inbound_counts[target] = inbound_counts.get(target, 0) + 1

    # Memoize broken-target suggestions: suggest_broken_target depends only on
    # the target string and the (fixed) candidate set, so the same broken link
    # repeated across many pages is scanned once. On a wiki with lots of dangling
    # links this is a big win (e.g. 1856 broken links → 773 distinct targets).
    _broken_cache: dict[str, "tuple[_PageData, float] | None"] = {}

    def _cached_broken_target(target: str) -> "tuple[_PageData, float] | None":
        key = target.lower()
        if key not in _broken_cache:
            _broken_cache[key] = suggest_broken_target(target)
        return _broken_cache[key]

    results: list[dict] = []
    for p in data:
        short_name = p.short_name

        # Aggregate files are scanned above (their outlinks count toward inbound),
        # but are exempt from findings so the headless fixer never mutates them.
        if _get_file_name(short_name) in AGGREGATE_FILES:
            continue

        # Orphan: no inbound links.
        if inbound_counts.get(p.slug.lower(), 0) == 0:
            suggested_source = suggest_related_page(p, "source") if with_suggestions else None
            results.append({
                "type": "orphan",
                "severity": "info",
                "page": short_name,
                "detail": "No other pages link to this page.",
                "suggested_source": suggested_source.short_name if suggested_source else None,
            })

        # No outbound links.
        if len(p.outlinks) == 0:
            suggested_target = suggest_related_page(p, "target") if with_suggestions else None
            results.append({
                "type": "no-outlinks",
                "severity": "info",
                "page": short_name,
                "detail": "This page has no [[wikilink]] references to other pages.",
                "suggested_target": suggested_target.short_name if suggested_target else None,
            })

        # Broken links.
        for link in p.outlinks:
            lookup = link.lower()
            basename = re.sub(r"\.md$", "", _get_file_name(link)).lower()
            if lookup in slug_map or basename in slug_map:
                continue
            suggestion = _cached_broken_target(link) if with_suggestions else None
            results.append({
                "type": "broken-link",
                "severity": "warning",
                "page": short_name,
                "detail": f"Broken link: [[{link}]] — target page not found.",
                "broken_target": link,
                "suggested_target": suggestion[0].short_name if suggestion else None,
                # improved-wiki extension (2026-07-10): the suggestion's
                # similarity score, persisted so wiki-lint-fix.py can gate
                # headless auto-rewrites (>=0.9 auto, below -> review).
                "suggested_score": round(suggestion[1], 4) if suggestion else None,
            })

    return results
