"""Review write side — port of the disk work inside NashSU ``handleResolve``.

``review_actions.py`` decides *what* an action means and stays pure. This
module performs the two actions that actually write: Create Page and
``save:``. Both create a page, add it to ``wiki/index.md``, and append a line
to ``wiki/log.md``.

In NashSU this is code, inline in ``review-view.tsx`` (Create Page at
:196-259, ``save:`` at :87-131). On the improved-wiki side it was prose in
``references/process-reviews.md``, which is exactly why two nearby claims in
that file had drifted unnoticed — prose cannot be tested. The index and log
edits are pure string transforms here so they can be.

Deep Research deliberately does **not** come through here: it only reads
``wiki/index.md`` for grounding and writes neither aggregate
(``deep-research.ts:339``). Do not "unify" the two.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

from _paths import atomic_write
from _wiki_filename import make_query_file_name

DEFAULT_INDEX_TEXT = "# Wiki Index\n"
DEFAULT_LOG_TEXT = "# Wiki Log\n"


def index_section_header(directory: str) -> str:
    """``concepts`` -> ``## Concepts``.

    Only the first character is upper-cased, matching NashSU's
    ``dir.charAt(0).toUpperCase() + dir.slice(1)``; ``str.title()`` would
    mangle any directory containing a separator.
    """
    return f"## {directory[:1].upper()}{directory[1:]}"


def index_entry_line(directory: str, file_name: str, title: str) -> str:
    """The wikilink line an index section carries for one page."""
    target = re.sub(r"\.md$", "", file_name)
    return f"- [[{directory}/{target}|{title}]]"


def insert_index_entry(
    index_text: str,
    directory: str,
    file_name: str,
    title: str,
) -> str:
    """Add one entry to its section, creating the section when absent.

    New entries go directly under the section header, which is where NashSU
    puts them.

    Divergence from NashSU, deliberate: NashSU tests for the section with
    ``includes(header)`` but inserts with ``/(header\\n)/``. When a wiki has a
    section whose name merely starts with this one — ``## Conceptsx`` beside
    ``## Concepts`` — the test passes, the insert matches nothing, and the
    entry is silently dropped. Both steps here use the same full-line match, so
    the entry either lands in the section or starts a new one.
    """
    header = index_section_header(directory)
    entry = index_entry_line(directory, file_name, title)
    header_line = re.compile(rf"^{re.escape(header)}[ \t]*$", re.MULTILINE)
    match = header_line.search(index_text)
    if match:
        cut = match.end()
        return f"{index_text[:cut]}\n{entry}{index_text[cut:]}"
    return f"{index_text.rstrip()}\n\n{header}\n{entry}\n"


def append_log_entry(log_text: str, date: str, message: str) -> str:
    """Append one dated log line."""
    return f"{log_text.rstrip()}\n- {date}: {message}\n"


def _read_or_default(path: Path, default: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return default


def _page_frontmatter(
    page_type: str,
    title: str,
    created: str,
    *,
    related: bool,
) -> str:
    """Frontmatter for a review-created page.

    ``related: []`` is present for Create Page and absent for ``save:``,
    matching the two literals NashSU writes (review-view.tsx:212 vs :98).
    """
    lines = [
        "---",
        f"type: {page_type}",
        f'title: "{title.replace(chr(34), chr(92) + chr(34))}"',
        f"created: {created}",
        "tags: []",
    ]
    if related:
        lines.append("related: []")
    lines += ["---", "", ""]
    return "\n".join(lines)


def write_created_pages(
    project: Path,
    item: dict,
    drafts: list[dict],
) -> list[dict]:
    """Write every draft, then update the index and log once.

    ``drafts`` must come from ``review_actions.create_page_decision`` — the
    filenames it computed are the ones recorded in the item's resolve reason,
    so re-deriving them here would let the two disagree.

    Returns the created page records. Raises rather than reporting partial
    success: a review resolved against pages that were only half written is
    worse than one left pending.
    """
    wiki = Path(project) / "wiki"
    description = item.get("description") or ""
    created: list[dict] = []

    for draft in drafts:
        file_name = draft["file_name"]
        page_path = wiki / draft["dir"] / file_name
        page_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(page_path, (
            _page_frontmatter(
                draft["page_type"], draft["title"], draft["created"],
                related=True)
            + f"# {draft['title']}\n\n{description}\n"
        ))
        created.append({
            "title": draft["title"],
            "dir": draft["dir"],
            "file_name": file_name,
            "created": draft["created"],
            "path": page_path.relative_to(project).as_posix(),
        })

    index_path = wiki / "index.md"
    index_text = _read_or_default(index_path, DEFAULT_INDEX_TEXT)
    for page in created:
        index_text = insert_index_entry(
            index_text, page["dir"], page["file_name"], page["title"])
    atomic_write(index_path, index_text)

    names = ", ".join(f"`{page['file_name']}`" for page in created)
    plural = "" if len(created) == 1 else "s"
    log_path = wiki / "log.md"
    atomic_write(log_path, append_log_entry(
        _read_or_default(log_path, DEFAULT_LOG_TEXT),
        created[0]["created"],
        f"Created {len(created)} page{plural} from review: {names}",
    ))
    return created


def write_saved_query_page(
    project: Path,
    title: str,
    content: str,
    now: datetime | None = None,
) -> dict:
    """Write a ``save:`` payload as a query page, then index and log it."""
    wiki = Path(project) / "wiki"
    file_name, created = make_query_file_name(title, now)
    page_path = wiki / "queries" / file_name
    page_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(page_path, (
        _page_frontmatter("query", title, created, related=False) + content
    ))

    index_path = wiki / "index.md"
    atomic_write(index_path, insert_index_entry(
        _read_or_default(index_path, DEFAULT_INDEX_TEXT),
        "queries", file_name, title,
    ))

    log_path = wiki / "log.md"
    atomic_write(log_path, append_log_entry(
        _read_or_default(log_path, DEFAULT_LOG_TEXT),
        created,
        f"Saved query page `{file_name}`",
    ))
    return {
        "title": title,
        "dir": "queries",
        "file_name": file_name,
        "created": created,
        "path": page_path.relative_to(project).as_posix(),
    }
