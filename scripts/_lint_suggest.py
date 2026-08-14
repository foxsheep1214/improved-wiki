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
import unicodedata
from dataclasses import dataclass, field

import yaml

from _frontmatter import (
    TITLE_LINE_RE as _TITLE_LINE_RE,
    WIKILINK_RE as _WIKILINK_RE_SHARED,
    parse_frontmatter,
)

__all__ = [
    "run_structural_lint",
    "tokenize_for_suggestion",
    "levenshtein",
    "string_similarity",
    "extract_wikilinks",
    "ANCHOR_FILES",
    "AGGREGATE_FILES",
    "STATE_FILES",
    "BROKEN_LINK_AUTO_REWRITE_MIN_SCORE",
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

# Runtime/state filenames that must never be scanned as wiki pages. Shared
# constant (2026-07-12): wiki-lint-fix.py, validate_ingest.py,
# wiki-lint-semantic.py and wiki-lint.sh's embedded scan each carried a
# drifted local copy — this is the union of all of them. Extra names are
# harmless for any one consumer (skipping a state file that could never
# appear is a no-op), missing names are not.
STATE_FILES = {
    "lint-cache.json", "lint.json", "lint-semantic.json",
    "ingest-cache.json", "ingest-queue.json", "ingest-lock",
    "lint-lock", "lint.lock",
    "review.json", "review-suggestions.json",
    "embed-cache.json", "dedup-report.json",
}

_YAML_FRONTMATTER_RE = re.compile(
    r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)",
    re.DOTALL,
)


def _lint_frontmatter(content: str) -> dict:
    """Return YAML-aware frontmatter for structural edge semantics.

    The shared lightweight parser is retained as a fallback for legacy files,
    but PyYAML is required here so ``null`` means no redirect and inline
    comments are not folded into a redirect target.
    """
    fallback, _ = parse_frontmatter(content)
    match = _YAML_FRONTMATTER_RE.match(content)
    if not match:
        return fallback
    try:
        parsed = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return fallback
    return parsed if isinstance(parsed, dict) else fallback

# Headless auto-rewrite gate (2026-07-10, user-approved lint hardening): only
# exact (1.0) / same-basename (0.96) tier suggestions may be rewritten without
# a human — contains-tier (0.82) and fuzzy-Levenshtein suggestions go to
# review instead (string-similar is not meaning-similar; a headless batch
# multiplies one bad suggestion). Shared here (2026-07-12) so wiki-lint-fix.py
# and enrich_wikilinks_retroactive.py apply the SAME threshold.
BROKEN_LINK_AUTO_REWRITE_MIN_SCORE = 0.9

BROKEN_LINK_SUGGESTION_MIN_SCORE = 0.74
RELATED_PAGE_SUGGESTION_MIN_SCORE = 0.08
SAME_FOLDER_SCORE_BONUS = 0.08
SINGLE_CJK_TOKEN_WEIGHT = 0.35
SUGGESTION_TOKEN_WINDOW = 4000
SAME_BASENAME_SCORE = 0.96
CONTAINS_TARGET_SCORE = 0.82
MAX_SUGGESTION_CANDIDATES = 64

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


def _fragments(value: str) -> list[str]:
    """NFKC-normalized character bigrams used by NashSU 0.6.6."""
    normalized = unicodedata.normalize("NFKC", normalize_link_target(value))
    chars = list(normalized)
    if len(chars) < 2:
        return [normalized] if normalized else []
    result: list[str] = []
    seen: set[str] = set()
    for i in range(len(chars) - 1):
        fragment = chars[i] + chars[i + 1]
        if fragment not in seen:
            seen.add(fragment)
            result.append(fragment)
    return result


def _add_to_index(index: dict[str, list[int]], key: str, page_index: int) -> None:
    index.setdefault(key, []).append(page_index)


def _top_candidates(scores: dict[int, float], excluded: int) -> list[int]:
    """Return NashSU's score-desc/index-asc candidate window."""
    ranked = ((idx, score) for idx, score in scores.items() if idx != excluded)
    return [
        idx for idx, _ in
        sorted(ranked, key=lambda item: (-item[1], item[0]))[
            :MAX_SUGGESTION_CANDIDATES
        ]
    ]


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
    page_type: str = ""
    redirect_target: str = ""


def _build_slug_map(pages: list[_PageData]) -> dict[str, int]:
    """Dual-index by normalized relative slug and basename, as in 0.6.6."""
    m: dict[str, int] = {}
    for index, p in enumerate(pages):
        basename = re.sub(
            r"\.md$", "", _get_file_name(p.short_name), flags=re.IGNORECASE
        )
        m[normalize_link_target(p.slug)] = index
        m[normalize_link_target(basename)] = index
    return m


# ── structural lint ─────────────────────────────────────────────────────────

def run_structural_lint(pages: list[tuple[str, str]], with_suggestions: bool = True) -> list[dict]:
    """Run structural lint over in-memory pages.

    pages: list of (short_name, content), short_name relative to wiki/.
    Returns a list of finding dicts:
        {type, severity, page, detail,
         broken_target?, suggested_target?, suggested_source?, link_origin?}

    with_suggestions=False skips the indexed candidate/scoring engine and the
    per-page tokenization that feeds it — detection (broken-link / orphan /
    no-outlinks) still runs in O(n). Used by validate_ingest.py over the whole
    wiki; suggestion generation is left to the dedicated wiki-lint.sh.
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
        fm = _lint_frontmatter(content)
        page_type = str(fm.get("type", "")).strip().lower()
        raw_redirect = fm.get("redirect")
        redirect_target = raw_redirect.strip() if isinstance(raw_redirect, str) else ""
        if page_type == "redirect" and redirect_target:
            # A redirect's frontmatter target is a real structural edge even
            # when the compatibility page omits a prose wikilink. Keep it in
            # broken-link validation, while suppressing orphan/no-outlinks
            # noise for the redirect page itself below.
            outlinks = list(dict.fromkeys([*outlinks, redirect_target]))
        slug_name = _get_file_name(slug)
        tokens = (
            tokenize_for_suggestion(
                f"{title}\n{slug_name}\n{content[:SUGGESTION_TOKEN_WINDOW]}"
            ) if with_suggestions else set()
        )
        data.append(_PageData(
            short_name, short_name, slug, title, content, outlinks, tokens,
            page_type, redirect_target,
        ))

    slug_map = _build_slug_map(data)
    token_index: dict[str, list[int]] = {}
    fragment_index: dict[str, list[int]] = {}
    if with_suggestions:
        for page_index, page in enumerate(data):
            for token in page.tokens:
                _add_to_index(token_index, token, page_index)
            for value in (page.slug, page.short_name, page.title):
                for fragment in _fragments(value):
                    _add_to_index(fragment_index, fragment, page_index)

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
            clean_base = re.sub(
                r"\.md$", "", _get_file_name(clean), flags=re.IGNORECASE
            )
            for key in (
                normalize_link_target(clean),
                normalize_link_target(clean_base),
            ):
                page_index = slug_map.get(key)
                if page_index is not None:
                    return data[page_index], 1.0

        _MIN = BROKEN_LINK_SUGGESTION_MIN_SCORE
        candidate_scores: dict[int, float] = {}
        for fragment in _fragments(target):
            for page_index in fragment_index.get(fragment, []):
                candidate_scores[page_index] = candidate_scores.get(page_index, 0) + 1
        best: tuple[_PageData, float] | None = None
        best_ties = 0  # how many candidates share the current top score
        for candidate_index in _top_candidates(candidate_scores, -1):
            candidate = data[candidate_index]
            score = max(
                string_similarity(target, candidate.slug, _MIN),
                string_similarity(target, candidate.short_name, _MIN),
                string_similarity(target, candidate.title, _MIN),
            )
            if best is None or score > best[1] + 1e-9:
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

    def suggest_related_page(
        page: _PageData, page_index: int, direction: str
    ) -> _PageData | None:
        scores: dict[int, float] = {}
        common_limit = max(20, math.ceil(len(data) * 0.25))
        for token in page.tokens:
            matches = token_index.get(token, [])
            # Very common terms do not identify a useful related page and
            # recreate the quadratic scan this index is intended to avoid.
            if len(matches) > common_limit:
                continue
            weight = 1 if len(token) > 1 else SINGLE_CJK_TOKEN_WEIGHT
            for candidate_index in matches:
                scores[candidate_index] = scores.get(candidate_index, 0) + weight

        existing_outlinks = {normalize_link_target(o) for o in page.outlinks}
        best: tuple[_PageData, float] | None = None
        for candidate_index in _top_candidates(scores, page_index):
            candidate = data[candidate_index]
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
            overlap = scores.get(candidate_index, 0)
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

    # Inbound counts use the same normalization and basename fallback as
    # broken-link existence checks (NashSU 0.6.6 parity).
    inbound_counts: dict[int, int] = {}
    for p in data:
        for link in p.outlinks:
            basename = re.sub(
                r"\.md$", "", _get_file_name(link), flags=re.IGNORECASE
            )
            target = slug_map.get(normalize_link_target(link))
            if target is None:
                target = slug_map.get(normalize_link_target(basename))
            if target is not None:
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

    # Cross-directory basename collisions. The dedup/merge path is keyed by
    # basename slug (_dedup._slug_from_path), so concepts/x.md and
    # methodology/x.md collapse to one id. cross_source_dedup already REFUSES
    # to merge such a group (guard 2026-07-11) rather than risk reading or
    # deleting the wrong file, but that refusal only fires when a detector
    # group happens to contain the slug — otherwise the collision stays
    # invisible. known-issues.md documented a manual `find wiki -name
    # "<slug>.md"` sweep as the workaround; this automates it (2026-07-30).
    paths_by_basename: dict[str, list[str]] = {}
    for p in data:
        stem = re.sub(r"\.md$", "", _get_file_name(p.short_name), flags=re.IGNORECASE)
        paths_by_basename.setdefault(stem, []).append(p.short_name)

    results: list[dict] = []
    for page_index, p in enumerate(data):
        short_name = p.short_name

        # Aggregate files are scanned above (their outlinks count toward inbound),
        # but are exempt from findings so the headless fixer never mutates them.
        if _get_file_name(short_name) in AGGREGATE_FILES:
            continue

        # Slug collision: same basename filed under two or more directories.
        _stem = re.sub(r"\.md$", "", _get_file_name(short_name), flags=re.IGNORECASE)
        _twins = [q for q in paths_by_basename.get(_stem, []) if q != short_name]
        if _twins:
            results.append({
                "type": "slug-collision",
                "severity": "warning",
                "page": short_name,
                "detail": (
                    "Basename collides across directories with "
                    + ", ".join(sorted(_twins))
                    + ". Dedup/merge is keyed by basename, so these share one "
                    "id and are skipped by cross-source dedup. Decide whether "
                    "they are one topic (merge, keep the schema-correct type) "
                    "or genuinely distinct (rename one to a type-specific "
                    "slug)."
                ),
            })

        # Redirect pages are compatibility aliases. Once callers have migrated
        # to the canonical target they are expected to have no inbound links,
        # so reporting them as orphans invites unsafe deletion or a pointless
        # canonical→legacy backlink. Their redirect target still participates
        # in broken-link validation above.
        if p.page_type != "redirect" and page_index not in inbound_counts:
            suggested_source = (
                suggest_related_page(p, page_index, "source")
                if with_suggestions else None
            )
            results.append({
                "type": "orphan",
                "severity": "info",
                "page": short_name,
                "detail": "No other pages link to this page.",
                "suggested_source": suggested_source.short_name if suggested_source else None,
            })

        # No outbound links.
        if p.page_type != "redirect" and len(p.outlinks) == 0:
            suggested_target = (
                suggest_related_page(p, page_index, "target")
                if with_suggestions else None
            )
            results.append({
                "type": "no-outlinks",
                "severity": "info",
                "page": short_name,
                "detail": "This page has no [[wikilink]] references to other pages.",
                "suggested_target": suggested_target.short_name if suggested_target else None,
            })

        # Broken links.
        for link in p.outlinks:
            basename = re.sub(
                r"\.md$", "", _get_file_name(link), flags=re.IGNORECASE
            )
            target = slug_map.get(normalize_link_target(link))
            if target is None:
                target = slug_map.get(normalize_link_target(basename))
            if target is not None:
                continue
            suggestion = _cached_broken_target(link) if with_suggestions else None
            link_origin = (
                "redirect-frontmatter"
                if p.page_type == "redirect"
                and p.redirect_target
                and normalize_link_target(link)
                == normalize_link_target(p.redirect_target)
                else "body"
            )
            results.append({
                "type": "broken-link",
                "severity": "warning",
                "page": short_name,
                "detail": f"Broken link: [[{link}]] — target page not found.",
                "broken_target": link,
                # Preserve the edge source so a confident redirect suggestion
                # is not planned as a body-only rewrite and silently skipped.
                "link_origin": link_origin,
                "suggested_target": suggestion[0].short_name if suggestion else None,
                # improved-wiki extension (2026-07-10): the suggestion's
                # similarity score, persisted so wiki-lint-fix.py can gate
                # headless auto-rewrites (>=0.9 auto, below -> review).
                "suggested_score": round(suggestion[1], 4) if suggestion else None,
            })

    return results
