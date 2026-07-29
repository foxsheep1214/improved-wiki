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
  (Vector dimension is always probed from the first returned embedding —
   there is no EMBEDDING_DIMENSIONS override.)

Commands:
  embed    — chunk all wiki pages + embed + write LanceDB, then compact/prune
  compact  — compact LanceDB and prune verified old versions
  search   — vector search LanceDB with a natural-language query
  stats    — show LanceDB table info

Usage:
  build_embeddings.py --project ~/Documents/知识库/HardwareWiki embed
  build_embeddings.py --project ~/Documents/知识库/HardwareWiki compact
  build_embeddings.py --project ~/Documents/知识库/HardwareWiki search --query "buck ringing"
  build_embeddings.py --project ~/Documents/知识库/HardwareWiki stats
"""
import os, sys, json, urllib.request, urllib.error, time, argparse, re, hashlib
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _frontmatter import TITLE_LINE_RE  # noqa: E402
from _paths import atomic_write  # noqa: E402
import lancedb


# ── Embedding backend ──────────────────────────────────────────────

def get_embed_config():
    """Resolve (base_url, model, api_key) from env.

    The vector dimension is NOT configured here: it is probed from the first
    embedding the endpoint returns (see cmd_embed's "Detected dims" line).
    The former EMBEDDING_DIMENSIONS env var was a no-op and has been removed
    (2026-07-12)."""
    base_url = os.environ.get("EMBEDDING_BASE_URL", "http://127.0.0.1:11434/v1")
    model = os.environ.get("EMBEDDING_MODEL", "bge-m3")
    api_key = os.environ.get("EMBEDDING_API_KEY", "")
    return base_url, model, api_key


def embed_texts(texts, base_url, model, api_key, timeout=120):
    """Call OpenAI-compatible /v1/embeddings. Returns list[list[float]].

    ``timeout`` is the per-request cap in seconds (default 120 preserves the
    historical behavior; _dedup_embedding passes its own bounded value)."""
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
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                    data = result.get("data", [])
                    vecs = [item.get("embedding") for item in data if item.get("embedding") is not None]
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
    sub.add_parser("embed", help="Chunk all pages + embed into LanceDB, then compact/prune")
    sub.add_parser("compact", help="Compact LanceDB and prune verified old versions")
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


_HEADING_LINE_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def chunk_spans(text, max_chars=1500, overlap=200):
    """Split text into (start, end) spans, markdown-aware.

    Spans rather than strings because each chunk's heading breadcrumb is
    resolved from its position (see heading_path_at).

    Boundary preference and the protected-range guard are shared with the
    Stage 2.2 book chunker (NashSU text-chunker.ts parity: never cut inside a
    fenced code block or a markdown table, prefer heading > paragraph >
    newline > sentence end). Only the window sizing differs — Stage 2.2 sizes
    to a token budget with a 2000-char floor, embeddings want ~1500 chars.

    The previous character-window split had no such guard, so a long datasheet
    parameter table was routinely cut between rows and the trailing half lost
    its header line.
    """
    from _stage_2_analyze import (
        _stage_2_1_find_protected_ranges,
        _stage_2_1_pick_boundary,
        _stage_2_1_snap_out,
    )

    text = re.sub(r"\n{3,}", "\n\n", text)
    n = len(text)
    if n <= max_chars:
        return [(0, n)] if text.strip() else []

    heading_positions = [m.start() for m in _HEADING_LINE_RE.finditer(text)]
    protected = _stage_2_1_find_protected_ranges(text)

    spans = []
    start = 0
    while start < n:
        end = min(start + max_chars, n)
        if end >= n:
            spans.append((start, n))
            break

        search_start = max(start, end - int(max_chars * 0.15))
        boundary = _stage_2_1_pick_boundary(
            text, search_start, end, heading_positions, protected)
        if boundary > start:
            end = boundary
        end = _stage_2_1_snap_out(start, end, protected)
        if end <= start:  # a protected block fills the window — let it overflow
            r = next((rng for rng in protected if rng[0] < start + 1 < rng[1]), None)
            end = r[1] if r else min(start + max_chars, n)

        spans.append((start, end))
        # The overlap start must clear protected blocks too. Backing up a fixed
        # number of characters from a chunk that ended just past a long table
        # lands mid-table, so the next chunk opened with headerless rows — the
        # very split this function exists to prevent.
        new_start = end - overlap
        containing = next(
            (r for r in protected if r[0] < new_start < r[1]), None)
        if containing is not None:
            new_start = containing[0]
        start = new_start if new_start > start else end

    return [(s, e) for s, e in spans if text[s:e].strip()]


def heading_path_at(text, pos):
    """Breadcrumb of the H1-H6 stack open at ``pos`` — "Page > Section > Sub".

    A page-scoped version of the Stage 2.2 resolver: wiki pages have no
    第N章/Chapter anchors, so the ancestor stack is the whole signal.
    """
    stack = []
    for m in _HEADING_LINE_RE.finditer(text):
        if m.start() > pos:
            break
        level = len(m.group(1))
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, m.group(2).strip()))
    return " > ".join(title for _level, title in stack)


def enrich_for_embedding(title, heading_path, chunk_text):
    """Text actually sent to the embedder: title + breadcrumb + chunk body.

    Port of NashSU enrichChunkForEmbedding (embedding.ts:299-308). The
    breadcrumb is the most valuable context a short chunk can carry — a
    300-char excerpt is far more findable when the embedded text names the
    page and section containing it. Measured on this project's own wikis, the
    page title was literally absent from 33% (HardwareWiki) and 46%
    (RadarWiki) of chunks, so those vectors carried no page identity at all.

    Only the vector sees this; LanceDB still stores the raw chunk_text, exactly
    as NashSU stores chunk.text rather than the enriched string.
    """
    parts = [p.strip() for p in (title, heading_path, chunk_text) if p and p.strip()]
    return "\n\n".join(parts)


def collect_pages():
    """Walk the wiki tree for knowledge pages to embed.

    Uses the shared walker rather than a hardcoded type list: a schema may
    define its own page folders (wiki/people/, wiki/protocols/, …), and a
    fixed whitelist silently left every page under them unembedded — the same
    bug already patched once by adding "methodology" to the list. The walker
    skips artifact dirs (lint/REVIEW/clusters/media) and SKIP_STEMS drops the
    app-managed aggregates.
    """
    from _paths import WIKI_ARTIFACT_DIRS, iter_wiki_pages

    pages = []
    for rel_path, content in iter_wiki_pages(
            Path(WIKI), skip_dirs=WIKI_ARTIFACT_DIRS):
        rel_path = rel_path.replace(os.sep, "/")
        stem = rel_path.rsplit("/", 1)[-1][:-3]
        if stem in SKIP_STEMS:
            continue
        # path-derived id: avoids collisions between same-named pages nested
        # under different type subdirs (e.g. two manufacturers' datasheets
        # both named "LM2596.md")
        page_id = rel_path[:-3]
        if content.startswith("---"):
            end = content.find("\n---", 3)
            body = content[end + 4:] if end != -1 else content
        else:
            body = content
        title = ""
        m = TITLE_LINE_RE.search(content)
        if m:
            title = m.group(1).strip()
        heading_match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        heading = heading_match.group(1) if heading_match else ""
        pages.append({
            "page_id": page_id,
            "path": rel_path,
            "title": title,
            "heading": heading,
            "body": body,
        })
    return pages


def build_chunks(pages):
    chunks = []
    for page in pages:
        body = page["body"]
        for idx, (s, e) in enumerate(chunk_spans(body, MAX_CHARS)):
            ctext = body[s:e]
            # Per-chunk breadcrumb. This used to be the page's first H1 copied
            # onto every chunk, which carried no section signal at all.
            hpath = heading_path_at(body, s) or page["heading"]
            embed_text = enrich_for_embedding(page["title"], hpath, ctext)
            # Hash the ENRICHED text: the cache is keyed by what is actually
            # embedded, so changing enrichment or chunking invalidates stale
            # vectors instead of silently serving them.
            sha = hashlib.sha256(embed_text.encode("utf-8")).hexdigest()[:16]
            chunks.append({
                "chunk_id": f"{page['page_id']}#{idx}",
                "page_id": page["page_id"],
                "chunk_index": idx,
                "chunk_text": ctext,
                "embed_text": embed_text,
                "heading_path": hpath,
                "title": page["title"],
                "path": page["path"],
                "text_sha16": sha,
            })
    return chunks


# ── Commands ────────────────────────────────────────────────────────

def _compact_and_prune_table(tbl):
    """Compact the current table and prune verified historical versions.

    Mirrors NashSU's post-rebuild maintenance contract: compact first, then
    remove every verified version older than the current snapshot. Keep
    ``delete_unverified=False`` so files that may belong to another live
    process are never force-deleted.
    """
    tbl.optimize(
        cleanup_older_than=timedelta(seconds=0),
        delete_unverified=False,
    )


def _best_effort_compact_and_prune(tbl):
    """Run post-rebuild maintenance without invalidating a good new index."""
    try:
        _compact_and_prune_table(tbl)
    except Exception as e:
        print(
            "⚠ LanceDB compact/prune failed after a successful rebuild "
            f"({type(e).__name__}: {e}). The current index remains usable; "
            "run the `compact` command later to reclaim old versions."
        )
        return False
    print("✓ LanceDB compacted; verified historical versions pruned")
    return True


def cmd_embed():
    pages = collect_pages()
    print(f"Pages: {len(pages)}")
    chunks = build_chunks(pages)
    print(f"Chunks: {len(chunks)}")

    cache = {}
    if os.path.exists(EMBED_CACHE):
        try:
            with open(EMBED_CACHE, encoding="utf-8") as f:
                cache = json.load(f)
        except (ValueError, OSError) as e:
            # Corrupt cache = full re-embed, not a crash. Loud so the operator
            # knows why this run suddenly embeds everything from scratch.
            print(f"⚠ embed-cache.json unreadable ({type(e).__name__}: {e}) — "
                  f"resetting to an empty cache; ALL chunks will re-embed.")
            cache = {}

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
            vecs = embed_texts([c["embed_text"] for c in sl], BASE_URL, MODEL, API_KEY)
            if vecs and dim is None:
                dim = len(vecs[0])
                print(f"  Detected dims: {dim}")
            for c, v in zip(sl, vecs):
                cache[c["text_sha16"]] = v
            atomic_write(EMBED_CACHE, json.dumps(cache))
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
    _best_effort_compact_and_prune(tbl)


def cmd_compact():
    """Compact the live table and prune old verified snapshots."""
    db = lancedb.connect(LANCE_DIR)
    try:
        tbl = db.open_table("wiki_chunks")
    except Exception as e:
        print(f"✗ Table not found: {e}. Run 'embed' first.")
        raise SystemExit(1)
    rows_before = tbl.count_rows()
    _compact_and_prune_table(tbl)
    rows_after = db.open_table("wiki_chunks").count_rows()
    if rows_after != rows_before:
        raise RuntimeError(
            "LanceDB row count changed during compact/prune: "
            f"{rows_before} → {rows_after}"
        )
    print(
        "✓ LanceDB compacted; verified historical versions pruned "
        f"({rows_after} rows preserved)"
    )


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
    global BASE_URL, MODEL, API_KEY
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
    BASE_URL, MODEL, API_KEY = get_embed_config()
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
    elif ARGS.command == "compact":
        cmd_compact()
    elif ARGS.command == "search":
        cmd_search()
    elif ARGS.command == "stats":
        cmd_stats()
