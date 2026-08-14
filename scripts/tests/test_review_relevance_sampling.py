"""Stage 3.1 must review source-relevant context, not a fixed global sample."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _stage_3_review as review  # noqa: E402
import _ingest_write as ingest_write  # noqa: E402
from _progress import is_stage_done, mark_stage_done  # noqa: E402
from _config import Config  # noqa: E402


def _config(root: Path) -> Config:
    cfg = Config.from_env()
    cfg.wiki_root = root
    cfg.wiki_dir = root / "wiki"
    cfg.raw_root = root / "raw"
    cfg.runtime_dir = root / ".llm-wiki"
    cfg.cache_path = cfg.runtime_dir / "ingest-cache.json"
    cfg.progress_dir = cfg.runtime_dir / "ingest-progress"
    cfg.extract_tmp_dir = cfg.runtime_dir / "extract-tmp"
    return cfg


def _write(root: Path, rel: str, body: str) -> None:
    path = root / "wiki" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: concept\ntitle: Test\n---\n\n" + body,
        encoding="utf-8",
    )


class RelevantExistingPageSelection(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cfg = _config(self.root)

        # More than the old fixed budget of alphabetically early pages.
        for index in range(45):
            _write(
                self.root,
                f"concepts/00-unrelated-{index:02d}.md",
                "# Unrelated\n\nNo connection to this ingest.\n",
            )
        _write(
            self.root,
            "concepts/relevant-target.md",
            "# Relevant Target\n",
        )
        _write(
            self.root,
            "concepts/backlink.md",
            "# Backlink\n\nSee [[concepts/new-topic]].\n",
        )
        _write(
            self.root,
            "methodology/new-topic.md",
            "# Same basename\n",
        )
        _write(
            self.root,
            "concepts/new-topic.md",
            "# Prior on-disk version\n",
        )
        _write(
            self.root,
            "concepts/related-target.md",
            "# Related Target\n",
        )
        _write(
            self.root,
            "concepts/related-backlink.md",
            "# Related Backlink\n",
        )
        related_backlink = (
            "---\ntype: concept\ntitle: Related Backlink\n"
            "related:\n  - concepts/new-topic\n---\n\n"
            "# Related Backlink\n"
        )
        path = self.root / "wiki/concepts/related-backlink.md"
        path.write_text(related_backlink, encoding="utf-8")
        _write(
            self.root,
            "concepts/escaped-target.md",
            "# Escaped Target\n",
        )

    def test_only_graph_and_path_neighbours_are_selected(self):
        selected = review._select_relevant_existing_pages(
            [(
                "concepts/new-topic.md",
                "---\ntype: concept\n---\n\n# New Topic\n\n"
                "See [[concepts/relevant-target]].\n",
            )],
            self.cfg,
        )
        joined = "\n".join(selected)
        self.assertIn("concepts/relevant-target.md", joined)
        self.assertIn("concepts/backlink.md", joined)
        self.assertIn("methodology/new-topic.md", joined)
        self.assertIn("concepts/new-topic.md", joined)
        self.assertNotIn("00-unrelated", joined)

    def test_related_frontmatter_and_escaped_alias_are_graph_edges(self):
        selected = review._select_relevant_existing_pages(
            [(
                "concepts/new-topic.md",
                "---\ntype: concept\ntitle: New Topic\n"
                "related: [concepts/related-target]\n---\n\n"
                "# New Topic\n\n"
                "| Link |\n|---|\n"
                "| [[concepts/escaped-target\\|display]] |\n",
            )],
            self.cfg,
        )
        joined = "\n".join(selected)
        self.assertIn("concepts/related-target.md", joined)
        self.assertIn("concepts/related-backlink.md", joined)
        self.assertIn("concepts/escaped-target.md", joined)

    def test_unrelated_non_utf8_page_is_skipped(self):
        bad = self.root / "wiki/concepts/00-invalid-utf8.md"
        bad.write_bytes(b"\xff\xfe")
        selected = review._select_relevant_existing_pages(
            [("concepts/new-topic.md", "# New Topic\n")], self.cfg)
        self.assertTrue(selected)
        self.assertNotIn("00-invalid-utf8", "\n".join(selected))

    def test_selection_is_deterministic_and_bounded(self):
        blocks = [(
            "concepts/new-topic.md",
            "# New Topic\n\nSee [[concepts/relevant-target]].\n",
        )]
        first = review._select_relevant_existing_pages(
            blocks, self.cfg, limit=2)
        second = review._select_relevant_existing_pages(
            blocks, self.cfg, limit=2)
        self.assertEqual(first, second)
        self.assertEqual(2, len(first))


class SourceScopedReviewGate(unittest.TestCase):
    def test_global_only_findings_are_dropped(self):
        items = [
            {
                "type": "confirm",
                "title": "Own issue",
                "affected_pages": ["concepts/new-topic.md"],
            },
            {
                "type": "confirm",
                "title": "Unrelated old issue",
                "affected_pages": ["concepts/00-unrelated-00.md"],
            },
            {
                "type": "duplicate",
                "title": "Cross-page issue",
                "affected_pages": [
                    "concepts/new-topic.md",
                    "methodology/new-topic.md",
                ],
            },
        ]
        kept, dropped = review._filter_review_items_to_new_pages(
            items, {"concepts/new-topic.md"})
        self.assertEqual(2, len(kept))
        self.assertEqual(1, dropped)
        self.assertEqual(
            {"Own issue", "Cross-page issue"},
            {item["title"] for item in kept},
        )


class ReviewPreparedCheckpointVersioning(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = _config(Path(self._tmp.name))
        self.source_hash = "a" * 64

    @staticmethod
    def _item(page: str) -> dict:
        return {
            "type": "confirm",
            "title": "Issue",
            "description": "Description",
            "affected_pages": [page],
            "severity": "low",
            "search_queries": [],
        }

    def test_legacy_checkpoint_is_invalidated_before_restore(self):
        mark_stage_done(
            self.cfg,
            self.source_hash,
            "review_prepared",
            payload={"items_data": [self._item("concepts/old.md")]},
        )
        restored = ingest_write._restore_review_prepared_checkpoint(
            self.cfg, self.source_hash)
        self.assertIsNone(restored)
        self.assertFalse(is_stage_done(
            self.cfg, self.source_hash, "review_prepared"))

    def test_current_source_scoped_checkpoint_restores(self):
        payload = {
            "scope_version": review._REVIEW_SCOPE_VERSION,
            "new_page_refs": ["concepts/new.md"],
            "items_data": [self._item("concepts/new.md")],
            "skipped": False,
            "reason": "",
            "stop_reason": "end_turn",
        }
        mark_stage_done(
            self.cfg,
            self.source_hash,
            "review_prepared",
            payload=payload,
        )
        restored = ingest_write._restore_review_prepared_checkpoint(
            self.cfg, self.source_hash)
        self.assertEqual(payload, restored)
        self.assertTrue(is_stage_done(
            self.cfg, self.source_hash, "review_prepared"))

    def test_stage35_rejects_unversioned_payload(self):
        with self.assertRaisesRegex(RuntimeError, "source-unscoped"):
            review.stage_3_5_persist_review_suggestions(
                {"items_data": []}, Path("raw/Book.pdf"), self.cfg)


if __name__ == "__main__":
    unittest.main()
