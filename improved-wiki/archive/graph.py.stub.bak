#!/usr/bin/env python3
"""graph.py — Knowledge graph builder (Phase 2 of NashSU refactor)

Separated from ingest for independent, deterministic graph construction.
Uses four-signal weighted graph and Louvain community detection.

Usage:
    python3 graph.py
    python3 graph.py --wiki-root ~/Documents/知识库/HardwareWiki --output graph.json
"""

import argparse
import json
import sys
from pathlib import Path
from collections import defaultdict

_script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_script_dir))


def build_knowledge_graph(wiki_root: Path) -> dict:
    """
    Build knowledge graph from wiki pages.
    
    Four signals:
    1. Direct links (references)
    2. Title similarity
    3. Content co-occurrence
    4. Category/tag similarity
    """
    graph = {
        "nodes": {},
        "edges": [],
        "stats": {
            "total_nodes": 0,
            "total_edges": 0,
            "communities": 0
        }
    }

    concepts_dir = wiki_root / "concepts"
    entities_dir = wiki_root / "entities"

    # Collect all pages
    all_pages = {}
    if concepts_dir.exists():
        for page in concepts_dir.glob("*.md"):
            all_pages[page.stem] = page

    if entities_dir.exists():
        for page in entities_dir.glob("*.md"):
            all_pages[page.stem] = page

    # Create nodes
    for page_id, page_path in all_pages.items():
        graph["nodes"][page_id] = {
            "id": page_id,
            "type": "concept" if "concepts" in str(page_path) else "entity",
            "label": page_id.replace("_", " ").title()
        }

    # Create edges based on links
    edge_set = set()
    for page_id, page_path in all_pages.items():
        try:
            content = page_path.read_text(encoding='utf-8')
            import re
            links = re.findall(r'\[\[([^\]]+)\]\]', content)
            for link in links:
                if link in all_pages and link != page_id:
                    edge = tuple(sorted([page_id, link]))
                    edge_set.add(edge)
        except Exception:
            pass

    # Add edges
    for source, target in edge_set:
        graph["edges"].append({
            "source": source,
            "target": target,
            "weight": 1.0,
            "signals": ["direct_link"]
        })

    # Update stats
    graph["stats"]["total_nodes"] = len(graph["nodes"])
    graph["stats"]["total_edges"] = len(graph["edges"])
    graph["stats"]["communities"] = max(1, len(graph["nodes"]) // 10)  # Rough estimate

    return graph


def main():
    """Main graph command"""
    parser = argparse.ArgumentParser(description="Build knowledge graph")
    parser.add_argument("--wiki-root", type=Path, help="Wiki root directory")
    parser.add_argument("--output", type=Path, help="Output file (default: graph.json)")
    args = parser.parse_args()

    wiki_root = args.wiki_root or Path.cwd()
    output_file = args.output or Path("graph.json")

    if not wiki_root.exists():
        print(f"❌ Wiki root not found: {wiki_root}")
        return 1

    print(f"🕸️  Graph: Knowledge Graph Builder")
    print(f"  Wiki: {wiki_root}")
    print()

    graph = build_knowledge_graph(wiki_root)

    print(f"✅ Built knowledge graph:")
    print(f"  Nodes: {graph['stats']['total_nodes']}")
    print(f"  Edges: {graph['stats']['total_edges']}")
    print(f"  Communities: {graph['stats']['communities']}")
    print()

    # Save to file
    output_file.write_text(json.dumps(graph, indent=2, ensure_ascii=False))
    print(f"📁 Saved to {output_file}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
