"""Stage 2.3 person-initials guard (2026-07-09, Hansen live failure).

_stage_2_title_words drops single-letter tokens, so "W. W. Hansen" and
"J. P. Hansen" both tokenize to {hansen} → Jaccard 1.0 → Stage 2.3 associated
the 1938 Hansen-Woodyard co-originator (W. W. Hansen) with an existing page
about J. P. Hansen (an NRL sea-clutter researcher from a different book), and
generation wikilinked [[entities/j-p-hansen]] from the Hansen-Woodyard concept
page — factual corruption, found live in the Hansen re-ingest review.

Fix: when BOTH the new name and the existing slug carry single-letter
initials and the initial sets are disjoint, the match is a different-person
collision — block it (same shape as the cross-domain acronym guard).

Stdlib unittest only.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _stage_2_3_incremental as s23  # noqa: E402


def _mk_wiki(tmp: Path) -> Path:
    wiki = tmp / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "entities").mkdir(parents=True)
    (wiki / "concepts" / "placeholder.md").write_text(
        '---\ntype: concept\ntitle: "Placeholder"\n---\n\nx\n', encoding="utf-8")
    (wiki / "entities" / "j-p-hansen.md").write_text(
        '---\ntype: entity\ntitle: "J. P. Hansen"\n---\n\nNRL sea-clutter researcher.\n',
        encoding="utf-8")
    return wiki


def _detect(wiki: Path, entity_name: str) -> dict:
    chunks = [{"concepts_found": [], "entities_found": [{"name": entity_name}]}]
    return s23.stage_2_3_detect_incremental_associations(wiki, chunks)


class InitialsGuard(unittest.TestCase):
    def test_different_initials_same_surname_do_not_associate(self):
        with tempfile.TemporaryDirectory() as t:
            wiki = _mk_wiki(Path(t))
            assoc = _detect(wiki, "W. W. Hansen")
            self.assertNotIn("W. W. Hansen", assoc)

    def test_same_initials_still_associate(self):
        with tempfile.TemporaryDirectory() as t:
            wiki = _mk_wiki(Path(t))
            assoc = _detect(wiki, "J. P. Hansen")
            self.assertIn("J. P. Hansen", assoc)
            self.assertIn("j-p-hansen", assoc["J. P. Hansen"])

    def test_guard_helper_semantics(self):
        self.assertTrue(s23._stage_2_3_initials_mismatch("W. W. Hansen", "j-p-hansen"))
        self.assertFalse(s23._stage_2_3_initials_mismatch("J. P. Hansen", "j-p-hansen"))
        # Bare surname on one side → no initials evidence → do not block here
        # (surname-only ambiguity is out of scope for this guard).
        self.assertFalse(s23._stage_2_3_initials_mismatch("Hansen", "j-p-hansen"))
        self.assertFalse(s23._stage_2_3_initials_mismatch("W. W. Hansen", "hansen"))


if __name__ == "__main__":
    unittest.main()
