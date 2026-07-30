"""A same-slug collision inside ONE source's write loop must MERGE, not replace.

NashSU 0.6.6's corrected-source rule says: when every ``sources`` entry on an
existing page resolves to the source being ingested, that page is owned solely
by this source, so the freshly generated body supersedes the stale one
(_stage_3_write._stage_3_1_is_owned_only_by_source → replace_existing_body).

That rule is about a page left behind by a PREVIOUS ingest. Applied to a page
this same write loop wrote moments ago it is destructive: the "owner" test
holds by construction (Stage 3.1 canonicalizes ``sources`` to the current
source), so the second FILE block silently discards the first block's body —
no LLM merge, no warning, and the console still prints ``[merge]``.

Observed shapes of the collision:
  * Stage 2.6 prepends the source page to ``file_blocks``; a stray
    ``wiki/sources/<stem>.md`` block from Stage 2.4 (which
    ``_verify_stage_2_4_file_blocks`` already warns about) sorts AFTER it and
    would replace the real source summary;
  * two Stage 2.2 candidate names that slugify identically
    (``MTI（动目标显示）`` / ``MTI``);
  * two differently-typed candidates that schema routing collapses onto the
    same directory + stem.

Stdlib unittest only — no pytest, no network. The LLM merger is stubbed.
"""
from __future__ import annotations

import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _core  # noqa: E402
import _ingest_write as iw  # noqa: E402
import _stage_3_write as sw  # noqa: E402

CANONICAL = "raw/Book/foo.pdf"


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


def _page(body: str, *, fm_type: str = "source", tags: str = "[radar]") -> str:
    return (
        "---\n"
        f"type: {fm_type}\n"
        'title: "Foo"\n'
        f"tags: {tags}\n"
        "related: []\n"
        f'sources: ["{CANONICAL}"]\n'
        "created: 2026-07-30\n"
        "updated: 2026-07-30\n"
        "---\n\n"
        f"# Foo\n\n{body}\n"
    )


FIRST = _page("Rich Stage 2.6 source summary with 40 claims.")
SECOND = _page("Stray short block from Stage 2.4.", tags="[ew]")


class SameRunCollisionMerges(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.cfg = _make_config(self.tmp)
        self.page = self.cfg.wiki_dir / "sources" / "Book" / "foo.md"
        self.page.parent.mkdir(parents=True, exist_ok=True)
        self.page.write_text(FIRST, encoding="utf-8")

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write(self, **kwargs) -> str:
        # >=100 chars: _frontmatter.merge_page_content rejects a tiny merge
        # response rather than falling back to the array-only merge.
        merged_marker = (
            "MERGED BODY: both versions preserved.\n\n"
            "Rich Stage 2.6 source summary with 40 claims.\n\n"
            "Stray short block from Stage 2.4.\n"
        )
        with mock.patch.object(
            sw,
            "call_anthropic_protocol",
            return_value=(merged_marker, "end_turn"),
        ) as llm:
            sw.stage_3_1_write_wiki_file(
                self.page,
                SECOND,
                self.cfg,
                merge=True,
                source_file=CANONICAL,
                **kwargs,
            )
        return self.page.read_text(encoding="utf-8"), llm.call_count

    def test_same_run_collision_calls_the_merger_and_keeps_both_bodies(self):
        content, llm_calls = self._write(same_run_collision=True)

        self.assertEqual(
            1, llm_calls,
            "a same-run slug collision must go through the real page merge")
        self.assertIn("MERGED BODY", content)
        self.assertIn("ew", content, "frontmatter arrays still union")
        self.assertIn("radar", content)

    def test_previous_ingest_page_still_replaces(self):
        """The NashSU corrected-source path must stay intact."""
        content, llm_calls = self._write(same_run_collision=False)

        self.assertEqual(
            0, llm_calls,
            "a page left by a previous ingest of this same source is replaced")
        self.assertIn("Stray short block", content)
        self.assertNotIn("Rich Stage 2.6 source summary", content)

    def test_default_preserves_replace_semantics(self):
        content, llm_calls = self._write()

        self.assertEqual(0, llm_calls)
        self.assertIn("Stray short block", content)


class WriteLoopDetectsTheCollision(unittest.TestCase):
    def test_collision_helper_truth_table(self):
        written = {Path("/w/sources/foo.md"): FIRST}

        self.assertTrue(iw._is_same_run_collision(
            Path("/w/sources/foo.md"), SECOND, written))
        self.assertFalse(
            iw._is_same_run_collision(
                Path("/w/sources/foo.md"), FIRST, written),
            "byte-identical replays are the redundant-duplicate case")
        self.assertFalse(iw._is_same_run_collision(
            Path("/w/concepts/other.md"), SECOND, written))

    def test_write_loop_forwards_the_flag(self):
        source = inspect.getsource(iw._do_write)
        self.assertIn("same_run_collision=", source)
        self.assertIn("_is_same_run_collision(", source)


if __name__ == "__main__":
    unittest.main()
