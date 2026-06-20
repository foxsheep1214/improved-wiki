#!/usr/bin/env python3
"""
graph.py — the Graph command of improved-wiki (knowledge graph build).

The Graph command is a peer of Ingest and Lint — a separate phase, not part
of lint. It is the CLI equivalent of NashSU's desktop graph-view
(`src/lib/wiki-graph.ts` + `graph-relevance.ts` + `graph-insights.ts`):
builds a weighted undirected graph from wiki pages using NashSU's four-signal
model, runs Louvain community detection to discover knowledge clusters,
computes cohesion scores, and exports graph data for visualization.

Deterministic — no LLM calls (unlike Ingest text-gen / Lint semantic). Pure
networkx + python-louvain computation.

Four signals (NashSU v0.4.25 parity):
  1. Direct link     (×3.0)  [[wikilinks]] between pages
  2. Source overlap  (×4.0)  pages citing the same raw source
  3. Adamic-Adar     (×1.5)  common neighbors / log(neighbor degree)
  4. Type affinity   (×1.0)  TYPE_AFFINITY matrix (entity↔concept 1.2, …)

Outputs (written to <state_dir>/):
  graph.json          Full graph: nodes, edges, communities, cohesion — for
                      LLM Wiki desktop app frontend visualization.
  knowledge-gaps.md   Missing links, isolated nodes, low-cohesion clusters,
                      bridge nodes between distant communities.
  clusters/           One hub page per Louvain community with key concepts,
                      suggested cross-links, and cohesion score.

Run it periodically after batch ingests to audit the whole knowledge base at
once. For per-book suggestions during ingest, use the lightweight read-only
query mode (--mode query). ingest.py can also auto-trigger it post-ingest
behind AUTO_BUILD_GRAPH=1 (30-min staleness guard).

Usage:
  ./graph.py                          # full build + outputs
  ./graph.py --mode query --slug <s>  # read-only: suggest wikilinks for a
                                         # single new page
  ./graph.py --dry-run                # print stats, skip write
  ./graph.py --min-cohesion 0.12      # lower warning threshold

Dependencies:
  pip install networkx python-louvain pyyaml
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

# ── constants ────────────────────────────────────────────────────────────────
# Signal weights (NashSU parity)
W_DIRECT_LINK = 3.0
W_SOURCE_OVERLAP = 4.0
W_ADAMIC_ADAR = 1.5
W_TYPE_AFFINITY = 1.0

# Cohesion warning threshold (NashSU parity: < 0.15 flagged)
COHESION_WARN = 0.15

# State files to exclude
EXCLUDE_NAMES = {
    "index.md", "overview.md", "log.md", "schema.md",
    "lint-cache.json", "lint.json", "lint-semantic.json",
    "ingest-cache.json", "ingest-queue.json", "ingest-lock",
}

# Wiki page type → directory mapping (NashSU parity)
TYPE_DIRS = {
    "source": "sources",
    "concept": "concepts",
    "entity": "entities",
    "query": "queries",
    "comparison": "comparisons",
    "synthesis": "synthesis",
    "finding": "findings",
    "thesis": "thesis",
}

# ── YAML frontmatter extraction ─────────────────────────────────────────────
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _parse_frontmatter(text: str) -> dict:
    """Extract YAML frontmatter as a dict. Returns {} on parse failure."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    try:
        import yaml
        return yaml.safe_load(m.group(1)) or {}
    except Exception:
        return {}


def _extract_wikilinks(text: str) -> list[str]:
    """Extract [[wikilink]] targets, normalizing to stem form (no .md)."""
    targets = []
    for m in _WIKILINK_RE.finditer(text):
        target = m.group(1).strip()
        # Remove .md extension if present
        if target.endswith(".md"):
            target = target[:-3]
        # Remove anchor (#section)
        if "#" in target:
            target = target[: target.index("#")]
        if target:
            targets.append(target)
    return targets


def _resolve_page_path(stem: str, wiki_dir: Path) -> Optional[Path]:
    """Given a wikilink stem, find the matching .md file under wiki/."""
    # Try direct stem match first
    for subdir in list(TYPE_DIRS.values()) + ["media", "REVIEW", "lint"]:
        candidate = wiki_dir / subdir / f"{stem}.md"
        if candidate.exists():
            return candidate
    # Fallback: full recursive search (expensive, only for unmatched links)
    for path in wiki_dir.rglob(f"{stem}.md"):
        return path
    return None


# ── node/edge extraction ─────────────────────────────────────────────────────
def _parse_wiki_pages(wiki_dir: Path) -> dict[str, dict]:
    """Walk wiki/ and return {stem: {type, domain, sources, wikilinks, path}}.

    Stem = relative path without .md (e.g., 'concepts/adum3165').
    """
    pages: dict[str, dict] = {}
    for md_path in sorted(wiki_dir.rglob("*.md")):
        rel = md_path.relative_to(wiki_dir)
        if rel.name in EXCLUDE_NAMES:
            continue
        # Skip non-content dirs
        parts = rel.parts
        if parts[0] not in TYPE_DIRS.values() and parts[0] not in (
            "media", "REVIEW", "lint"
        ):
            continue

        try:
            text = md_path.read_text(encoding="utf-8")
        except Exception:
            continue

        stem = str(rel.with_suffix(""))

        fm = _parse_frontmatter(text)
        sources_raw = fm.get("sources") or fm.get("source") or []
        if isinstance(sources_raw, str):
            sources_raw = [sources_raw]
        wikilinks = _extract_wikilinks(text)

        pages[stem] = {
            "type": fm.get("type", "concept"),
            "domain": fm.get("domain", "general"),
            "tags": fm.get("tags", []),
            "related": fm.get("related", []),
            "sources": [str(s) for s in sources_raw],
            "wikilinks": wikilinks,
            "title": fm.get("title", stem.rsplit("/", 1)[-1]),
            "path": str(rel),
        }
    return pages


# ── graph construction ───────────────────────────────────────────────────────
def _build_graph(pages: dict[str, dict]) -> "nx.Graph":
    """Build weighted undirected graph from parsed pages."""
    import networkx as nx

    G = nx.Graph()
    stems = list(pages.keys())

    # Add nodes
    for stem, info in pages.items():
        G.add_node(stem, **info)

    # Index: raw source → list of stems
    source_to_stems: dict[str, list[str]] = defaultdict(list)
    for stem, info in pages.items():
        for src in info["sources"]:
            source_to_stems[src].append(stem)

    # Signal 1: Direct links ([[wikilinks]])
    for stem, info in pages.items():
        for target in info["wikilinks"]:
            if target in pages:
                if G.has_edge(stem, target):
                    G[stem][target]["weight"] += W_DIRECT_LINK
                else:
                    G.add_edge(stem, target, weight=W_DIRECT_LINK)

    # Signal 2: Source overlap
    for src, sharing_stems in source_to_stems.items():
        for i in range(len(sharing_stems)):
            for j in range(i + 1, len(sharing_stems)):
                a, b = sharing_stems[i], sharing_stems[j]
                if G.has_edge(a, b):
                    G[a][b]["weight"] += W_SOURCE_OVERLAP
                else:
                    G.add_edge(a, b, weight=W_SOURCE_OVERLAP)

    # Signal 3: Adamic-Adar (common neighbors / log(neighbor degree))
    # Performance: only refine existing edges (from signals 1+2), not all O(n²) pairs.
    # For a 7500+ page wiki this runs in seconds instead of hours.
    degrees = dict(G.degree())
    for u, v in list(G.edges()):
        neighbors_u = set(G.neighbors(u))
        neighbors_v = set(G.neighbors(v))
        common = neighbors_u & neighbors_v
        if not common:
            continue
        aa_score = sum(
            1.0 / math.log(max(degrees.get(n, 2), 2))
            for n in common
        )
        if aa_score > 0:
            G[u][v]["weight"] += W_ADAMIC_ADAR * aa_score

    # Signal 4: Type affinity (same type + same domain)
    # Only refine existing edges — full O(n²) would be prohibitive for >5000 pages.
    for u, v in list(G.edges()):
        info_u, info_v = pages[u], pages[v]
        affinity = 0.0
        if info_u["type"] == info_v["type"]:
            affinity += 0.6
        if info_u["domain"] == info_v["domain"] and info_u["domain"] != "general":
            affinity += 0.4
        if affinity > 0:
            G[u][v]["weight"] += W_TYPE_AFFINITY * affinity

    return G


# ── Louvain + analysis ───────────────────────────────────────────────────────
def _run_louvain(G: "nx.Graph") -> dict[str, int]:
    """Run Louvain community detection on the graph."""
    from community import community_louvain

    # Louvain expects positive weights; our graph may have very small edges.
    # Build an unweighted copy for the algorithm if needed.
    return community_louvain.best_partition(G, weight="weight")


def _compute_cohesion(G: "nx.Graph", partition: dict[str, int]) -> dict[int, dict]:
    """Compute per-community cohesion scores.

    Cohesion = actual intra-community edges / possible intra-community edges.
    """
    communities: dict[int, list[str]] = defaultdict(list)
    for node, comm in partition.items():
        communities[comm].append(node)

    results = {}
    for comm, nodes in communities.items():
        n = len(nodes)
        if n <= 1:
            results[comm] = {
                "size": n,
                "cohesion": 1.0 if n == 1 else 0.0,
                "nodes": nodes,
                "warning": False,
            }
            continue

        node_set = set(nodes)
        actual = sum(
            1 for u, v in G.edges(node_set) if u in node_set and v in node_set
        )
        possible = n * (n - 1) / 2
        cohesion = actual / possible if possible > 0 else 0.0

        results[comm] = {
            "size": n,
            "cohesion": round(cohesion, 4),
            "nodes": nodes,
            "warning": cohesion < COHESION_WARN,
        }
    return results


def _find_bridges(
    G: "nx.Graph", partition: dict[str, int]
) -> list[dict]:
    """Find bridge nodes that connect different communities."""
    bridge_nodes = []
    for node in G.nodes():
        neighbor_comms = set()
        for neighbor in G.neighbors(node):
            if neighbor in partition:
                neighbor_comms.add(partition[neighbor])
        if len(neighbor_comms) >= 2:
            node_comm = partition.get(node)
            other_comms = neighbor_comms - {node_comm}
            bridge_nodes.append({
                "node": node,
                "community": node_comm,
                "bridges_to": sorted(other_comms),
                "neighbor_count": len(list(G.neighbors(node))),
            })
    return sorted(bridge_nodes, key=lambda b: b["neighbor_count"], reverse=True)


# ── output generators ────────────────────────────────────────────────────────
def _write_graph_json(
    G: "nx.Graph",
    partition: dict[str, int],
    cohesion: dict[int, dict],
    out_path: Path,
) -> None:
    """Export full graph data as JSON for LLM Wiki frontend."""
    nodes = []
    for node, data in G.nodes(data=True):
        nodes.append({
            "id": node,
            "type": data.get("type", "concept"),
            "domain": data.get("domain", "general"),
            "title": data.get("title", node),
            "path": data.get("path", ""),
            "community": partition.get(node),
            "degree": G.degree(node),
        })

    edges = []
    for u, v, data in G.edges(data=True):
        edges.append({
            "source": u,
            "target": v,
            "weight": round(data.get("weight", 0), 2),
        })

    communities = []
    for comm_id, info in sorted(cohesion.items()):
        communities.append({
            "id": comm_id,
            "size": info["size"],
            "cohesion": info["cohesion"],
            "warning": info.get("warning", False),
        })

    output = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "community_count": len(communities),
        "nodes": nodes,
        "edges": edges,
        "communities": communities,
    }

    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out_path)
    print(f"[graph] Wrote {out_path} ({len(nodes)} nodes, {len(edges)} edges)")


def _write_gaps_report(
    G: "nx.Graph",
    partition: dict[str, int],
    cohesion: dict[int, dict],
    bridges: list[dict],
    pages: dict[str, dict],
    out_path: Path,
) -> None:
    """Generate knowledge-gaps.md report."""
    # Isolated nodes (degree 0)
    isolated = [n for n, d in G.degree() if d == 0]

    # Low-cohesion communities
    low_coh = [
        (cid, info)
        for cid, info in sorted(cohesion.items())
        if info.get("warning") and info["size"] > 2
    ]

    # Top bridge nodes
    top_bridges = bridges[:20]

    # Missing links: high-degree unconnected pairs in same community
    missing_links = []
    for comm_id, info in cohesion.items():
        nodes = info["nodes"]
        if len(nodes) < 2:
            continue
        # Find pairs in same community that share sources but no edge
        node_set = set(nodes)
        checked = set()
        for a in nodes:
            a_sources = set(pages.get(a, {}).get("sources", []))
            for b in nodes:
                if a >= b or (a, b) in checked:
                    continue
                checked.add((a, b))
                if G.has_edge(a, b):
                    continue
                b_sources = set(pages.get(b, {}).get("sources", []))
                common = a_sources & b_sources
                if common:
                    missing_links.append({
                        "a": a,
                        "b": b,
                        "shared_sources": sorted(common),
                        "community": comm_id,
                    })

    lines = [
        "# Knowledge Graph Gaps Report",
        f"",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Total nodes: {G.number_of_nodes()}",
        f"Total edges: {G.number_of_edges()}",
        f"Communities: {len(cohesion)}",
        f"Isolated nodes: {len(isolated)}",
        f"Low-cohesion communities: {len(low_coh)}",
        f"Bridge nodes: {len(bridges)}",
        f"Suggested missing links: {len(missing_links)}",
        f"",
    ]

    if isolated:
        lines.append("## Isolated Nodes")
        lines.append("")
        lines.append("These pages have no connections. Consider adding wikilinks or related sources.")
        lines.append("")
        for node in sorted(isolated):
            info = pages.get(node, {})
            lines.append(
                f"- [[{node}]] ({info.get('type', '?')}, "
                f"domain={info.get('domain', '?')})"
            )
        lines.append("")

    if low_coh:
        lines.append("## Low-Cohesion Communities")
        lines.append("")
        lines.append(
            f"Communities with cohesion < {COHESION_WARN}. "
            f"Consider adding cross-links or reviewing cluster boundaries."
        )
        lines.append("")
        for cid, info in low_coh:
            lines.append(
                f"- Community {cid}: {info['size']} nodes, "
                f"cohesion={info['cohesion']:.3f}"
            )
        lines.append("")

    if top_bridges:
        lines.append("## Bridge Nodes (Top 20)")
        lines.append("")
        lines.append("These nodes connect disparate communities. They are good candidates for synthesis or hub pages.")
        lines.append("")
        for b in top_bridges:
            lines.append(
                f"- [[{b['node']}]] bridges communities {b['bridges_to']} "
                f"({b['neighbor_count']} neighbors)"
            )
        lines.append("")

    if missing_links[:30]:
        lines.append("## Suggested Missing Links (Top 30)")
        lines.append("")
        lines.append("Pages in the same community, sharing sources, but not linked.")
        lines.append("")
        for ml in missing_links[:30]:
            srcs = ", ".join(ml["shared_sources"][:2])
            lines.append(f"- [[{ml['a']}]] ↔ [[{ml['b']}]] (shared sources: {srcs})")
        lines.append("")

    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(out_path)
    print(f"[graph] Wrote {out_path}")


def _write_cluster_hubs(
    G: "nx.Graph",
    partition: dict[str, int],
    cohesion: dict[int, dict],
    pages: dict[str, dict],
    out_dir: Path,
) -> None:
    """Generate one hub page per Louvain community."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # Find top concepts per community (by degree)
    communities: dict[int, list[str]] = defaultdict(list)
    for node, comm in partition.items():
        communities[comm].append(node)

    written = 0
    for comm_id, nodes in sorted(communities.items()):
        info = cohesion.get(comm_id, {"size": len(nodes), "cohesion": 0.0})
        if info["size"] < 3:
            continue  # skip tiny clusters

        # Top nodes by degree
        ranked = sorted(nodes, key=lambda n: G.degree(n), reverse=True)
        top_n = ranked[: min(15, len(ranked))]

        # Find dominant type and domain
        types = Counter(pages.get(n, {}).get("type", "concept") for n in nodes)
        domains = Counter(pages.get(n, {}).get("domain", "general") for n in nodes)
        dominant_type = types.most_common(1)[0][0]
        dominant_domain = domains.most_common(1)[0][0]

        # Find tags
        all_tags = Counter()
        for n in nodes:
            for t in pages.get(n, {}).get("tags", []):
                all_tags[t] += 1
        top_tags = [t for t, _ in all_tags.most_common(10)]

        # Hub title from dominant domain + type
        hub_title = f"Cluster {comm_id}: {dominant_domain} {dominant_type}s"

        lines = [
            "---",
            f"type: synthesis",
            f"title: \"{hub_title}\"",
            f"domain: {dominant_domain}",
            f"cluster_id: {comm_id}",
            f"cluster_size: {info['size']}",
            f"cluster_cohesion: {info['cohesion']}",
            f"dominant_type: {dominant_type}",
            f"generated: {time.strftime('%Y-%m-%d')}",
            "auto_generated: true",
            "---",
            "",
            f"# {hub_title}",
            "",
            f"- **Size**: {info['size']} pages",
            f"- **Cohesion**: {info['cohesion']:.3f}"
            + (" ⚠️ Low cohesion" if info.get("warning") else ""),
            f"- **Dominant Type**: {dominant_type}",
            f"- **Dominant Domain**: {dominant_domain}",
            "",
            f"## Key Topics",
            "",
        ]
        for tag in top_tags[:8]:
            lines.append(f"- {tag}")

        lines += [
            "",
            "## Core Pages",
            "",
        ]
        for node in top_n:
            node_info = pages.get(node, {})
            node_type = node_info.get("type", "?")
            node_title = node_info.get("title", node)
            deg = G.degree(node)
            lines.append(
                f"- [[{node}]] ({node_type}, degree={deg}) — {node_title}"
            )

        lines += [
            "",
            "## All Pages",
            "",
        ]
        for node in sorted(nodes):
            lines.append(f"- [[{node}]]")

        lines += [
            "",
            "## Suggested Actions",
            "",
        ]
        if info.get("warning"):
            lines.append(
                "- ⚠️ This cluster has low cohesion. "
                "Consider adding more internal wikilinks."
            )
        if len(nodes) >= 5 and info["cohesion"] < 0.3:
            lines.append(
                "- Consider splitting this cluster or adding a hub page "
                "to improve navigability."
            )
        if len(nodes) < 5:
            lines.append(
                "- This is a small cluster. Check if it should merge with a "
                "neighboring community."
            )
        lines.append(
            "- Regenerate this report by running `graph.py` "
            "after significant ingest batches."
        )

        stem = f"cluster-{comm_id:03d}"
        out_file = out_dir / f"{stem}.md"
        tmp = out_file.with_suffix(out_file.suffix + ".tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        tmp.replace(out_file)
        written += 1

    print(f"[graph] Wrote {written} cluster hub pages → {out_dir}")


# ── query mode (lightweight, for ingest use) ─────────────────────────────────
def _query_mode(wiki_dir: Path, slug: str) -> int:
    """Read-only: suggest wikilinks for a single new page without rebuilding
    the full graph. Returns 0 on success."""
    pages = _parse_wiki_pages(wiki_dir)
    G = _build_graph(pages)

    # Find the target page
    target_stem = None
    for stem in pages:
        if stem.endswith(slug) or slug in stem:
            target_stem = stem
            break

    if not target_stem:
        # Try as a direct path
        md_path = wiki_dir / f"{slug}.md"
        if md_path.exists():
            # Parse just this one page and add to graph
            text = md_path.read_text(encoding="utf-8")
            fm = _parse_frontmatter(text)
            sources_raw = fm.get("sources") or fm.get("source") or []
            if isinstance(sources_raw, str):
                sources_raw = [sources_raw]
            wikilinks = _extract_wikilinks(text)
            # Find source-sharing candidates
            candidates = set()
            for src in sources_raw:
                for stem, info in pages.items():
                    if src in info.get("sources", []):
                        candidates.add(stem)
            # Remove existing links
            existing = set(wikilinks)
            suggestions = sorted(candidates - existing)
            if suggestions:
                print(f"[query] Suggested wikilinks for '{slug}':")
                for s in suggestions[:20]:
                    info = pages.get(s, {})
                    print(
                        f"  - [[{s}]] "
                        f"({info.get('type', '?')}, "
                        f"domain={info.get('domain', '?')})"
                    )
            else:
                print(f"[query] No new wikilink suggestions for '{slug}'")
            return 0
        print(f"[query] Page '{slug}' not found in wiki", file=sys.stderr)
        return 1

    # Target exists in graph — find nearest neighbors
    if target_stem in G:
        neighbors = sorted(
            G.neighbors(target_stem),
            key=lambda n: G[target_stem][n].get("weight", 0),
            reverse=True,
        )
        target_info = pages.get(target_stem, {})
        existing_links = set(target_info.get("wikilinks", []))
        suggestions = [n for n in neighbors if n not in existing_links]

        print(
            f"[query] Graph neighbors for '{target_stem}' "
            f"(community={G.nodes[target_stem].get('community', '?')}):"
        )
        for n in suggestions[:20]:
            weight = G[target_stem][n].get("weight", 0)
            info = pages.get(n, {})
            print(
                f"  - [[{n}]] "
                f"(weight={weight:.1f}, {info.get('type', '?')}, "
                f"domain={info.get('domain', '?')})"
            )

    return 0


# ── main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode", choices=["full", "query"], default="full",
        help="full: build graph + all outputs; query: suggest wikilinks for a page",
    )
    parser.add_argument(
        "--slug", type=str, default=None,
        help="Page slug for query mode",
    )
    parser.add_argument(
        "--min-cohesion", type=float, default=COHESION_WARN,
        help=f"Cohesion warning threshold (default: {COHESION_WARN})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print stats, skip writing output files",
    )
    parser.add_argument(
        "--output-dir", type=str, default=None,
        help="Override output directory (default: <state_dir>)",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Cap number of pages parsed (for testing on large wikis)",
    )
    args = parser.parse_args()

    root = Path(os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd()))
    wiki_dir = root / "wiki"
    if not wiki_dir.is_dir():
        print(f"ERROR: wiki/ not found under {root}", file=sys.stderr)
        return 2

    # State dir resolution
    _script_root = Path(__file__).resolve().parent
    sys.path.insert(0, str(_script_root))
    from _paths import detect_runtime_dir

    state_dir = detect_runtime_dir(root) if not args.output_dir else Path(args.output_dir)
    if args.output_dir:
        out_base = Path(args.output_dir)
    else:
        out_base = state_dir

    # Override module-level threshold (used by _compute_cohesion)
    import builtins
    cohesion_warn = args.min_cohesion
    # Rebind the module-level constant for _compute_cohesion
    globals()["COHESION_WARN"] = cohesion_warn

    # ── Query mode (read-only, fast) ──
    if args.mode == "query":
        if not args.slug:
            print("ERROR: --slug required for query mode", file=sys.stderr)
            return 2
        return _query_mode(wiki_dir, args.slug)

    # ── Full mode ──
    print(f"[graph] Parsing wiki pages from {wiki_dir} ...")
    pages = _parse_wiki_pages(wiki_dir)
    total_parsed = len(pages)
    if args.limit and args.limit < total_parsed:
        limited_keys = sorted(pages.keys())[:args.limit]
        pages = {k: pages[k] for k in limited_keys}
        print(f"[graph] Limited to {args.limit} pages (from {total_parsed} total)")
    print(f"[graph] Found {len(pages)} pages")

    if len(pages) < 2:
        print("[graph] Not enough pages for graph analysis (need ≥ 2)", file=sys.stderr)
        return 0

    print(f"[graph] Building four-signal weighted graph ...")
    G = _build_graph(pages)
    print(
        f"[graph] Graph built: {G.number_of_nodes()} nodes, "
        f"{G.number_of_edges()} edges"
    )

    # Louvain
    print(f"[graph] Running Louvain community detection ...")
    partition = _run_louvain(G)
    n_communities = len(set(partition.values()))
    print(f"[graph] Found {n_communities} communities")

    # Cohesion
    cohesion = _compute_cohesion(G, partition)
    low_coh_count = sum(
        1 for info in cohesion.values() if info.get("warning") and info["size"] > 2
    )
    if low_coh_count:
        print(f"[graph] {low_coh_count} community(s) with low cohesion (< {COHESION_WARN})")

    # Bridges
    bridges = _find_bridges(G, partition)
    print(f"[graph] Found {len(bridges)} bridge nodes")

    # Attach community info to graph nodes for query reuse
    for node, comm in partition.items():
        G.nodes[node]["community"] = comm

    if args.dry_run:
        print(f"\n[graph] DRY-RUN — skipping file output")
        print(f"  Nodes:         {G.number_of_nodes()}")
        print(f"  Edges:         {G.number_of_edges()}")
        print(f"  Communities:   {n_communities}")
        print(f"  Isolated:      {sum(1 for _, d in G.degree() if d == 0)}")
        print(f"  Low cohesion:  {low_coh_count}")
        print(f"  Bridge nodes:  {len(bridges)}")
        return 0

    # Write outputs
    out_base.mkdir(parents=True, exist_ok=True)

    _write_graph_json(G, partition, cohesion, out_base / "graph.json")
    _write_gaps_report(G, partition, cohesion, bridges, pages, out_base / "knowledge-gaps.md")

    clusters_dir = out_base / "clusters"
    _write_cluster_hubs(G, partition, cohesion, pages, clusters_dir)

    print(f"\n[graph] Done. Outputs in {out_base}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
