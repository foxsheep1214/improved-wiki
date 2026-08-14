"""Stage 2.3 bare-surname under-specification guard.

The initials guard (test_stage_2_3_initials_guard.py) explicitly left one
case unblocked: "Bare surname on one side -> no initials evidence -> do not
block here". That case turned out to bite for real: the existing wiki had
``entities/taylor.md`` titled just "Taylor" (zero disambiguating info — no
initials in the slug OR the title). A new chunk's "J. W. Taylor" (fully
qualified, two initials) collapsed to {taylor} under word-Jaccard and
associated with it, allowing downstream generation to link the wrong entity.

Fix: when the EXISTING page's title carries ZERO distinguishing tokens beyond
a bare single-word surname, and the NEW name is a multi-part name with at
least one single-letter initial (i.e. strictly MORE specific), the existing
page provides no real evidence of being the same, specific person — block
the match. This does not touch the reverse case (new name is a bare surname,
existing page has initials) or genuinely-vague-on-both-sides matches, which
still need real semantic judgment, not a token heuristic.

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
    (wiki / "entities" / "taylor.md").write_text(
        '---\ntype: entity\ntitle: "Taylor"\n---\n\n'
        'T. T. Taylor, originator of the Taylor aperture distribution.\n',
        encoding="utf-8")
    (wiki / "entities" / "t-t-taylor.md").write_text(
        '---\ntype: entity\ntitle: "T. T. Taylor"\n---\n\nAlso covers Taylor.\n',
        encoding="utf-8")
    return wiki


def _detect(wiki: Path, entity_name: str) -> dict:
    chunks = [{"concepts_found": [], "entities_found": [{"name": entity_name}]}]
    return s23.stage_2_3_detect_incremental_associations(wiki, chunks)


class BareSurnameGuard(unittest.TestCase):
    def test_qualified_name_does_not_associate_with_bare_surname_page(self):
        with tempfile.TemporaryDirectory() as t:
            wiki = _mk_wiki(Path(t))
            assoc = _detect(wiki, "J. W. Taylor")
            matches = assoc.get("J. W. Taylor", [])
            self.assertNotIn("entities/taylor", matches)

    def test_already_initialed_existing_page_still_associates_normally(self):
        # t-t-taylor.md legitimately carries initials — unaffected by this
        # guard; a genuinely matching initialed name still associates.
        with tempfile.TemporaryDirectory() as t:
            wiki = _mk_wiki(Path(t))
            assoc = _detect(wiki, "T. T. Taylor")
            self.assertIn(
                "entities/t-t-taylor",
                assoc.get("T. T. Taylor", []),
            )

    def test_bare_name_still_associates_with_bare_existing_page(self):
        # Both sides equally vague — not this guard's problem to solve;
        # existing exact-slug-form behavior is preserved.
        with tempfile.TemporaryDirectory() as t:
            wiki = _mk_wiki(Path(t))
            assoc = _detect(wiki, "Taylor")
            self.assertIn("entities/taylor", assoc.get("Taylor", []))

    def test_guard_helper_semantics(self):
        self.assertTrue(
            s23._stage_2_3_bare_surname_mismatch("J. W. Taylor", "Taylor"))
        self.assertFalse(
            s23._stage_2_3_bare_surname_mismatch("T. T. Taylor", "T. T. Taylor"))
        self.assertFalse(
            s23._stage_2_3_bare_surname_mismatch("Taylor", "Taylor"))
        # Reverse direction (new name bare, existing has initials) is out of
        # scope for this guard — leave it alone.
        self.assertFalse(
            s23._stage_2_3_bare_surname_mismatch("Taylor", "T. T. Taylor"))


if __name__ == "__main__":
    unittest.main()
