#!/usr/bin/env python3
"""search_local.py — Local source search for deep-research (NashSU AnyTXT parity).

Searches BOTH wiki/ (reuses keyword_search from _wiki_keyword) and raw/
(macOS Spotlight `mdfind` over PDF content, ripgrep fallback) so deep-research
can merge local hits with web results before synthesis.

Output format matches NashSU WebSearchResult shape so Claude can merge local
and web sources uniformly:

    [N] **<title>** (local:wiki)
    <snippet>
    path: <absolute path>

    [N] **<filename>** (local:raw)
    <snippet or "(PDF content match — open file to view context)">
    path: <absolute path>

Usage:
    search_local.py "<query>" --project ~/Documents/知识库/HardwareWiki [--top 10]
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from _wiki_keyword import keyword_search


def _search_wiki(project: Path, query: str, top: int) -> list[dict]:
    """Reuse keyword_search over wiki/*.md — returns hits with title + snippet."""
    wiki_dir = project / "wiki"
    if not wiki_dir.is_dir():
        return []
    hits = keyword_search(wiki_dir, query, max_results=top)
    out = []
    for h in hits:
        rel = h.get("path", h.get("file", ""))
        snippet = h.get("snippet", h.get("context", ""))
        title = h.get("title", Path(rel).stem if rel else "")
        out.append({
            "source": "local:wiki",
            "title": title,
            "snippet": snippet,
            "path": str(wiki_dir / rel) if rel else str(wiki_dir),
        })
    return out


def _extract_pdf_snippet(pdf_path: Path, query: str, max_chars: int = 300) -> str:
    """Try to extract a context snippet from a PDF via pdftotext + grep.

    Returns a fallback message if pdftotext is unavailable or no match found.
    """
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        return "(PDF content match — open file to view context)"
    try:
        result = subprocess.run(
            [pdftotext, str(pdf_path), "-"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0 or not result.stdout:
            return "(PDF content match — open file to view context)"
        text = result.stdout
        query_lower = query.lower()
        tokens = [t for t in query_lower.split() if len(t) > 1]
        for token in tokens:
            idx = text.lower().find(token)
            if idx >= 0:
                start = max(0, idx - 120)
                end = min(len(text), idx + max_chars - 120)
                snippet = text[start:end].replace("\n", " ").strip()
                return snippet[:max_chars]
        return "(PDF content match — open file to view context)"
    except Exception as e:
        print(f"[search-local] pdftotext snippet failed for {pdf_path.name} "
              f"({type(e).__name__}) — returning generic snippet", file=sys.stderr)
        return "(PDF content match — open file to view context)"


def _search_raw_mdfind(raw_dir: Path, query: str, top: int) -> list[dict]:
    """Search raw/ PDF content via macOS Spotlight (mdfind).

    Spotlight indexes PDF text natively on macOS. Returns matching file paths.
    """
    mdfind = shutil.which("mdfind")
    if not mdfind:
        return []
    try:
        result = subprocess.run(
            [mdfind, "-onlyin", str(raw_dir), query],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return []
        paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        out = []
        for p in paths[:top]:
            path = Path(p)
            if not path.is_file():
                continue
            snippet = _extract_pdf_snippet(path, query) if path.suffix.lower() == ".pdf" else ""
            out.append({
                "source": "local:raw",
                "title": path.name,
                "snippet": snippet,
                "path": str(path),
            })
        return out
    except Exception as e:
        print(f"[search-local] mdfind failed ({type(e).__name__}: {e}) — "
              f"falling back to ripgrep sidecar search", file=sys.stderr)
        return []


def _search_raw_ripgrep(raw_dir: Path, query: str, top: int) -> list[dict]:
    """Fallback: ripgrep over any text-extractable files in raw/.

    Searches .txt/.md/.json sidecars (minerU extractions, captions) since
    ripgrep cannot read PDF binaries directly.
    """
    rg = shutil.which("rg")
    if not rg:
        return []
    try:
        result = subprocess.run(
            [rg, "-l", "-i", "--max-count", "1",
             "-g", "*.txt", "-g", "*.md", "-g", "*.json",
             query, str(raw_dir)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode not in (0, 1):
            return []
        paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        out = []
        for p in paths[:top]:
            path = Path(p)
            if not path.is_file():
                continue
            out.append({
                "source": "local:raw",
                "title": path.name,
                "snippet": f"(text match in {path.suffix} sidecar)",
                "path": str(path),
            })
        return out
    except Exception as e:
        print(f"[search-local] ripgrep sidecar search failed "
              f"({type(e).__name__}: {e}) — raw/ results unavailable",
              file=sys.stderr)
        return []


def search_local(project: Path, query: str, top: int = 10) -> list[dict]:
    """Search wiki/ + raw/, return merged local sources (wiki first, then raw)."""
    wiki_hits = _search_wiki(project, query, top)
    raw_dir = project / "raw"
    raw_hits: list[dict] = []
    if raw_dir.is_dir():
        raw_hits = _search_raw_mdfind(raw_dir, query, top)
        if not raw_hits:
            raw_hits = _search_raw_ripgrep(raw_dir, query, top)
    return (wiki_hits + raw_hits)[:top * 2]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Local source search (wiki + raw) for deep-research",
    )
    parser.add_argument("query", help="Search query")
    parser.add_argument("--project", required=True, help="Path to wiki project root")
    parser.add_argument("--top", type=int, default=10, help="Max results per source (default 10)")
    args = parser.parse_args()

    project = Path(args.project).expanduser()
    results = search_local(project, args.query, top=args.top)

    if not results:
        print(f"No local results for: {args.query}", file=sys.stderr)
        return 1

    print(f"{len(results)} local result(s) for: {args.query}\n")
    for i, r in enumerate(results, 1):
        print(f"[{i}] **{r['title']}** ({r['source']})")
        if r.get("snippet"):
            print(r["snippet"])
        print(f"path: {r['path']}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
