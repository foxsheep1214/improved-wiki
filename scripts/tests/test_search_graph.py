"""One-hop graph expansion of search results (NashSU search.rs
``blend_graph_results`` / ``graph_result_quota``)."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _search_graph as sg  # noqa: E402
import search_wiki  # noqa: E402


def _page(title: str, body: str = "", fm: str = "type: concept") -> str:
    return f"---\n{fm}\ntitle: {title}\n---\n\n# {title}\n\n{body}\n"


def _hit(path: str, score: float = 1.0) -> dict:
    return {"path": path, "title": path, "snippet": "kw", "title_match": False,
            "score": score, "vector_score": None}


def _graph(pages: dict[str, str]) -> sg.GraphPages:
    graph = sg.GraphPages()
    for rel, content in pages.items():
        graph.add(rel, content)
    return graph


class QuotaTests(unittest.TestCase):
    def test_quota_matches_search_rs(self):
        self.assertEqual(sg.graph_result_quota(20, 0), 6)    # 30 %
        self.assertEqual(sg.graph_result_quota(20, 20), 3)   # 15 %
        self.assertEqual(sg.graph_result_quota(20, 10), 5)   # ceil(20 × 0.225)
        self.assertEqual(sg.graph_result_quota(4, 0), 2)
        self.assertEqual(sg.graph_result_quota(2, 0), 1)     # clamp to limit-1
        self.assertEqual(sg.graph_result_quota(1, 0), 0)


class BlendTests(unittest.TestCase):
    def test_neighbours_of_seeds_fill_the_graph_share(self):
        graph = _graph({
            "concepts/a.md": _page("A", "see [[concepts/c]]"),
            "concepts/b.md": _page("B", "see [[c]] and [[d]]"),
            "concepts/c.md": _page("C"),
            "concepts/d.md": _page("D"),
        })
        results, hits = sg.blend_graph_results(
            [_hit("concepts/a.md"), _hit("concepts/b.md")], graph, 4, 0)
        self.assertEqual(hits, 2)
        self.assertEqual([r["path"] for r in results],
                         ["concepts/a.md", "concepts/b.md",
                          "concepts/c.md", "concepts/d.md"])
        c = results[2]
        self.assertEqual(c["graph_related_to"], ["A", "B"])
        self.assertEqual(c["snippet"], "Graph neighbor of A, B")
        self.assertAlmostEqual(c["score"], 1.5 / 61.0)   # 1/1 + 1/2 over K+1
        self.assertEqual(results[3]["graph_related_to"], ["B"])

    def test_ranked_non_seed_neighbour_keeps_its_result_and_moves_to_graph_slot(self):
        graph = _graph({
            "concepts/a.md": _page("A", "[[concepts/c]]"),
            "concepts/b.md": _page("B"),
            "concepts/c.md": _page("C"),
        })
        ranked = [_hit("concepts/a.md"), _hit("concepts/b.md"),
                  _hit("concepts/c.md", 0.1)]
        results, hits = sg.blend_graph_results(ranked, graph, 2, 0)
        self.assertEqual(hits, 1)
        self.assertEqual([r["path"] for r in results],
                         ["concepts/a.md", "concepts/c.md"])
        self.assertEqual(results[1]["snippet"], "kw")
        self.assertEqual(results[1]["graph_related_to"], ["A"])

    def test_related_frontmatter_and_backlinks_are_edges(self):
        graph = _graph({
            "concepts/a.md": _page(
                "A", fm='type: concept\nrelated: ["entities/e"]'),
            "entities/e.md": _page("E"),
            "concepts/back.md": _page("Back", "links to [[concepts/a]]"),
        })
        results, _ = sg.blend_graph_results([_hit("concepts/a.md")], graph, 4, 0)
        self.assertEqual({r["path"] for r in results[1:]},
                         {"entities/e.md", "concepts/back.md"})

    def test_redirect_stub_stands_for_its_target(self):
        graph = _graph({
            "concepts/a.md": _page("A", "[[concepts/old]] [[concepts/orphan-stub]]"),
            "concepts/old.md": _page(
                "Old", "merged into [[concepts/new]]",
                fm='type: redirect\nredirect: "concepts/new"'),
            "concepts/orphan-stub.md": _page(
                "Orphan", "见 [[concepts/gone]]", fm="type: redirect"),
            "concepts/new.md": _page("New"),
        })
        results, hits = sg.blend_graph_results([_hit("concepts/a.md")], graph, 4, 0)
        self.assertEqual(hits, 1)
        self.assertEqual([r["path"] for r in results],
                         ["concepts/a.md", "concepts/new.md"])

    def test_no_neighbours_truncates_to_limit(self):
        graph = _graph({"concepts/a.md": _page("A"), "concepts/b.md": _page("B")})
        ranked = [_hit("concepts/a.md"), _hit("concepts/b.md")]
        results, hits = sg.blend_graph_results(ranked, graph, 1, 0)
        self.assertEqual((hits, [r["path"] for r in results]), (0, ["concepts/a.md"]))


class SearchWikiIntegrationTests(unittest.TestCase):
    def test_keyword_search_output_includes_graph_neighbours(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            concepts = root / "wiki" / "concepts"
            concepts.mkdir(parents=True)
            (concepts / "buck.md").write_text(
                _page("Buck Converter", "zebracrossing; see [[concepts/ringing]]"),
                encoding="utf-8")
            (concepts / "ringing.md").write_text(
                _page("Switch-node Ringing", "LC resonance after each edge."),
                encoding="utf-8")
            stdout = io.StringIO()
            argv = ["search_wiki.py", "zebracrossing", "--project", str(root),
                    "--keyword-only", "--json"]
            with (mock.patch.object(sys, "argv", argv),
                  redirect_stdout(stdout), redirect_stderr(io.StringIO())):
                self.assertEqual(search_wiki.main(), 0)
        results = json.loads(stdout.getvalue())
        self.assertEqual([r["path"] for r in results],
                         ["concepts/buck.md", "concepts/ringing.md"])
        self.assertEqual(results[1]["graph_related_to"], ["Buck Converter"])


if __name__ == "__main__":
    unittest.main()
