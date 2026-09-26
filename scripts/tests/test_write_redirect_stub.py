"""Stage 3.2 must never merge generated content into a redirect stub.

Dedup leaves ``type: redirect`` stubs on retired slugs, and a later generation
naturally reuses those names. On 2026-09-24 a RadarWiki ingest merged a new
concept body into concepts/micro-doppler-uav-classification: the page kept
``type: redirect``, lost its ``redirect:`` target, and hid the new content
behind a stub. A block for a stub path now lands on the stub's target; a stub
without a usable target is replaced rather than merged.

Stdlib unittest only — no pytest, no network, no LLM calls.
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
from _frontmatter import parse_frontmatter  # noqa: E402

ROUTING = {"concept": "concepts", "methodology": "methodology"}
VALID_SUBDIRS = {"sources", "concepts", "entities", "methodology"}


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


def _concept(title: str, body: str) -> str:
    return (
        "---\n"
        "type: concept\n"
        f'title: "{title}"\n'
        "tags: [radar]\n"
        "related: []\n"
        'sources: ["raw/Paper/new.pdf"]\n'
        "created: 2026-09-24\n"
        "updated: 2026-09-24\n"
        "---\n\n"
        f"# {title}\n\n{body}\n"
    )


def _stub(title: str, target: str | None) -> str:
    field = f'redirect: "{target}"\n' if target is not None else ""
    return (
        "---\n"
        "type: redirect\n"
        f'title: "{title} — compatibility alias"\n'
        f"{field}"
        "created: 2026-09-01\n"
        "updated: 2026-09-07\n"
        "---\n\n"
        f"# {title}\n\nMerged into the canonical page.\n"
    )


class _WikiCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cfg = _make_config(Path(self._tmpdir.name))

    def tearDown(self):
        self._tmpdir.cleanup()

    def _put(self, rel: str, text: str) -> Path:
        path = self.cfg.wiki_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _resolve(self, rel: str, *, wiki_dir: Path | None = None) -> str | None:
        return sw.resolve_ingest_write_path(
            rel, _concept("Old Name", "new body"), VALID_SUBDIRS, ROUTING,
            quiet=True, wiki_dir=self.cfg.wiki_dir if wiki_dir is None else wiki_dir)


class ResolveFollowsRedirectStub(_WikiCase):
    def test_block_for_a_stub_lands_on_its_target(self):
        self._put("concepts/old-name.md", _stub("Old Name", "concepts/canonical"))
        self._put("concepts/canonical.md", _concept("Canonical", "kept"))
        self.assertEqual("concepts/canonical.md", self._resolve("concepts/old-name.md"))

    def test_redirect_chain_is_followed_across_directories(self):
        self._put("concepts/old-name.md", _stub("Old Name", "methodology/middle"))
        self._put("methodology/middle.md", _stub("Middle", "concepts/final.md"))
        self._put("concepts/final.md", _concept("Final", "kept"))
        self.assertEqual("concepts/final.md", self._resolve("concepts/old-name.md"))

    def test_stub_without_usable_target_keeps_its_path(self):
        cases = {
            "no field": _stub("Old Name", None),
            "missing target": _stub("Old Name", "concepts/gone"),
            "outside wiki": _stub("Old Name", "../outside"),
        }
        (self.cfg.wiki_root / "outside.md").write_text(_concept("X", "y"), encoding="utf-8")
        for label, stub in cases.items():
            with self.subTest(label):
                self._put("concepts/old-name.md", stub)
                self.assertEqual("concepts/old-name.md", self._resolve("concepts/old-name.md"))

    def test_redirect_loop_keeps_the_original_path(self):
        self._put("concepts/old-name.md", _stub("Old Name", "concepts/other"))
        self._put("concepts/other.md", _stub("Other", "concepts/old-name"))
        self.assertEqual("concepts/old-name.md", self._resolve("concepts/old-name.md"))

    def test_ordinary_pages_and_callers_without_wiki_dir_are_unchanged(self):
        self._put("concepts/old-name.md", _concept("Old Name", "live page"))
        self.assertEqual("concepts/old-name.md", self._resolve("concepts/old-name.md"))
        self._put("concepts/stub.md", _stub("Stub", "concepts/old-name"))
        self.assertEqual("concepts/stub.md", sw.resolve_ingest_write_path(
            "concepts/stub.md", _concept("Stub", "b"), VALID_SUBDIRS, ROUTING, quiet=True))

    def test_review_projection_sees_the_target_path(self):
        self._put("concepts/old-name.md", _stub("Old Name", "concepts/canonical"))
        self._put("concepts/canonical.md", _concept("Canonical", "kept"))
        projected = sw.project_write_result_blocks(
            [("concepts/old-name.md", _concept("Old Name", "new body"))],
            VALID_SUBDIRS, ROUTING, {},
            canonical_source="raw/Paper/new.pdf", today="2026-09-26",
            source_page_slug="sources/new", wiki_dir=self.cfg.wiki_dir)
        self.assertEqual(["concepts/canonical.md"], [path for path, _ in projected])


class WriterReplacesStubWithoutTarget(_WikiCase):
    def test_stub_without_target_is_replaced_not_merged(self):
        path = self._put("concepts/old-name.md", _stub("Old Name", None))
        with mock.patch.object(sw, "_stage_3_2_merge_page_content",
                               side_effect=AssertionError("must not merge into a stub")):
            sw.stage_3_2_write_wiki_file(
                path, _concept("Old Name", "new body"), self.cfg, merge=True,
                source_file="raw/Paper/new.pdf")
        fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        self.assertEqual("concept", fm["type"])
        self.assertIn("new body", body)
        history = list((self.cfg.runtime_dir / "page-history").glob("*_concepts_old-name.md"))
        self.assertEqual(1, len(history), "the replaced stub is kept in page-history")


class IngestWriteLoopPassesWikiDir(unittest.TestCase):
    def test_loop_and_projection_resolve_against_the_wiki(self):
        source = inspect.getsource(iw._do_write)
        self.assertGreaterEqual(source.count("wiki_dir=config.wiki_dir"), 2)


if __name__ == "__main__":
    unittest.main()
