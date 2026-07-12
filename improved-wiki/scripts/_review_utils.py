"""_review_utils.py — shared helpers for reasoning about review items.

Port of NashSU ``src/lib/review-utils.ts`` (``normalizeReviewTitle`` +
``REVIEW_TITLE_PREFIX_RE``) and the content-stable id / field-union dedup from
``src/stores/review-store.ts`` (``reviewIdFor`` / ``unionField`` /
``mergeReviewItems`` / ``normalizeReviewItems``).

Kept dependency-free so both ``_stage_3_4_review`` (write side) and
``sweep_reviews`` (read/dedup side) can import it without a cycle.

Python 3.9 target: no ``match``, no ``X | Y`` runtime unions.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence

# Port of NashSU review-utils.ts REVIEW_TITLE_PREFIX_RE — common prefixes the
# LLM may prepend in English or Chinese review titles. Kept in one place so
# dedup and sweep agree on what "the same concept" means.
REVIEW_TITLE_PREFIX_RE = re.compile(
    r"^(missing[\s-]?page[:：]\s*|duplicate[\s-]?page[:：]\s*|"
    r"possible[\s-]?duplicate[:：]\s*|缺失页面[:：]\s*|缺少页面[:：]\s*|"
    r"重复页面[:：]\s*|疑似重复[:：]\s*)",
    re.IGNORECASE,
)


def normalize_review_title(title: str) -> str:
    """Port of NashSU review-utils.ts ``normalizeReviewTitle``.

    Normalize a review title for equality comparison:
      - strip leading "Missing page:" / "缺失页面:" / etc.
      - collapse whitespace
      - lowercase

    Two review items with the same (type, normalized title) are considered the
    same concept and should be merged rather than duplicated.
    """
    stripped = title.lstrip()
    stripped = REVIEW_TITLE_PREFIX_RE.sub("", stripped)
    stripped = re.sub(r"\s+", " ", stripped)
    return stripped.strip().lower()


def review_id_for(rtype: str, title: str) -> str:
    """Port of NashSU review-store.ts ``reviewIdFor`` (FNV-1a 32-bit).

    Content-derived stable id: the SAME logical review (same type + same
    normalized title) always gets the SAME id, so it survives ingest
    regeneration, file moves, and reloads.
    """
    key = f"{rtype}::{normalize_review_title(title)}"
    h = 0x811C9DC5
    # Iterate UTF-16 code units (little-endian byte pairs), matching NashSU's
    # JS `charCodeAt` semantics: a non-BMP character (emoji, rare CJK ext)
    # hashes as its surrogate PAIR, not its single code point. Python's
    # `ord(ch)` (code points) agreed only for BMP text and forked ids for
    # non-BMP titles.
    data = key.encode("utf-16-le")
    for i in range(0, len(data), 2):
        h ^= data[i] | (data[i + 1] << 8)
        # FNV prime 0x01000193, kept to 32 bits (JS Math.imul semantics).
        h = (h * 0x01000193) & 0xFFFFFFFF
    return f"review-{(h & 0xFFFFFFFF):08x}"


def union_field(a: Optional[Sequence[str]],
                b: Optional[Sequence[str]]) -> Optional[List[str]]:
    """Port of NashSU review-store.ts ``unionField``.

    Union two optional string sequences (order-preserving, deduped), dropping
    the field (return None) when the result is empty.
    """
    merged: List[str] = []
    seen = set()
    for seq in (a or [], b or []):
        for v in seq:
            if v not in seen:
                seen.add(v)
                merged.append(v)
    return merged if merged else None


def merge_review_items(a: Dict, b: Dict) -> Dict:
    """Port of NashSU review-store.ts ``mergeReviewItems``.

    Collapse two items that resolved to the same stable id: resolved wins (if
    either was resolved, the survivor is), union the array fields, keep the
    earliest createdAt, prefer a non-empty description.

    ``a`` is the survivor; its ``id`` is kept (both share it by construction).
    """
    resolved = bool(a.get("resolved")) or bool(b.get("resolved"))
    resolved_action = (
        (a.get("resolved_action") or b.get("resolved_action")) if resolved else None
    )
    merged = dict(a)
    merged["resolved"] = resolved
    merged["resolved_action"] = resolved_action
    merged["description"] = a.get("description") or b.get("description") or ""
    merged["source_path"] = (
        a.get("source_path") if a.get("source_path") is not None else b.get("source_path")
    )
    merged["affected_pages"] = union_field(a.get("affected_pages"), b.get("affected_pages"))
    merged["search_queries"] = union_field(a.get("search_queries"), b.get("search_queries"))
    a_created = a.get("created_at")
    b_created = b.get("created_at")
    if a_created is not None and b_created is not None:
        merged["created_at"] = min(a_created, b_created)
    else:
        merged["created_at"] = a_created if a_created is not None else b_created
    return merged


def normalize_review_items(items: Sequence[Dict]) -> List[Dict]:
    """Port of NashSU review-store.ts ``normalizeReviewItems``.

    Remap every item to its content-stable id, collapsing any that share one
    (resolved wins, array fields unioned). Idempotent: the id is computed from
    content, not from the incoming id, so re-running over already-normalized
    items is a no-op.

    Each item dict must carry ``type`` and ``title``; ``id`` is (re)assigned.
    """
    by_id: Dict[str, Dict] = {}
    order: List[str] = []
    for raw in items:
        remapped = dict(raw)
        remapped["id"] = review_id_for(raw.get("type", ""), raw.get("title", ""))
        existing = by_id.get(remapped["id"])
        if existing is None:
            by_id[remapped["id"]] = remapped
            order.append(remapped["id"])
        else:
            by_id[remapped["id"]] = merge_review_items(existing, remapped)
    return [by_id[i] for i in order]
