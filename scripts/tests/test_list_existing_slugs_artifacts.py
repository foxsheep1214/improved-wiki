"""list_existing_slugs must exclude DERIVED artifacts, not just REVIEW.

Regression: graph.py writes per-community hub pages to wiki/clusters/cluster-NNN.md
(type: index) — derived graph output, not knowledge content. graph.py excludes
clusters/ from its own input (GRAPH_SKIP_DIRS), but list_existing_slugs only
skipped REVIEW + lint, so the cluster pages leaked into the Stage 2.1/2.2/2.3
existing-pages context fed to the LLM (observed: cluster-000..015 in a book's
linkable list). The fix mirrors GRAPH_SKIP_DIRS = {REVIEW, clusters, lint, media}.

Stdlib unittest only.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _core  # noqa: E402


def _make_config(tmp: Path) -> _core.Config:
    return _core.Config(
        wiki_root=tmp, raw_root=tmp / "raw",
        wiki_dir=tmp / "wiki", runtime_dir=tmp / "rt",
        cache_path=tmp / "rt" / "ingest-cache.json",
        progress_dir=tmp / "rt" / "ingest-progress",
        extract_tmp_dir=tmp / "rt" / "extract-tmp",
        llm_base_url="https://example.invalid", llm_model="m", llm_api_key="",
        llm_protocol="anthropic", caption_api_key="", caption_base_url="x",
        caption_model="c", chunk_size=60000, chunk_overlap=3000,
        source_budget=100000, target_chars=60000, target_tokens=30000,
        max_tokens=8192, conversation_prefix="ab12cd34",
    )


def _write(p: Path, text: str = "---\ntype: x\n---\nbody\n") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class TestListExistingSlugsArtifacts(unittest.TestCase):
    def test_clusters_and_artifacts_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            w = cfg.wiki_dir
            # real knowledge content — must be included
            _write(w / "concepts" / "real-concept.md")
            _write(w / "entities" / "real-entity.md")
            # derived artifacts — must be excluded
            _write(w / "clusters" / "cluster-000.md", "---\ntype: index\n---\n# Cluster 000\n")
            _write(w / "REVIEW" / "20260630-duplicate-foo.md")
            _write(w / "lint" / "some-lint.md")
            _write(w / "media" / "Book" / "note.md")  # stray .md under media
            # anchors / system — must be excluded
            _write(w / "index.md")
            _write(w / "_audit_thing.md")

            slugs = _core.list_existing_slugs(cfg)

            self.assertIn("real-concept", slugs)
            self.assertIn("real-entity", slugs)
            self.assertNotIn("cluster-000", slugs)
            self.assertNotIn("20260630-duplicate-foo", slugs)
            self.assertNotIn("some-lint", slugs)
            self.assertNotIn("note", slugs)
            self.assertNotIn("index", slugs)
            self.assertNotIn("_audit_thing", slugs)

    def test_lint_stubs_and_garbage_query_slugs_excluded(self):
        """A2 (audit 2026-07-02, M7): lint stub placeholder pages and
        date-prefixed garbage query slugs must not enter the linkable list."""
        stub = ("---\ntype: query\ntitle: \"匹配滤波器 Matched Filter\"\n"
                "created: 2026-06-16\nupdated: 2026-06-16\n"
                "tags: [stub, lint]\nrelated: []\nsources: []\n---\n\n"
                "# 匹配滤波器\n\nCreated by Wiki Lint as a placeholder.\n")
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_config(Path(d))
            w = cfg.wiki_dir
            # lint stubs (queries/ and a concepts/-parked one) — excluded
            _write(w / "queries" / "some-lint-stub.md", stub)
            _write(w / "concepts" / "匹配滤波器-matched-filter.md", stub)
            # legacy date-prefixed garbage query slug — excluded
            _write(w / "queries" / "2026-06-16-低空目标检测-001.md",
                   "---\ntype: query\ntitle: \"低空目标检测\"\ntags: [radar]\n---\nbody\n")
            # REAL query page — included (type query alone is not a stub)
            _write(w / "queries" / "real-question.md",
                   "---\ntype: query\ntitle: \"Real Question\"\ntags: [radar, mti]\n---\nbody\n")
            # date-prefixed SOURCE stem — included (filter is queries/-scoped)
            _write(w / "sources" / "2026-05-31-news-clip.md",
                   "---\ntype: source\ntitle: \"News\"\ntags: [news]\n---\nbody\n")
            _write(w / "concepts" / "real-concept.md")

            slugs = _core.list_existing_slugs(cfg)

            self.assertNotIn("some-lint-stub", slugs)
            self.assertNotIn("匹配滤波器-matched-filter", slugs)
            self.assertNotIn("2026-06-16-低空目标检测-001", slugs)
            self.assertIn("real-question", slugs)
            self.assertIn("2026-05-31-news-clip", slugs)
            self.assertIn("real-concept", slugs)


if __name__ == "__main__":
    unittest.main()
