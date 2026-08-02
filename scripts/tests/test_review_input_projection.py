"""Stage 3.1 must review what Stage 3.2 will actually write.

NashSU 0.6.6 generates and validates review suggestions before
``writeFileBlocks`` (improved-wiki Stage 3.1). Feeding it raw in-memory FILE
blocks makes the reviewer describe a draft the writer then
changes deterministically, in two ways that both reach ``wiki/REVIEW/``:

  * ``strict_missing_targets`` de-links a ``[[prefixed/target]]`` that is
    outside the batch ∪ disk inventory, so a ``missing-page`` item raised on it
    is already resolved by the time Stage 3.5 persists it;
  * ``_stage_3_2_schema_route`` moves a ``type: finding`` page emitted at
    ``concepts/x.md`` to ``findings/x.md``, so the item's ``affected_pages``
    renders a broken ``[[concepts/x]]`` link.

Neither needs an LLM to predict: both transforms are deterministic. The
projection below applies exactly the write loop's own resolution chain, so the
reviewer sees post-write paths and post-write links while still reviewing the
generation (page merges stay out of scope — that is what NashSU reviews too).

Stdlib unittest only — no pytest, no network, no LLM calls.
"""
from __future__ import annotations

import inspect
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _core  # noqa: E402
import _ingest_write as iw  # noqa: E402
import _stage_3_write as sw  # noqa: E402

CANONICAL = "raw/Book/foo.pdf"
ROUTING = {
    "source": "sources",
    "concept": "concepts",
    "entity": "entities",
    "finding": "findings",
}
VALID_SUBDIRS = {"sources", "concepts", "entities", "findings"}


def _make_config(tmp: Path) -> _core.Config:
    return _core.Config(
        wiki_root=tmp,
        raw_root=tmp / "raw",
        wiki_dir=tmp / "wiki",
        runtime_dir=tmp / "rt",
        cache_path=tmp / "rt" / "ingest-cache.json",
        progress_dir=tmp / "rt" / "ingest-progress",
        extract_tmp_dir=tmp / "rt" / "extract-tmp",
        llm_model="m",
        caption_api_key="",
        caption_base_url="x",
        caption_model="c",
        chunk_overlap=3000,
        source_budget=100000,
        target_chars=60000,
        target_tokens=30000,
        max_tokens=8192,
        conversation_prefix="ab12cd34",
    )


def _block(fm_type: str, title: str, body: str) -> str:
    return (
        "---\n"
        f"type: {fm_type}\n"
        f'title: "{title}"\n'
        "tags: [radar]\n"
        "related: []\n"
        'sources: ["raw/Book/foo.pdf"]\n'
        "created: 2026-07-01\n"
        "updated: 2026-07-01\n"
        "---\n\n"
        f"# {title}\n\n{body}\n"
    )


class ProjectWriteResultBlocks(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.cfg = _make_config(self.tmp)
        (self.cfg.wiki_dir / "concepts").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _project(self, blocks, slug_dirs):
        return sw.project_write_result_blocks(
            blocks,
            VALID_SUBDIRS,
            ROUTING,
            slug_dirs,
            canonical_source=CANONICAL,
            today="2026-07-30",
            source_page_slug="sources/Book/foo",
        )

    def test_schema_routed_page_is_reviewed_at_its_written_path(self):
        blocks = [(
            "concepts/swerling-gain.md",
            _block("finding", "Swerling Gain", "A measured finding."),
        )]
        projected = self._project(blocks, {"swerling-gain": {"findings"}})

        self.assertEqual(["findings/swerling-gain.md"],
                         [path for path, _ in projected])

    def test_missing_target_is_delinked_before_review(self):
        blocks = [(
            "concepts/matched-filter.md",
            _block("concept", "Matched Filter",
                   "See [[concepts/pulse-compression]] and "
                   "[[concepts/never-generated]]."),
        )]
        slug_dirs = {
            "matched-filter": {"concepts"},
            "pulse-compression": {"concepts"},
        }
        _path, content = self._project(blocks, slug_dirs)[0]

        self.assertIn("[[concepts/pulse-compression]]", content)
        self.assertNotIn("[[concepts/never-generated]]", content)
        self.assertIn("never-generated", content,
                      "de-linking keeps the display text as plain prose")

    def test_application_managed_aggregates_are_excluded(self):
        blocks = [
            ("index.md", _block("concept", "Index", "should be dropped")),
            ("concepts/alpha.md", _block("concept", "Alpha", "kept")),
        ]
        projected = self._project(blocks, {"alpha": {"concepts"}})

        self.assertEqual(["concepts/alpha.md"], [p for p, _ in projected])

    def test_sources_and_dates_match_the_write_result(self):
        blocks = [("concepts/alpha.md", _block("concept", "Alpha", "body"))]
        _path, content = self._project(blocks, {"alpha": {"concepts"}})[0]

        self.assertIn("updated: 2026-07-30", content)
        self.assertIn(CANONICAL, content)


class WriteLoopSharesOneResolutionChain(unittest.TestCase):
    """The projection must not become a third copy of the path chain."""

    def test_resolver_is_shared_by_loop_slug_dirs_and_projection(self):
        for func in (
            iw._do_write,
            sw.stage_3_2_build_slug_dirs,
            sw.project_write_result_blocks,
        ):
            self.assertIn(
                "resolve_ingest_write_path(",
                inspect.getsource(func),
                f"{func.__name__} must use the shared path resolver",
            )

    def test_review_input_is_projected_before_it_is_reviewed(self):
        source = inspect.getsource(iw._do_write)
        self.assertLess(
            source.index("project_write_result_blocks("),
            source.index("stage_3_1_prepare_review_suggestions("),
            "Stage 3.1 must receive the projected blocks",
        )


class ResolveIngestWritePath(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cfg = _make_config(Path(self._tmpdir.name))

    def tearDown(self):
        self._tmpdir.cleanup()

    def _resolve(self, rel_path, content):
        return sw.resolve_ingest_write_path(
            rel_path, content, VALID_SUBDIRS, ROUTING, quiet=True)

    def test_traversal_and_unsafe_paths_are_rejected(self):
        body = _block("concept", "X", "b")
        self.assertIsNone(self._resolve("../escape.md", body))
        self.assertIsNone(self._resolve("/abs/escape.md", body))

    def test_listing_pages_are_rejected(self):
        for name in ("index.md", "log.md", "overview.md", "schema.md"):
            self.assertIsNone(
                self._resolve(name, _block("concept", "X", "b")), name)

    def test_missing_suffix_and_bare_path_are_corrected(self):
        self.assertEqual(
            "concepts/alpha.md",
            self._resolve("concepts/alpha", _block("concept", "Alpha", "b")))

    def test_correct_path_passes_through(self):
        self.assertEqual(
            "entities/bell-labs.md",
            self._resolve(
                "entities/bell-labs.md", _block("entity", "Bell Labs", "b")))


if __name__ == "__main__":
    unittest.main()
