"""A resolved review page must survive regeneration.

NashSU's ``addItem``/``addItems`` dedup against ALL existing items including
resolved ones, and say why (review-store.ts:120):

    The previous scheme only deduped against *pending* items, which is exactly
    why re-surfacing a review during ingest discarded its resolved state.

improved-wiki materializes each item as a markdown page, so "the existing
item" is a file on disk and the port has to be a write-time guard. Two sites
had it (``wiki-lint-semantic.py``, the orphan branch of ``wiki-lint-fix.py``);
Stage 3.5 and three ``wiki-lint-fix.py`` branches did not, and regenerated
``resolved: false`` straight over a triaged page.

Pending pages stay refreshable — the fresh render can carry a better
description or a longer ``Referenced by`` list, and NashSU likewise keeps
merging into a pending item.

Stdlib unittest only — no pytest, no network, no LLM calls.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _stage_3_review as review  # noqa: E402
from _config import Config  # noqa: E402
from _review_utils import (  # noqa: E402
    is_resolved_review_file,
    resolve_review_path,
)


def _page(resolved: str, body: str = "original body") -> str:
    return (
        "---\n"
        "type: review\n"
        "review_id: review-deadbeef\n"
        "review_type: confirm\n"
        f"resolved: {resolved}\n"
        'resolved_reason: "Approve"\n'
        "---\n"
        "\n"
        f"# [confirm] Title\n\n{body}\n"
    )


class IsResolvedReviewFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, name: str, content: str) -> Path:
        path = self.dir / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_resolved_page_is_protected(self):
        self.assertTrue(
            is_resolved_review_file(self._write("a.md", _page("true"))))

    def test_pending_page_is_not_protected(self):
        self.assertFalse(
            is_resolved_review_file(self._write("b.md", _page("false"))))

    def test_missing_file_is_not_protected(self):
        self.assertFalse(is_resolved_review_file(self.dir / "nope.md"))

    def test_alternate_truthy_spellings_are_protected(self):
        """Shares is_review_resolved's vocabulary — yes / 1 / quoted count."""
        for spelling in ("yes", "1", '"true"'):
            with self.subTest(spelling=spelling):
                self.assertTrue(
                    is_resolved_review_file(
                        self._write(f"{spelling.strip(chr(34))}.md",
                                    _page(spelling))))

    def test_a_body_mentioning_resolved_does_not_protect(self):
        """Only the frontmatter decides — same anchoring as is_review_resolved."""
        page = _page("false", body="The user wrote resolved: true in prose.")
        self.assertFalse(is_resolved_review_file(self._write("c.md", page)))

    def test_unreadable_path_is_not_protected(self):
        """A directory where a file was expected must not block the write."""
        (self.dir / "sub.md").mkdir()
        self.assertFalse(is_resolved_review_file(self.dir / "sub.md"))


class Stage35DoesNotOverwriteTriagedPages(unittest.TestCase):
    """The regression itself: a re-ingest must not reset a triaged review.

    Exercises the real Stage 3.5 write loop, because the helper passing proves
    nothing about whether the write site consults it.
    """

    TITLE = "验证 40 dB 反隐身增益的一致性"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        (root / "wiki").mkdir()
        self.config = Config.from_env()
        self.config.wiki_root = root
        self.config.wiki_dir = root / "wiki"
        self.config.runtime_dir = root / ".llm-wiki"
        self.reviews_dir = self.config.wiki_dir / "REVIEW" / "confirm"
        self.reviews_dir.mkdir(parents=True)

    def _prepared(self, description: str) -> dict:
        return {
            "scope_version": review._REVIEW_SCOPE_VERSION,
            "new_page_refs": ["concepts/a.md"],
            "items_data": [{
                "type": "confirm",
                "title": self.TITLE,
                "description": description,
                "affected_pages": ["concepts/a.md"],
                "severity": "low",
                "search_queries": [],
            }],
            "stop_reason": "end_turn",
        }

    def _persist(self, description: str) -> dict:
        return review.stage_3_5_persist_review_suggestions(
            self._prepared(description), Path("raw/Book.pdf"), self.config)

    @staticmethod
    def _mark_resolved(page: Path) -> None:
        """Flip only the frontmatter line — the body boilerplate quotes it too."""
        page.write_text(
            page.read_text(encoding="utf-8").replace(
                "resolved: false", 'resolved: true\nresolved_reason: "Approve"',
                1),
            encoding="utf-8")

    def _path(self) -> Path:
        path, _rid = resolve_review_path(
            self.reviews_dir, "confirm", self.TITLE, "20260701")
        return path

    def test_resolved_page_survives_a_re_ingest(self):
        first = self._persist("first pass description")
        self.assertEqual(1, first["items"])
        page = self._path()
        self._mark_resolved(page)

        second = self._persist("re-ingest description")

        after = page.read_text(encoding="utf-8")
        self.assertTrue(is_resolved_review_file(page),
                        "re-ingest reset a triaged review to pending")
        self.assertIn("Approve", after)
        self.assertIn("first pass description", after)
        self.assertNotIn("re-ingest description", after)
        self.assertEqual(0, second["items"],
                         "a preserved page must not count as written")

    def test_pending_page_is_still_refreshed(self):
        self._persist("first pass description")
        page = self._path()

        self._persist("re-ingest description")

        after = page.read_text(encoding="utf-8")
        self.assertIn("re-ingest description", after)
        self.assertNotIn("first pass description", after)


if __name__ == "__main__":
    unittest.main()
