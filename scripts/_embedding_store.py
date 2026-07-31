#!/usr/bin/env python3
"""Small, provider-independent LanceDB lifecycle helpers.

NashSU removes a page's vector rows after the Markdown file is deleted.  Keep
that operation separate from ``build_embeddings.py`` so source/lint deletion
does not need an embedding provider or import the HTTP client.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from _paths import detect_runtime_dir


TABLE_NAME = "wiki_chunks"


def _page_filter(page_id: str) -> str:
    return "page_id = '" + page_id.replace("'", "''") + "'"


def page_id_from_path(project_root: Path, raw_path: str | Path) -> str:
    """Return improved-wiki's collision-safe, wiki-relative page id."""
    root = Path(project_root).expanduser().resolve()
    wiki = (root / "wiki").resolve()
    raw = Path(raw_path).expanduser()
    if raw.is_absolute():
        candidate = raw.resolve()
    elif raw.parts and raw.parts[0] == "wiki":
        candidate = (root / raw).resolve()
    else:
        candidate = (wiki / raw).resolve()
    try:
        relative = candidate.relative_to(wiki)
    except ValueError as exc:
        raise ValueError(f"Embedding page escapes wiki/: {raw_path}") from exc
    if relative.suffix.lower() != ".md":
        raise ValueError(f"Embedding page is not Markdown: {raw_path}")
    return relative.with_suffix("").as_posix()


def remove_page_embeddings(
    project_root: Path,
    page_paths: Iterable[str | Path],
    *,
    strict: bool = False,
) -> dict:
    """Delete all vector rows belonging to ``page_paths``.

    The Markdown file may already be gone.  Missing indexes/tables are normal
    no-ops.  NashSU treats page-vector cleanup as non-critical, so callers use
    the default best-effort mode; the explicit embedding CLI uses ``strict``.
    """
    root = Path(project_root).expanduser().resolve()
    try:
        page_ids = sorted(
            {
                page_id_from_path(root, raw_path)
                for raw_path in page_paths
            }
        )
    except Exception as exc:
        if strict:
            raise
        return {
            "requested_pages": 0,
            "matched_pages": 0,
            "rows_removed": 0,
            "index_present": False,
            "error": str(exc),
        }

    result = {
        "requested_pages": len(page_ids),
        "matched_pages": 0,
        "rows_removed": 0,
        "index_present": False,
        "error": "",
    }
    if not page_ids:
        return result

    lance_dir = detect_runtime_dir(root) / "lancedb"
    if not lance_dir.exists():
        return result

    try:
        import lancedb

        db = lancedb.connect(str(lance_dir))
        try:
            table = db.open_table(TABLE_NAME)
        except Exception:
            return result
        result["index_present"] = True
        for page_id in page_ids:
            predicate = _page_filter(page_id)
            before = table.count_rows(predicate)
            if before <= 0:
                continue
            table.delete(predicate)
            remaining = table.count_rows(predicate)
            if remaining != 0:
                raise RuntimeError(
                    f"LanceDB page delete incomplete for {page_id}: "
                    f"{remaining} row(s) remain"
                )
            result["matched_pages"] += 1
            result["rows_removed"] += before
    except Exception as exc:
        if strict:
            raise RuntimeError(f"Failed to delete page embeddings: {exc}") from exc
        result["error"] = str(exc)
    return result
