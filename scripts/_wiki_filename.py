"""Shared query-page filename rules — port of NashSU ``wiki-filename.ts``.

Every wiki page born outside ingest (deep research, review Create Page, review
``save:``) gets its filename here, exactly as NashSU routes all three through
``makeQueryFileName``. Keeping it in one module is what stops the review path
and the research path from drifting into two naming schemes.
"""
from __future__ import annotations

from datetime import datetime, timezone, tzinfo
import re
import unicodedata


def make_query_slug(title: str) -> str:
    """Port NashSU ``makeQuerySlug`` without using ingest's broader slugger."""
    normalized = unicodedata.normalize("NFKC", title).strip()
    slug = re.sub(r"\s+", "-", normalized)
    slug = "".join(
        char
        for char in slug
        if char == "-" or unicodedata.category(char)[:1] in {"L", "N"}
    )
    slug = re.sub(r"-+", "-", slug).strip("-").lower()
    truncated = slug[:50]
    return truncated or "query"


def make_query_file_name(
    title: str,
    now: datetime | None = None,
    *,
    local_timezone: tzinfo | None = None,
) -> tuple[str, str]:
    """Return ``(<slug>-<UTC date>-<UTC time>.md, local created date)``.

    The filename is stamped in UTC so the same save produces the same name on
    any machine rather than shifting with the local timezone or DST, while
    ``created`` is the author's local calendar date — a page written at 08:00
    on the 4th should not be dated the 3rd because UTC has not caught up.

    That split is improved-wiki's, not NashSU's: NashSU uses its UTC date for
    both. The research writer settled on it first and is tested on it, so
    review-created pages follow the same rule — one date convention for every
    non-ingest page beats matching NashSU on a detail the skill has already
    deliberately diverged on.
    """
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.astimezone()
    utc_instant = instant.astimezone(timezone.utc)
    local_instant = (instant.astimezone(local_timezone) if local_timezone
                     else instant.astimezone())
    file_name = (
        f"{make_query_slug(title)}-"
        f"{utc_instant.strftime('%Y-%m-%d-%H%M%S')}.md"
    )
    return file_name, local_instant.strftime("%Y-%m-%d")
