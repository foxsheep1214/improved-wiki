#!/usr/bin/env python3
"""search_wiki.py — Hybrid search a wiki project (keyword + vector + RRF).

Port of NashSU ``search.rs`` hybrid retrieval (GAP-search):

  - keyword path: CJK bigram + weighted scoring (``_wiki_keyword``) — always
    runs, no dependencies, works offline.
  - vector path: LanceDB + configured embedding endpoint — over-fetches chunks,
    then applies NashSU 0.6.6 page aggregation (top + bounded tail × 0.3).
    On failure it reports the error and continues keyword-only, matching
    NashSU's optional-vector search behavior.
  - fusion: Reciprocal Rank Fusion (K=60) when both paths return; the richer
    keyword snippet wins on overlap.
  - graph: one-hop link neighbours of the top results take 15–30 % of the
    window (``_search_graph``, search.rs ``blend_graph_results``); they carry
    ``graph_related_to``.

Mode is reported: ``hybrid`` | ``keyword`` | ``vector`` (``hybrid`` whenever
graph neighbours were added, as in search.rs).

Usage:
  search_wiki.py "ADL8113" --project ~/Documents/知识库/HardwareWiki
  search_wiki.py "LC谐振" --project ~/path --top 10
  search_wiki.py "query" --project ~/path --keyword-only   # skip vector
"""
import argparse
import json
import os
import sys
from pathlib import Path

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from _frontmatter import MAX_REDIRECT_HOPS, redirect_target  # noqa: E402
from _paths import detect_runtime_dir  # noqa: E402
from _search_graph import GraphPages, blend_graph_results  # noqa: E402
from _wiki_keyword import (  # noqa: E402
    extract_title,
    keyword_search,
    page_snippet,
    rrf_merge,
    tokenize_query,
)


def _vector_search(query: str, runtime: Path, top: int, wiki_dir: Path):
    """Run the vector path. Returns (results, error_or_None).

    results: list of {path, title, snippet, score, vector_score}.
    On any failure (deps missing, Ollama down, lancedb missing/empty) returns
    ([], error) so the caller degrades to keyword-only. Rows whose page no
    longer exists are dropped with a warning pointing at ``sync``.
    """
    lancedb_dir = runtime / "lancedb"
    if not lancedb_dir.exists():
        return [], "lancedb index not found"
    try:
        import lancedb  # noqa: F401
    except ImportError:
        return [], "lancedb not installed (pip install lancedb)"

    try:
        from build_embeddings import (
            _aggregate_page_results,
            embed_with_config,
            embedding_config_from_env,
        )
        config = embedding_config_from_env()
        qvec = embed_with_config([query], config)[0]
    except Exception as e:
        return [], f"embedding API failed ({e})"

    try:
        import lancedb
        db = lancedb.connect(str(lancedb_dir))
        tbl = db.open_table("wiki_chunks")
        df = tbl.search(qvec).limit(max(top * 3, 30)).to_pandas()
    except Exception as e:
        return [], f"lancedb search failed ({e})"

    if df is None or df.empty:
        return [], None  # no vector hits, but no error

    exists = df["path"].map(lambda p: (wiki_dir / str(p)).is_file())
    if not exists.all():
        gone = sorted(set(df.loc[~exists, "page_id"].astype(str)))
        print(f"⚠️  vector index still holds {len(gone)} deleted page(s) in this "
              f"result window (e.g. {gone[0]}); dropped. Repair with: "
              f"build_embeddings.py --project {wiki_dir.parent} sync",
              file=sys.stderr)
        df = df[exists]
        if df.empty:
            return [], None

    results = []
    for page in _aggregate_page_results(df, top):
        matched = page.get("matched_chunks") or []
        best = matched[0] if matched else {}
        path = page.get("path") or f"{page.get('page_id', '')}.md"
        title = page.get("title") or best.get("heading_path", "") or ""
        snippet = str(best.get("chunk_text", ""))[:250].replace("\n", " ")
        score = float(page.get("score", 0.0))
        results.append({
            "path": str(path),
            "title": title,
            "snippet": snippet,
            "title_match": False,
            "score": score,
            "vector_score": score,
            "matched_chunks": matched,
        })
    return results, None


def _resolve_redirects(results: list[dict], wiki_dir: Path, query: str) -> list[dict]:
    """Replace dedup redirect stubs with the page they point to.

    A merged page leaves a one-line stub that still matches its old title,
    and iCloud conflict copies can double it, so stubs crowded the canonical
    page out of the window. Each stub becomes its target (first occurrence
    keeps the rank); later duplicates of a path are dropped.
    """
    phrase = query.strip().lower()
    tokens = tokenize_query(query)
    resolved: list[dict] = []
    seen: set[str] = set()
    for result in results:
        path = result["path"]
        try:
            content = (wiki_dir / path).read_text(encoding="utf-8")
        except OSError:
            content = ""
        origin = None
        for _ in range(MAX_REDIRECT_HOPS):
            target = redirect_target(wiki_dir, content) if content else None
            if target is None:
                break
            origin = origin or result["path"]
            path, content = target
        if origin is not None:
            result = dict(result)
            result.pop("matched_chunks", None)  # chunks of the stub, not the target
            result.update(
                path=path,
                title=extract_title(content, Path(path).name),
                snippet=page_snippet(content, phrase, tokens, query),
                redirected_from=origin,
            )
        if path in seen:
            continue
        seen.add(path)
        resolved.append(result)
    return resolved


def _warn_vector_unavailable(error: str, project: Path) -> None:
    """Surface vector failure before NashSU-style keyword-only continuation."""
    bar = "=" * 64
    print(bar, file=sys.stderr)
    print("⚠️  VECTOR SEARCH UNAVAILABLE — continuing keyword-only.", file=sys.stderr)
    print(f"   reason: {error}", file=sys.stderr)
    print("", file=sys.stderr)
    print("To enable hybrid (keyword + vector) search:", file=sys.stderr)
    print("  1. Start local Ollama:    ollama serve", file=sys.stderr)
    print("  2. Pull the embed model:  ollama pull bge-m3", file=sys.stderr)
    print("  3. Build the index:       build_embeddings.py "
          f"--project {project} embed", file=sys.stderr)
    print("Or set EMBEDDING_ENDPOINT / EMBEDDING_MODEL to a configured provider", file=sys.stderr)
    print("endpoint, then rebuild the index.", file=sys.stderr)
    print(bar, file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Hybrid wiki search (keyword + vector + RRF)")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--project", required=True, help="Path to wiki project root")
    parser.add_argument("--top", type=int, default=20, help="Max results (default: 20)")
    parser.add_argument("--keyword-only", action="store_true",
                        help="Skip the vector path (pure keyword, no Ollama needed)")
    parser.add_argument("--json", action="store_true",
                        help="Output results as a JSON array (machine-readable, for agent use)")
    args = parser.parse_args()

    project = Path(args.project).expanduser()
    runtime = detect_runtime_dir(project)
    wiki_dir = project / "wiki"

    # keyword path — always runs (offline, no deps); the same walk collects
    # the link graph for the one-hop expansion below
    graph_pages = GraphPages()
    kw_results = keyword_search(wiki_dir, args.query, max_results=args.top,
                                on_page=graph_pages.add)

    vec_results: list[dict] = []
    vec_error = None
    if not args.keyword_only:
        vec_results, vec_error = _vector_search(args.query, runtime, args.top,
                                                wiki_dir)
        if vec_error:
            _warn_vector_unavailable(vec_error, project)

    # decide mode + fuse (untruncated: redirect collapse can drop entries)
    if kw_results and vec_results:
        results = rrf_merge(kw_results, vec_results,
                            top=len(kw_results) + len(vec_results))
        mode = "hybrid"
    elif kw_results:
        results = kw_results
        mode = "keyword"
    elif vec_results:
        results = vec_results
        mode = "vector"
    else:
        if args.json:
            print("[]")
        else:
            print(f"No results for: {args.query}")
        return 1
    results = _resolve_redirects(results, wiki_dir, args.query)
    results, graph_hits = blend_graph_results(
        results, graph_pages, args.top, len(vec_results))
    if graph_hits:
        mode = "hybrid"

    if args.json:
        print(json.dumps(results, ensure_ascii=False))
        return 0

    print(f"{len(results)} result(s) for: {args.query}  [mode={mode}]\n")
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        title_str = f"  ({title})" if title else ""
        vscore = r.get("vector_score")
        vscore_str = f" vec={vscore:.3f}" if vscore is not None else ""
        print(f"{i}. [{r['score']:.3f}{vscore_str}] wiki/{r['path']}{title_str}")
        if r.get("redirected_from"):
            print(f"   (via redirect wiki/{r['redirected_from']})")
        if r.get("graph_related_to") and not r["snippet"].startswith("Graph neighbor"):
            print(f"   (graph neighbor of {', '.join(r['graph_related_to'])})")
        snippet = str(r.get("snippet", ""))[:250].replace("\n", " ")
        if snippet:
            print(f"   {snippet}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
