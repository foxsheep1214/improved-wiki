#!/usr/bin/env python3
"""search_wiki.py — Hybrid search a wiki project (keyword + vector + RRF).

Port of NashSU ``search.rs`` hybrid retrieval (GAP-search):

  - keyword path: CJK bigram + weighted scoring (``_wiki_keyword``) — always
    runs, no dependencies, works offline.
  - vector path: LanceDB + local Ollama bge-m3 — runs when available; on any
    failure (Ollama down, lancedb missing, package absent) it is skipped and
    search degrades to keyword-only instead of erroring.
  - fusion: Reciprocal Rank Fusion (K=60) when both paths return; the richer
    keyword snippet wins on overlap.

Mode is reported: ``hybrid`` | ``keyword`` | ``vector``.

Usage:
  search_wiki.py "ADL8113" --project ~/Documents/知识库/HardwareWiki
  search_wiki.py "LC谐振" --project ~/path --top 10
  search_wiki.py "query" --project ~/path --keyword-only   # skip vector
"""
import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from _paths import detect_runtime_dir  # noqa: E402
from _wiki_keyword import keyword_search, rrf_merge  # noqa: E402


def _vector_search(query: str, project: Path, runtime: Path, top: int):
    """Run the vector path. Returns (results, error_or_None).

    results: list of {path, title, snippet, score, vector_score}.
    On any failure (deps missing, Ollama down, lancedb missing/empty) returns
    ([], error) so the caller degrades to keyword-only.
    """
    lancedb_dir = runtime / "lancedb"
    if not lancedb_dir.exists():
        return [], "lancedb index not found"
    try:
        import lancedb  # noqa: F401
    except ImportError:
        return [], "lancedb not installed (pip install lancedb)"

    base_url = os.environ.get("EMBEDDING_BASE_URL", "http://127.0.0.1:11434/v1")
    model = os.environ.get("EMBEDDING_MODEL", "bge-m3")
    api_key = os.environ.get("EMBEDDING_API_KEY", "")

    body = json.dumps({"model": model, "input": [query]}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = f"{base_url.rstrip('/')}/embeddings"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        qvec = data["data"][0]["embedding"]
    except Exception as e:
        return [], f"embedding API failed ({e})"

    try:
        import lancedb
        db = lancedb.connect(str(lancedb_dir))
        tbl = db.open_table("wiki_chunks")
        df = tbl.search(qvec).limit(top).to_pandas()
    except Exception as e:
        return [], f"lancedb search failed ({e})"

    if df is None or df.empty:
        return [], None  # no vector hits, but no error

    results = []
    for _, row in df.iterrows():
        dist = float(row.get("_distance", 0))
        sim = 1.0 / (1.0 + dist)
        title = row.get("title", "") or row.get("heading_path", "") or ""
        snippet = str(row.get("chunk_text", ""))[:250].replace("\n", " ")
        results.append({
            "path": str(row.get("path", "")),
            "title": title,
            "snippet": snippet,
            "title_match": False,
            "score": sim,
            "vector_score": sim,
        })
    return results, None


def _warn_vector_unavailable(error: str, project: Path) -> None:
    """Emit a prominent warning with remediation steps when the vector path
    can't be used. Goes to stderr. Per skill policy the caller then PAUSES —
    no silent keyword-only fallback."""
    bar = "=" * 64
    print(bar, file=sys.stderr)
    print("⚠️  VECTOR SEARCH UNAVAILABLE — hybrid search cannot run.", file=sys.stderr)
    print(f"   reason: {error}", file=sys.stderr)
    print("", file=sys.stderr)
    print("To enable hybrid (keyword + vector) search:", file=sys.stderr)
    print("  1. Start local Ollama:    ollama serve", file=sys.stderr)
    print("  2. Pull the embed model:  ollama pull bge-m3", file=sys.stderr)
    print("  3. Build the index:       build_embeddings.py "
          f"--project {project} embed", file=sys.stderr)
    print("Or set EMBEDDING_BASE_URL / EMBEDDING_MODEL to an OpenAI-compatible", file=sys.stderr)
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

    # keyword path — always runs (offline, no deps)
    kw_results = keyword_search(wiki_dir, args.query, max_results=args.top)

    vec_results: list[dict] = []
    vec_error = None
    if not args.keyword_only:
        vec_results, vec_error = _vector_search(args.query, project, runtime, args.top)
        if vec_error:
            # Global skill policy: on alert, PAUSE — do not auto-degrade. Emit
            # the warning + remediation steps and stop. The user must either fix
            # the vector path or explicitly re-run with --keyword-only. Keyword
            # results are NOT returned as a silent fallback.
            _warn_vector_unavailable(vec_error, project)
            print("\nPaused. Fix the vector path and re-run, or use --keyword-only "
                  "to explicitly search without vectors.", file=sys.stderr)
            return 1

    # decide mode + fuse
    if kw_results and vec_results:
        results = rrf_merge(kw_results, vec_results, top=args.top)
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
        snippet = str(r.get("snippet", ""))[:250].replace("\n", " ")
        if snippet:
            print(f"   {snippet}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
