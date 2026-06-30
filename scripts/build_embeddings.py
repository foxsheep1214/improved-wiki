#!/usr/bin/env python3
"""
build_embeddings.py — wiki embedding engine (LanceDB + bge-m3)
Chunk wiki/ pages and embed them into LanceDB for semantic search. This is the
engine Stage 3.7 (_stage_3_7_embed.py) invokes after each ingest, and is runnable
standalone for a full-index (re)build. (NashSU-compatible embedding layout.)

Default backend: local Ollama with bge-m3 (1024d, best for Chinese).
Also works with any OpenAI-compatible /v1/embeddings endpoint.

Env vars:
  EMBEDDING_BASE_URL   — default http://127.0.0.1:11434/v1
  EMBEDDING_MODEL      — default bge-m3 (1024d). Use nomic-embed-text (768d) for English.
  EMBEDDING_API_KEY    — default "" (not needed for local Ollama)
  EMBEDDING_DIMENSIONS — auto-detected from first embedding if unset

Commands:
  embed   — chunk all wiki pages + embed + write LanceDB
  search  — vector search LanceDB with a natural-language query
  stats   — show LanceDB table info

Usage:
  build_embeddings.py --project ~/Documents/知识库/HardwareWiki embed
  build_embeddings.py --project ~/Documents/知识库/HardwareWiki search --query "buck ringing"
  build_embeddings.py --project ~/Documents/知识库/HardwareWiki stats
"""
import os, sys, json, urllib.request, urllib.error, time, argparse, re, hashlib
from pathlib import Path
import lancedb


# ── Embedding backend ──────────────────────────────────────────────

def get_embed_config():
    base_url = os.environ.get("EMBEDDING_BASE_URL", "http://127.0.0.1:11434/v1")
    model = os.environ.get("EMBEDDING_MODEL", "bge-m3")
    api_key = os.environ.get("EMBEDDING_API_KEY", "")
    dims_str = os.environ.get("EMBEDDING_DIMENSIONS", "")
    dims = int(dims_str) if dims_str else None
    return base_url, model, api_key, dims


def embed_texts(texts, base_url, model, api_key):
    """Call OpenAI-compatible /v1/embeddings. Returns list[list[float]]."""
    url = f"{base_url.rstrip('/')}/embeddings"
    out = []
    BATCH = 16
    for i in range(0, len(texts), BATCH):
        batch = texts[i:i + BATCH]
        payload = {"model": model, "input": batch}
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        for retry in range(3):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                    data = result.get("data", [])
                    vecs = [item["embedding"] for item in data]
                    out.extend(vecs)
                    break
            except Exception as e:
                if retry == 2:
                    print(f"  ✗ batch {i}-{i+len(batch)}: {e}")
                    raise
                time.sleep(2)
    return out


# ── CLI ─────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--project", default=os.environ.get("IMPROVED_WIKI_PROJECT"))
    p.add_argument("--max-chars", type=int, default=1500)
    p.add_argument("--model", default=None, help="Override EMBEDDING_MODEL")
    p.add_argument("--base-url", default=None, help="Override EMBEDDING_BASE_URL")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("embed", help="Chunk all pages + embed into LanceDB")
    s_search = sub.add_parser("search", help="Vector search LanceDB")
    s_search.add_argument("--query", required=True, help="Search query")
    s_search.add_argument("--top-k", type=int, default=5)
    sub.add_parser("stats", help="Show LanceDB table info")
    args = p.parse_args()
    if not args.project:
        p.error("--project required (or set IMPROVED_WIKI_PROJECT env)")
    return args


# CLI globals (ARGS/ROOT/WIKI/RUNTIME_DIR/LANCE_DIR/EMBED_CACHE/MAX_CHARS and the
# resolved embed config) are initialized by _init_cli() from the __main__ block
# below — NEVER at import time. Importing this module (e.g.
# `from build_embeddings import embed_texts` in _dedup_embedding.py) must not call
# parse_args(): doing so parsed the *parent* process's argv (the ingest book path)
# against the {embed,search,stats} subparser and crashed the whole pipeline with
# SystemExit(2) right after Stage 2.4. embed_texts() is a pure function and needs
# none of these globals.

# ── Chunking ────────────────────────────────────────────────────────

SKIP_STEMS = {"index", "log", "overview", "schema"}


def chunk_text(text, max_chars=1500, overlap=200):
    """Split text into chunks, preferring paragraph boundaries."""
    text = re.sub(r"\n{3,}", "\n\n", text)
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            last_nl = text.rfind("\n", start + max_chars - 200, end)
            if last_nl > start:
                end = last_nl
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def collect_pages():
    """Walk wiki/<type>/ subdirs for .md pages.

    wiki/sources/ mirrors raw/<type>/<any-subdir>/<file> (see
    naming-conventions.md), so it can nest arbitrarily deep
    (wiki/sources/Datasheet/ADI/ADL8113.md) — must walk recursively,
    not os.listdir() the top level only, or whole type folders go
    silently unembedded.
    """
    pages = []
    for sub in ["sources", "concepts", "entities", "queries", "comparisons",
                "findings", "synthesis", "thesis"]:
        d = f"{WIKI}/{sub}"
        if not os.path.exists(d):
            continue
        for dirpath, _dirnames, filenames in os.walk(d):
            for f in sorted(filenames):
                if not f.endswith(".md"):
                    continue
                stem = f[:-3]
                if stem in SKIP_STEMS:
                    continue
                path = os.path.join(dirpath, f)
                rel_path = os.path.relpath(path, WIKI)
                # path-derived id: avoids collisions between same-named
                # pages nested under different type subdirs (e.g. two
                # manufacturers' datasheets both named "LM2596.md")
                page_id = rel_path[:-3].replace(os.sep, "/")
                try:
                    content = open(path, encoding="utf-8").read()
                except Exception:
                    continue
                if content.startswith("---"):
                    end = content.find("\n---", 3)
                    body = content[end + 4:] if end != -1 else content
                else:
                    body = content
                title = ""
                m = re.search(r"^title:\s*[\"']?([^\"'\n]+)[\"']?", content, re.MULTILINE)
                if m:
                    title = m.group(1).strip()
                heading_match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
                heading = heading_match.group(1) if heading_match else ""
                pages.append({
                    "page_id": page_id,
                    "path": rel_path.replace(os.sep, "/"),
                    "title": title,
                    "heading": heading,
                    "body": body,
                })
    return pages


def build_chunks(pages):
    chunks = []
    for page in pages:
        cs = chunk_text(page["body"], MAX_CHARS)
        for idx, ctext in enumerate(cs):
            sha = hashlib.sha256(ctext.encode("utf-8")).hexdigest()[:16]
            chunks.append({
                "chunk_id": f"{page['page_id']}#{idx}",
                "page_id": page["page_id"],
                "chunk_index": idx,
                "chunk_text": ctext,
                "heading_path": page["heading"],
                "title": page["title"],
                "path": page["path"],
                "text_sha16": sha,
            })
    return chunks


# ── Commands ────────────────────────────────────────────────────────

def cmd_embed():
    pages = collect_pages()
    print(f"Pages: {len(pages)}")
    chunks = build_chunks(pages)
    print(f"Chunks: {len(chunks)}")

    cache = {}
    if os.path.exists(EMBED_CACHE):
        cache = json.load(open(EMBED_CACHE))

    to_embed = [c for c in chunks if c["text_sha16"] not in cache]
    print(f"To embed (uncached): {len(to_embed)}")

    if to_embed:
        t0 = time.time()
        # Persist the cache incrementally (every SAVE_EVERY chunks) instead of
        # once at the very end. cmd_embed embeds the ENTIRE uncached backlog of
        # the wiki, which for a large or freshly-backfilled wiki can exceed a
        # single run's wall clock (the Stage 3.7 caller enforces a cap, and
        # Ollama itself can stall). With a single trailing write, a kill mid-
        # backfill discarded every vector embedded so far and the next run
        # restarted from zero — the backfill could never converge. Saving per
        # slice means a crash loses at most one slice, and re-runs resume from
        # the saved cache. A fast incremental embed (only new pages uncached)
        # still does a single slice + save, so the common path is unchanged.
        SAVE_EVERY = 512
        dim = None
        done = 0
        for i in range(0, len(to_embed), SAVE_EVERY):
            sl = to_embed[i:i + SAVE_EVERY]
            vecs = embed_texts([c["chunk_text"] for c in sl], BASE_URL, MODEL, API_KEY)
            if vecs and dim is None:
                dim = len(vecs[0])
                print(f"  Detected dims: {dim}")
            for c, v in zip(sl, vecs):
                cache[c["text_sha16"]] = v
            with open(EMBED_CACHE, "w") as f:
                json.dump(cache, f)
            done += len(sl)
            print(f"  Embedded {done}/{len(to_embed)} (cache {len(cache)} entries)")
        print(f"  Embed time: {time.time() - t0:.1f}s")

    # Write LanceDB
    db = lancedb.connect(LANCE_DIR)
    data = []
    for c in chunks:
        vec = cache.get(c["text_sha16"])
        if vec is None:
            continue
        data.append({
            "chunk_id": c["chunk_id"],
            "page_id": c["page_id"],
            "chunk_index": c["chunk_index"],
            "chunk_text": c["chunk_text"],
            "heading_path": c["heading_path"] or "",
            "title": c["title"] or "",
            "path": c["path"],
            "vector": vec,
        })
    tbl = db.create_table("wiki_chunks", data, mode="overwrite")
    print(f"✓ LanceDB: {len(data)} chunks → {LANCE_DIR}/wiki_chunks")


def cmd_search():
    db = lancedb.connect(LANCE_DIR)
    try:
        tbl = db.open_table("wiki_chunks")
    except Exception as e:
        print(f"✗ Table not found: {e}. Run 'embed' first.")
        return

    qvecs = embed_texts([ARGS.query], BASE_URL, MODEL, API_KEY)
    if not qvecs:
        print("✗ Query embedding failed")
        return

    results = tbl.search(qvecs[0]).limit(ARGS.top_k).to_pandas()
    print(f"\n=== Top {ARGS.top_k} semantic matches ===\n")
    for i, row in results.iterrows():
        dist = row.get("_distance", 0)
        sim = 1.0 / (1.0 + float(dist))
        print(f"#{i + 1}  [{row['path']}]  {row['title']}")
        print(f"     similarity={sim:.3f}")
        snippet = str(row["chunk_text"])[:200].replace("\n", " ")
        print(f"     {snippet}...\n")


def cmd_stats():
    db = lancedb.connect(LANCE_DIR)
    try:
        tbl = db.open_table("wiki_chunks")
        n = tbl.count_rows()
        print(f"LanceDB table: {len(tbl.schema.names)} columns, {n} rows")
        for name in tbl.schema.names[:10]:
            print(f"  - {name}: {tbl.schema.field(name).type}")
    except Exception as e:
        print(f"✗ Table not found: {e}")


def _init_cli():
    """Parse CLI args + initialize embed globals. Call ONLY from __main__ — not at
    import time (see the note above the chunking section)."""
    global ARGS, ROOT, WIKI, RUNTIME_DIR, LANCE_DIR, EMBED_CACHE, MAX_CHARS
    global BASE_URL, MODEL, API_KEY, DIMS
    ARGS = parse_args()
    ROOT = ARGS.project
    WIKI = f"{ROOT}/wiki"
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    if _script_dir not in sys.path:
        sys.path.insert(0, _script_dir)
    from _paths import detect_runtime_dir
    RUNTIME_DIR = str(detect_runtime_dir(Path(ROOT)))
    LANCE_DIR = f"{RUNTIME_DIR}/lancedb"
    EMBED_CACHE = f"{RUNTIME_DIR}/embed-cache.json"
    MAX_CHARS = ARGS.max_chars
    BASE_URL, MODEL, API_KEY, DIMS = get_embed_config()
    if ARGS.model:
        MODEL = ARGS.model
    if ARGS.base_url:
        BASE_URL = ARGS.base_url
    os.makedirs(LANCE_DIR, exist_ok=True)
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    print(f"[embedding] backend: {BASE_URL}  model: {MODEL}")


if __name__ == "__main__":
    _init_cli()
    if ARGS.command == "embed":
        cmd_embed()
    elif ARGS.command == "search":
        cmd_search()
    elif ARGS.command == "stats":
        cmd_stats()
