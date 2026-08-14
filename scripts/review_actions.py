#!/usr/bin/env python3
"""Review action routing — port of NashSU review-view.tsx / review-create-page.ts.

In NashSU this is code: `handleResolve` is a deterministic branch over the
action string, `createReviewPageDrafts` is a deterministic branch over the
item, and `ReviewItem.options` is data carried on the item. improved-wiki had
none of it as code — it lived as prose in ``references/process-reviews.md``,
where it had drifted from the real source in four ways (audited 2026-08-05
against ``llm_wiki-0.6.7``; see ``scripts/tests/test_review_actions.py`` for
the itemised list and the source line numbers each rule comes from).

Prose cannot be tested, so it drifted silently. This module makes the routing
executable and pinned. The agent driving ``process-reviews`` decides *which
action a human chose*; this module decides *what that action means*.

Nothing here writes to disk: every function is pure and returns a decision the
caller executes. That keeps the human gate where it belongs — an action is
only ever routed after a person picked it.
"""
from __future__ import annotations

import base64
from datetime import datetime
import re

from _wiki_filename import make_query_file_name

# ── ① Options: per-item data, derived from the type ──────────────────────────
#
# NashSU's options reach the item through the ingest prompt
# (ingest.ts:2250-2258), which hard-restricts them to `Create Page | Skip` for
# the four recognised types, and through the parser's fallback
# (ingest.ts:2029-2032), which supplies `Approve | Skip` when a REVIEW block
# carries no OPTIONS line — the case for every `confirm` item, since `confirm`
# is the bucket for блоcks whose type wasn't recognised (ingest.ts:2016-2020)
# and the prompt never asks for OPTIONS on it.
#
# The value is therefore fully determined by the type. improved-wiki's Stage
# 3.1 uses a JSON schema rather than NashSU's `---REVIEW:` text blocks, so
# asking the LLM to emit a field it can only fill one of two ways would add a
# drift surface for zero information. Deriving it reproduces exactly what
# NashSU's parser produces, and cannot disagree with it.

_CREATE_PAGE_OPTIONS = ["Create Page", "Skip"]
_PARSER_FALLBACK_OPTIONS = ["Approve", "Skip"]

#: Types NashSU's ingest prompt lists an explicit OPTIONS line for.
PROMPTED_REVIEW_TYPES = frozenset(
    {"contradiction", "duplicate", "missing-page", "suggestion"})

#: Types whose card shows the UI-added Deep Research button
#: (review-view.tsx:491). NOT every type — this is the gate the old fixed
#: triple erased.
DEEP_RESEARCH_TYPES = frozenset({"suggestion", "missing-page"})


def default_options_for(review_type: str) -> list[str]:
    """The `options` a NashSU item of this type actually carries."""
    if review_type in PROMPTED_REVIEW_TYPES:
        return list(_CREATE_PAGE_OPTIONS)
    return list(_PARSER_FALLBACK_OPTIONS)


def offers_deep_research(review_type: str) -> bool:
    """Whether the review card shows the Deep Research button."""
    return review_type in DEEP_RESEARCH_TYPES


def buttons_for(review_type: str) -> list[str]:
    """Every button a human actually sees for this type, in panel order."""
    buttons = ["Deep Research"] if offers_deep_research(review_type) else []
    return buttons + default_options_for(review_type)


# ── ② Create Page: type routing and title extraction ─────────────────────────
#
# re.ASCII on the `\b` patterns is deliberate: JavaScript's `\b` is ASCII-only,
# so in NashSU `\bentity\b` does NOT match inside a CJK run. Python's `\b` is
# Unicode-aware by default and `\w` includes CJK, which would silently change
# which items route to entities/. The flag restores JS semantics. It does not
# affect the literal CJK alternatives (实体/概念) — flags never change literal
# matching.

_ENTITY_RE = re.compile(r"\b(entity|entities)\b|实体", re.I | re.A)
_CONCEPT_RE = re.compile(r"\b(concept|concepts)\b|概念", re.I | re.A)
_COMPARISON_RE = re.compile(r"comparison|compare|比较", re.I)
_SYNTHESIS_RE = re.compile(r"synthesis|综合", re.I)

_ACTION_PREFIX_RE = re.compile(
    r"^(Create|Save|Add|Missing page|Missing pages|缺失页面|缺少页面|创建|保存|新增)"
    r"[:：\s-]*", re.I)
_LEADING_MISSING_RE = re.compile(r"^(missing|缺失|缺少)\s*", re.I)
_TRAILING_PAGE_RE = re.compile(r"\s*(page|pages|页面|页)\s*$", re.I)
_TRAILING_KIND_RE = re.compile(
    r"\s*(entity|entities|concept|concepts|实体|概念)\s*"
    r"(page|pages|页面|页)?\s*$", re.I)

# ── Two deliberate divergences from NashSU, both defect fixes ────────────────
#
# NashSU's own regexes emit a garbage second candidate on the two commonest
# missing-page phrasings. Verified by running the verbatim JS against node:
#
#   "Missing page: [[concepts/cfar-loss]]"  -> ["concepts/cfar-loss",
#                                               ": [[concepts/cfar-loss"]
#   "Missing pages: Alpha, Beta and Gamma"  -> ["Alpha","Beta","Gamma",
#                                               "s: Alpha"]
#
# Both are single-token causes:
#
#  1. `(?:entity|entities|concept|concepts|page|pages)?` is ordered shortest
#     first, so `page` consumes "page" out of "pages" and the stray "s" leaks
#     into the captured title. Reordering longest-first is what the
#     alternation plainly intends.
#  2. The leading edge-punctuation class omits `:` / `：` while the trailing
#     class includes them, so a leading ":" blocks the `[` stripping that
#     immediately follows it. Adding them restores the symmetry.
#
# Reproducing these faithfully would create a junk wiki page beside every
# real one — 43 of them on RadarWiki's current missing-page backlog. That is a
# worse outcome than a documented divergence, so improved-wiki keeps the fixed
# forms. No correct candidate is lost: the corrections only remove candidates
# that were artifacts of the double match.
_EN_MISSING_KINDS = r"(?:entities|entity|concepts|concept|pages|page)?"
_EDGE_PUNCT_RE = re.compile(
    r"^[\s:：\"'“”‘’`\[\]【】()（）]+|[\s\"'“”‘’`\[\]【】()（）:：.。]+$")

_SEGMENT_SPLIT_RE = re.compile(r"[\n。]+")
_LIST_SPLIT_RE = re.compile(r"[,，、;；\n]+")
_AND_RE = re.compile(r"\band\b", re.I)
_CN_AND_RE = re.compile(r"\s+和\s+")

_COLON_TAIL_RE = re.compile(r"[:：]\s*(.+)$")
_CN_MISSING_RE = re.compile(
    r"(?:缺少|缺失|未创建|没有)\s*([^；;]+?)(?:等)?\s*(?:实体|概念)?\s*"
    r"(?:页面|页)(?:缺失|不存在|未创建)?", re.I)
_EN_MISSING_RE = re.compile(
    rf"missing\s+{_EN_MISSING_KINDS}\s*"
    r"([^.;]+?)(?:\s+pages?|\s+entities?|\s+concepts?)?$", re.I)

_DIR_FOR_TYPE = {
    "entity": "entities",
    "concept": "concepts",
    "comparison": "comparisons",
    "synthesis": "synthesis",
    "query": "queries",
}


def _clean_candidate_title(value: str) -> str:
    value = _ACTION_PREFIX_RE.sub("", value, count=1)
    value = _LEADING_MISSING_RE.sub("", value, count=1)
    value = _TRAILING_PAGE_RE.sub("", value, count=1)
    value = _TRAILING_KIND_RE.sub("", value, count=1)
    value = _EDGE_PUNCT_RE.sub("", value)
    return value.strip()


def _split_candidate_list(value: str) -> list[str]:
    value = _AND_RE.sub(",", value)
    value = _CN_AND_RE.sub(",", value)
    return [t for t in (_clean_candidate_title(p)
                        for p in _LIST_SPLIT_RE.split(value)) if t]


def _extract_missing_page_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    segments = [s for s in (re.sub(r"\s+", " ", part).strip()
                            for part in _SEGMENT_SPLIT_RE.split(text)) if s]

    for segment in segments:
        colon_tail = _COLON_TAIL_RE.search(segment)
        if colon_tail:
            candidates += _split_candidate_list(colon_tail.group(1))

        cn = _CN_MISSING_RE.search(segment)
        if cn and cn.group(1):
            candidates += _split_candidate_list(cn.group(1))

        en = _EN_MISSING_RE.search(segment)
        if en and en.group(1):
            candidates += _split_candidate_list(en.group(1))

    if not candidates:
        candidates.append(
            _clean_candidate_title(segments[0] if segments else "")
            or "Untitled")

    # dict.fromkeys — dedup preserving first-seen order (JS `new Set`).
    return list(dict.fromkeys(candidates))


def detect_page_type(action: str, review_type: str, text: str) -> str:
    """NashSU detectPageType — matched over action + title + description."""
    combined = f"{action}\n{text}"
    if _ENTITY_RE.search(combined):
        return "entity"
    if _CONCEPT_RE.search(combined):
        return "concept"
    if _COMPARISON_RE.search(combined):
        return "comparison"
    if _SYNTHESIS_RE.search(combined):
        return "synthesis"
    if review_type == "missing-page":
        return "concept"
    return "query"


def create_review_page_drafts(item: dict, action: str) -> list[dict]:
    """One draft per page this action should create (NashSU parity).

    Only ``missing-page`` fans out to several drafts; every other type yields
    exactly one. All drafts share the single detected page type.
    """
    title = (item.get("title") or "").strip()
    description = item.get("description") or ""
    review_type = item.get("review_type") or item.get("type") or ""
    text = f"{title}\n{description}"

    page_type = detect_page_type(action, review_type, text)
    titles = (_extract_missing_page_candidates(text)
              if review_type == "missing-page"
              else [_clean_candidate_title(title) or "Untitled"])

    return [{"title": t, "page_type": page_type,
             "dir": _DIR_FOR_TYPE[page_type]} for t in titles]


# ── ③/④ Action routing ───────────────────────────────────────────────────────

_OPEN_ACTIONS = frozenset({
    "open", "view", "open page", "view page",
    "打开", "查看", "打开页面", "查看页面",
})
_DISMISSAL_ACTIONS = frozenset({
    "skip", "dismiss", "ignore", "跳过", "忽略", "approve", "keep existing",
    "no",
})
_RESEARCH_NEEDLES = ("research", "investigate", "explore", "look into",
                     "研究", "调研", "探索")

_DEEP_RESEARCH_SENTINEL = "__deep_research__"
_CREATE_PAGE_SENTINEL = "__create_page__:"

_TOPIC_PREFIX_RE = re.compile(r"^(Save to Wiki|Create|Research)[:\s]*", re.I)
_RESEARCH_PREFIX_RE = re.compile(r"^research\s*", re.I)


def _looks_like_open(action: str) -> bool:
    return action.strip().lower() in _OPEN_ACTIONS


def _is_dismissal(action: str) -> bool:
    # NashSU's actionIsDismissal lowercases but does NOT trim, unlike
    # actionLooksLikeOpen. Replicated rather than "fixed": trimming here would
    # reroute a padded " Skip " from a plain resolve into a page creation.
    return action.lower() in _DISMISSAL_ACTIONS


def _looks_like_research(action: str) -> bool:
    if action.startswith("__"):
        return False
    lower = action.lower()
    return any(needle in lower for needle in _RESEARCH_NEEDLES)


def _first_description_line(item: dict) -> str:
    return (item.get("description") or "").split("\n")[0]


def route_review_action(item: dict, action: str, *,
                        has_search_source: bool,
                        now: datetime | None = None) -> dict:
    """What a chosen action means. Pure — the caller performs the effect.

    Returns a dict with a ``kind`` and a ``resolves`` flag. ``resolves`` is
    False for the two branches that deliberately leave the item pending:
    opening a page to look at it, and an explicit Deep Research with no
    configured search source.
    """
    # 1. Explicit Deep Research button — checked first, before any fuzzy
    #    matching, and the ONLY branch that blocks on a missing search source.
    if action == _DEEP_RESEARCH_SENTINEL:
        if not has_search_source:
            return {"kind": "blocked_no_search_source", "resolves": False}
        raw_title = (item.get("title") or "").strip()
        topic = _TOPIC_PREFIX_RE.sub("", raw_title, count=1).strip() \
            or _first_description_line(item)
        return {
            "kind": "deep_research",
            "resolves": False,  # resolution waits for the saved page
            "topic": topic,
            "search_queries": list(item.get("search_queries") or []),
        }

    # 2. Save decoded content as a query page.
    if action.startswith("save:"):
        try:
            content = base64.b64decode(action[5:], validate=True).decode()
            reason = "Saved to Wiki"
        except Exception:
            content = ""
            reason = "Save failed"
        return {"kind": "save_page", "resolves": True, "content": content,
                "resolve_reason": reason}

    # 3. Open a page WITHOUT resolving — looking is not accepting.
    if action.startswith("open:") or _looks_like_open(action):
        if action.startswith("open:"):
            page = action[5:]
        else:
            affected = item.get("affected_pages") or []
            page = affected[0] if affected else (item.get("source_path") or "")
        if not page:
            return {"kind": "noop", "resolves": False}
        return {"kind": "open_page", "resolves": False, "page": page}

    # 4. Delete a file, then resolve.
    if action.startswith("delete:"):
        return {"kind": "delete_file", "resolves": True, "path": action[7:],
                "resolve_reason": "Deleted"}

    # 5. Heuristic research. Unlike branch 1, a missing search source here
    #    falls through to page creation rather than blocking.
    if _looks_like_research(action):
        if not has_search_source:
            return create_page_decision(item, action, now)
        topic = _RESEARCH_PREFIX_RE.sub("", action, count=1).strip() \
            or _first_description_line(item)
        return {
            "kind": "deep_research",
            "resolves": False,
            "topic": topic,
            # NashSU passes `undefined` here — the item's seed queries are
            # deliberately not reused on this path (review-view.tsx:181).
            "search_queries": [],
        }

    # 6. Create page(s).
    if action.startswith(_CREATE_PAGE_SENTINEL):
        return create_page_decision(item, action[len(_CREATE_PAGE_SENTINEL):], now)
    if not _is_dismissal(action):
        return create_page_decision(item, action, now)

    # 7. Plain resolution (every dismissal action lands here).
    return {"kind": "resolve", "resolves": True, "resolve_reason": action}


def create_page_decision(
    item: dict,
    action: str,
    now: datetime | None = None,
) -> dict:
    """Decide the pages to create and the reason that records them.

    Each draft carries the filename it must be written under, so the resolve
    reason names a path that can actually be opened later. NashSU builds this
    string after the writes, from ``created[].fileName`` (review-view.tsx:250);
    naming the human title instead leaves an audit trail that cannot locate the
    page it claims to have created.
    """
    drafts = []
    for draft in create_review_page_drafts(item, action):
        file_name, created = make_query_file_name(draft["title"], now)
        drafts.append({**draft, "file_name": file_name, "created": created})
    reason = (f"Created: wiki/{drafts[0]['dir']}/{drafts[0]['file_name']}"
              if len(drafts) == 1 else f"Created {len(drafts)} pages")
    return {"kind": "create_page", "resolves": True, "drafts": drafts,
            "resolve_reason": reason}
