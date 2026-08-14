#!/usr/bin/env python3
"""Project-local search for Deep Research (NashSU AnyTXT CLI analogue).

NashSU v0.6.7 rewrites a topic into one to three compact AnyTXT queries,
searches them in order, deduplicates URL-first, and returns at most 15 local
results to Deep Research. This helper preserves that collection contract over
an improved-wiki project: ``wiki/`` uses the NashSU-style keyword scorer and
``raw/`` uses macOS Spotlight with a ripgrep sidecar fallback.

The backend is intentionally project-scoped rather than byte-identical to
AnyTXT. Its output does match the four-field ``WebSearchResult`` shape:

    {"title": ..., "url": "file:///...", "snippet": ..., "source": "AnyTXT"}

The calling agent should prepare one to three keyword-style queries before
invocation, mirroring NashSU's LLM query rewrite.

Usage:
    search_local.py "query one" "query two" --project <wiki-root> --json
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from _wiki_keyword import keyword_search


DEFAULT_LOCAL_RESULTS = 15
MAX_LOCAL_QUERIES = 3
_PDF_FALLBACK = "(PDF content match — open file to view context)"


def unique_local_queries(queries: str | list[str]) -> list[str]:
    """Trim, case-insensitively deduplicate, and keep at most three queries."""
    raw_queries = [queries] if isinstance(queries, str) else queries
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_queries:
        query = str(raw).strip().strip("\"'").strip()
        if not query:
            continue
        key = query.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(query)
        if len(out) >= MAX_LOCAL_QUERIES:
            break
    return out


def _file_url(path: Path) -> str:
    return path.resolve().as_uri()


def _result(path: Path, title: str, snippet: str) -> dict[str, str]:
    return {
        "title": title,
        "url": _file_url(path),
        "snippet": snippet,
        "source": "AnyTXT",
    }


def _search_wiki(project: Path, query: str, top: int) -> list[dict[str, str]]:
    """Reuse keyword_search over wiki/*.md and normalize to WebSearchResult."""
    wiki_dir = project / "wiki"
    if not wiki_dir.is_dir():
        return []
    out: list[dict[str, str]] = []
    for hit in keyword_search(wiki_dir, query, max_results=top):
        rel = hit.get("path", hit.get("file", ""))
        path = wiki_dir / rel if rel else wiki_dir
        title = hit.get("title", Path(rel).stem if rel else "")
        snippet = hit.get("snippet", hit.get("context", ""))
        out.append(_result(path, str(title), str(snippet)))
    return out


def _extract_pdf_snippet(pdf_path: Path, query: str, max_chars: int = 300) -> str:
    """Extract context from a PDF via pdftotext, like an AnyTXT match snippet."""
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        return _PDF_FALLBACK
    try:
        result = subprocess.run(
            [pdftotext, str(pdf_path), "-"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0 or not result.stdout:
            return _PDF_FALLBACK
        text = result.stdout
        tokens = [token for token in query.lower().split() if len(token) > 1]
        for token in tokens:
            index = text.lower().find(token)
            if index >= 0:
                start = max(0, index - 120)
                end = min(len(text), index + max_chars - 120)
                return text[start:end].replace("\n", " ").strip()[:max_chars]
        return _PDF_FALLBACK
    except Exception as exc:
        print(
            f"[search-local] pdftotext snippet failed for {pdf_path.name} "
            f"({type(exc).__name__}) — returning generic snippet",
            file=sys.stderr,
        )
        return _PDF_FALLBACK


def _extract_text_snippet(path: Path, query: str, max_chars: int = 300) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return f"(text match in {path.suffix} sidecar)"
    lowered = text.lower()
    anchors = [query.lower(), *[token for token in query.lower().split() if len(token) > 1]]
    index = next((lowered.find(anchor) for anchor in anchors if lowered.find(anchor) >= 0), -1)
    if index < 0:
        return text[:max_chars].replace("\n", " ").strip()
    start = max(0, index - 120)
    end = min(len(text), start + max_chars)
    return text[start:end].replace("\n", " ").strip()


def _search_raw_mdfind(raw_dir: Path, query: str, top: int) -> list[dict[str, str]]:
    """Search raw/ indexed content with macOS Spotlight."""
    mdfind = shutil.which("mdfind")
    if not mdfind:
        return []
    try:
        result = subprocess.run(
            [mdfind, "-onlyin", str(raw_dir), query],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []
        out: list[dict[str, str]] = []
        for raw_path in result.stdout.splitlines():
            path = Path(raw_path.strip())
            if not path.is_file():
                continue
            if path.suffix.lower() == ".pdf":
                snippet = _extract_pdf_snippet(path, query)
            else:
                snippet = _extract_text_snippet(path, query)
            out.append(_result(path, path.name, snippet))
            if len(out) >= top:
                break
        return out
    except Exception as exc:
        print(
            f"[search-local] mdfind failed ({type(exc).__name__}: {exc}) — "
            "falling back to ripgrep sidecar search",
            file=sys.stderr,
        )
        return []


def _search_raw_ripgrep(raw_dir: Path, query: str, top: int) -> list[dict[str, str]]:
    """Fallback over text sidecars because ripgrep cannot read PDF binaries."""
    rg = shutil.which("rg")
    if not rg:
        return []
    try:
        result = subprocess.run(
            [
                rg,
                "-l",
                "-i",
                "--max-count",
                "1",
                "-g",
                "*.txt",
                "-g",
                "*.md",
                "-g",
                "*.json",
                query,
                str(raw_dir),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode not in (0, 1):
            return []
        out: list[dict[str, str]] = []
        for raw_path in result.stdout.splitlines():
            path = Path(raw_path.strip())
            if not path.is_file():
                continue
            out.append(_result(path, path.name, _extract_text_snippet(path, query)))
            if len(out) >= top:
                break
        return out
    except Exception as exc:
        print(
            f"[search-local] ripgrep sidecar search failed "
            f"({type(exc).__name__}: {exc}) — raw/ results unavailable",
            file=sys.stderr,
        )
        return []


def _search_one_query(project: Path, query: str, top: int) -> list[dict[str, str]]:
    wiki_hits = _search_wiki(project, query, top)
    raw_dir = project / "raw"
    raw_hits: list[dict[str, str]] = []
    if raw_dir.is_dir():
        raw_hits = _search_raw_mdfind(raw_dir, query, top)
        if not raw_hits:
            raw_hits = _search_raw_ripgrep(raw_dir, query, top)
    return wiki_hits + raw_hits


def search_local(
    project: Path,
    query: str | list[str],
    top: int = DEFAULT_LOCAL_RESULTS,
) -> list[dict[str, str]]:
    """Search prepared queries in order, URL-dedup, and apply one global cap."""
    if top < 1:
        return []
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for prepared_query in unique_local_queries(query):
        for result in _search_one_query(project, prepared_query, top):
            key = (
                result.get("url")
                or f"{result.get('source', '')}:{result.get('title', '')}:"
                f"{result.get('snippet', '')}"
            ).lower()
            if key in seen:
                continue
            seen.add(key)
            results.append(result)
            if len(results) >= top:
                return results
    return results


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Project-local AnyTXT analogue for Deep Research",
    )
    parser.add_argument(
        "query",
        nargs="+",
        help="One to three prepared keyword-style queries (quote each query)",
    )
    parser.add_argument("--project", required=True, help="Path to wiki project root")
    parser.add_argument(
        "--max-results",
        "--top",
        dest="max_results",
        type=int,
        default=DEFAULT_LOCAL_RESULTS,
        help="Global result cap (default 15; --top is a compatibility alias)",
    )
    parser.add_argument("--json", action="store_true", help="Emit a JSON result array")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    project = Path(args.project).expanduser().resolve()
    results = search_local(project, args.query, top=args.max_results)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    if not results:
        print(f"No local results for: {', '.join(unique_local_queries(args.query))}")
        return 0

    print(f"{len(results)} local result(s)\n")
    for index, result in enumerate(results, 1):
        print(f"[{index}] **{result['title']}** ({result['source']})")
        if result.get("snippet"):
            print(result["snippet"])
        print(result["url"])
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
