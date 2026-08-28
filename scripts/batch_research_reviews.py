#!/usr/bin/env python3
"""Batch Deep Research over review items — NashSU v0.6.10 review-panel parity.

NashSU 0.6.10 extended the review panel's select-all: the checkbox that already
fed batch resolve/dismiss now also feeds Deep Research, and a completed or
failed research task can be rerun. `src/lib/review-batch-research.ts` is the
entire selection contract:

    reviewSupportsResearch  -> type is "suggestion" or "missing-page"
    reviewResearchTopic     -> title, else the first line of the description
    selectedResearchReviews -> unresolved AND selected AND supports research
                               AND not already in flight

improved-wiki had only the per-item path. On RadarWiki that is 2633 eligible
items across suggestion/ and missing-page/ — unusable one question at a time.

**This tool selects; it does not research.** It never touches the network and
never writes a wiki page. The 🔴 gate in `deep-research.md` is preserved by
splitting NashSU's single click in two:

    1. the human supplies the filter and sees the exact worklist (preview)
    2. the human authorizes it with --apply, which is the confirmation for
       every topic in it (deep-research.md: "用户在 Process Reviews 中选择
       Deep Research，已经确认该 review 的研究范围")
    3. the agent then runs the normal deep-research.md flow ONE TOPIC AT A
       TIME over the written worklist, with every existing gate intact

Same authority split as `batch_resolve_reviews.py`: the agent must not pick
the filter and must not fire --apply on its own.

Rerun (0.6.10): research that FAILED left its item pending — the saved path is
the success boundary — so it is already in the default set. Research that
COMPLETED resolved its item with a ``Research saved:`` reason; only --rerun
reaches those. A human ``Skip`` is never reopened.

Usage:
    # preview what the filter selects (writes nothing)
    batch_research_reviews.py --project <wiki-root> --type missing-page \\
        --created-before 2026-08-01 --limit 20
    # authorize that exact set
    ... --apply
    # also redo topics whose research page already exists
    ... --rerun --apply
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _paths import atomic_write  # noqa: E402
from _review_utils import (  # noqa: E402
    REVIEW_TITLE_PREFIX_RE,
    is_review_resolved,
)
from batch_resolve_reviews import _created_compact, _frontmatter_value  # noqa: E402

# NashSU reviewSupportsResearch (review-batch-research.ts). Contradictions and
# duplicates need a judgement call about existing pages, not a web search;
# confirm items are already answered. Widening this set is not a formatting
# choice — it changes which findings get answered by an external source.
RESEARCH_ELIGIBLE_TYPES = frozenset({"suggestion", "missing-page"})

# Written by --apply; read by the agent driving deep-research.md.
RESEARCH_BATCH_RELPATH = Path(".llm-wiki") / "research-batch.json"

# deep-research.md §1: `web` unless the user says otherwise.
DEFAULT_SOURCE_MODE = "web"
SOURCE_MODES = ("web", "anytxt", "both")

_TYPE_MARK_RE = re.compile(r"^\[[\w\-]+\]\s*")
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_RESEARCH_SAVED_RE = re.compile(r"^\s*Research saved:\s*(\S+)", re.IGNORECASE)


def review_supports_research(rtype: str) -> bool:
    """NashSU ``reviewSupportsResearch``."""
    return (rtype or "").strip() in RESEARCH_ELIGIBLE_TYPES


def research_topic(title: str, body: str) -> str:
    """The search topic for a review — NashSU ``reviewResearchTopic``.

    Deliberately NOT ``_review_utils.derive_review_topic``: that one builds a
    FILENAME segment (hyphenated, stripped of punctuation, truncated to 40
    characters). A research topic is a search string and must stay a readable
    phrase. Shared with it: dropping the ``[type]`` marker and the
    ``Missing page:``-style prefix that process-reviews.md Step 3 requires,
    and unwrapping a bare wikilink target.
    """
    text = (title or "").strip()
    if not text:
        for line in (body or "").splitlines():
            if line.strip():
                text = line.strip()
                break
    text = _TYPE_MARK_RE.sub("", text).strip()
    prefix = REVIEW_TITLE_PREFIX_RE.match(text)
    if prefix:
        text = text[prefix.end():].strip()
        link = _WIKILINK_RE.search(text)
        if link:
            return link.group(1).split("/")[-1].strip()
    return _WIKILINK_RE.sub(r"\1", text).strip()


def _frontmatter_list(text: str, key: str) -> list[str]:
    """Read a YAML block-sequence frontmatter value (``search_queries``)."""
    m = re.search(rf"^{re.escape(key)}:\s*$(.*?)(?=^\S|\Z)", text,
                  re.M | re.S)
    if not m:
        return []
    values = []
    for line in m.group(1).splitlines():
        item = line.strip()
        if not item.startswith("- "):
            continue
        values.append(item[2:].strip().strip('"').strip("'"))
    return [v for v in values if v]


def _body_of(text: str) -> str:
    parts = text.split("---", 2)
    return parts[2] if len(parts) >= 3 else text


def _previous_research_page(text: str) -> str:
    """The page a completed research run saved, or "" when there is none."""
    m = _RESEARCH_SAVED_RE.match(_frontmatter_value(text, "resolved_reason"))
    return m.group(1) if m else ""


def select_research_items(
    wiki_dir: Path,
    *,
    types: set[str] | None = None,
    created_before: str | None = None,
    title_contains: str | None = None,
    limit: int | None = None,
    rerun: bool = False,
) -> list[dict]:
    """Review items eligible for Deep Research under a human-supplied filter.

    Mirrors ``selectedResearchReviews``: pending + research-eligible type, with
    the CLI filter standing in for NashSU's checkbox selection. ``rerun`` also
    admits items a previous research run RESOLVED (``Research saved:``);
    items resolved by a human decision are never reopened.
    """
    review_dir = Path(wiki_dir) / "REVIEW"
    if not review_dir.is_dir():
        return []
    wanted = {t.strip() for t in (types or set()) if t.strip()}
    cutoff = re.sub(r"\D", "", created_before or "")[:8]
    needle = (title_contains or "").lower()

    items: list[dict] = []
    for path in sorted(review_dir.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        rtype = _frontmatter_value(text, "review_type") or path.parent.name
        if not review_supports_research(rtype):
            continue
        if wanted and rtype not in wanted:
            continue

        previous_page = ""
        if is_review_resolved(text):
            previous_page = _previous_research_page(text)
            # Resolved by a human (Skip / Bulk resolved / …) — not ours to redo.
            if not (rerun and previous_page):
                continue

        title = _frontmatter_value(text, "title")
        if needle and needle not in title.lower():
            continue
        if cutoff:
            created = _created_compact(path, text)
            if not created or created >= cutoff:
                continue

        topic = research_topic(title, _body_of(text))
        if not topic:
            continue
        queries = _frontmatter_list(text, "search_queries") or [topic]
        items.append({
            "review_path": str(path),
            "review_type": rtype,
            "topic": topic,
            "queries": queries,
            "previous_page": previous_page,
        })
        if limit is not None and len(items) >= limit:
            break
    return items


def _print_preview(items: list[dict], *, applied: bool, source_mode: str) -> None:
    if not items:
        print("No review items match this filter — nothing to research.")
        return
    verb = "CONFIRMED" if applied else "PREVIEW (nothing written)"
    print(f"{verb}: {len(items)} review item(s), source mode {source_mode}\n")
    for n, item in enumerate(items, 1):
        rerun_mark = f"  [rerun of {item['previous_page']}]" if item["previous_page"] else ""
        print(f"{n:>3}. [{item['review_type']}] {item['topic']}{rerun_mark}")
        print(f"     queries: {' | '.join(item['queries'])}")
        print(f"     {item['review_path']}")
    if not applied:
        print("\nRe-run with --apply to confirm this exact set. --apply only "
              "writes the worklist; the agent then runs deep-research.md one "
              "topic at a time.")


def main_with_args(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Select review items for batch Deep Research "
                    "(NashSU 0.6.10 review-panel parity). Selects only — the "
                    "research itself still runs through deep-research.md.")
    ap.add_argument("--project", default=".", help="wiki root")
    ap.add_argument("--type", action="append", dest="types",
                    help=f"filter by review type (repeatable; only "
                         f"{'/'.join(sorted(RESEARCH_ELIGIBLE_TYPES))} are "
                         f"research-eligible)")
    ap.add_argument("--created-before", metavar="YYYY-MM-DD",
                    help="only items created strictly before this date")
    ap.add_argument("--title-contains", help="substring filter on title")
    ap.add_argument("--limit", type=int, help="cap the number selected")
    ap.add_argument("--rerun", action="store_true",
                    help="also redo items a previous research run resolved "
                         "(NashSU 0.6.10 rerun-completed); human Skips are "
                         "never reopened")
    ap.add_argument("--source-mode", choices=SOURCE_MODES,
                    default=DEFAULT_SOURCE_MODE,
                    help="deep-research source mode (default: web)")
    ap.add_argument("--apply", action="store_true",
                    help="confirm this exact set and write the worklist; "
                         "without it the tool only previews")
    args = ap.parse_args(argv)

    project = Path(args.project).expanduser().resolve()
    wiki_dir = project / "wiki" if (project / "wiki").is_dir() else project
    items = select_research_items(
        wiki_dir,
        types=set(args.types or []),
        created_before=args.created_before,
        title_contains=args.title_contains,
        limit=args.limit,
        rerun=args.rerun,
    )
    _print_preview(items, applied=bool(args.apply), source_mode=args.source_mode)
    if not args.apply or not items:
        return 0

    plan_path = project / RESEARCH_BATCH_RELPATH
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(plan_path, json.dumps({
        "source_mode": args.source_mode,
        "rerun": bool(args.rerun),
        "items": items,
    }, ensure_ascii=False, indent=2) + "\n")
    print(f"\nWorklist written: {plan_path}")
    print("Run deep-research.md for each entry in order, one topic per "
          "invocation. Resolve a review only after its page is saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main_with_args())
