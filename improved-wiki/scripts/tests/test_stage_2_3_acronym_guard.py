"""Stage 2.3 cross-domain acronym guard regression.

Live failure (2026-07-02, 《直升机多普勒导航雷达原理》): _stage_2_title_words
strips CJK characters entirely, so the new concept "RAM 片选信号软件控制"
(computer-memory chip-select logic) and the existing page 雷达吸波材料-ram
(Radar Absorbing Material) both tokenized to {"ram"} → title Jaccard 1.0 →
Stage 2.3 flagged the concept ALREADY COVERED and the generation prompt
linked computer-memory pages to the radar-absorbing-material page.

Guard: reject a Jaccard match whose only shared evidence is short ASCII
tokens (<=4 chars) while the two names carry disjoint CJK parts. Exact
slug-form matches and full-name matches keep working.

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


def _write_page(wiki: Path, type_dir: str, slug: str, title: str) -> None:
    (wiki / type_dir).mkdir(parents=True, exist_ok=True)
    (wiki / type_dir / f"{slug}.md").write_text(
        f"---\ntype: {type_dir[:-1]}\ntitle: \"{title}\"\n---\n\nbody\n",
        encoding="utf-8",
    )


class Stage23AcronymGuardDetect(unittest.TestCase):
    def test_cross_domain_acronym_not_matched(self):
        """RAM 片选 (computer memory) must NOT match 雷达吸波材料-ram (radar)."""
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d)
            _write_page(wiki, "concepts", "雷达吸波材料-ram", "雷达吸波材料 (RAM)")
            chunks = [{"concepts_found": [{"name": "RAM 片选信号软件控制"}],
                       "entities_found": []}]
            assoc = s23.stage_2_3_detect_incremental_associations(wiki, chunks)
            self.assertNotIn("RAM 片选信号软件控制", assoc)

    def test_exact_slug_form_still_matched(self):
        """Bare acronym with exact slug-form equality keeps matching (MTI→mti)."""
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d)
            _write_page(wiki, "concepts", "mti", "MTI")
            chunks = [{"concepts_found": [{"name": "MTI"}], "entities_found": []}]
            assoc = s23.stage_2_3_detect_incremental_associations(wiki, chunks)
            self.assertIn("MTI", assoc)
            self.assertIn("mti", assoc["MTI"])

    def test_full_name_jaccard_still_matched(self):
        """Full-name match with long shared tokens is untouched by the guard."""
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d)
            _write_page(wiki, "concepts", "placeholder", "Placeholder")
            _write_page(wiki, "entities", "david-k-barton", "David K. Barton")
            chunks = [{"concepts_found": [],
                       "entities_found": [{"name": "David K. Barton"}]}]
            assoc = s23.stage_2_3_detect_incremental_associations(wiki, chunks)
            self.assertIn("David K. Barton", assoc)
            self.assertIn("david-k-barton", assoc["David K. Barton"])

    def test_cjk_exact_slug_still_matched(self):
        """CJK exact slug-form equality is unaffected by the guard.

        (Title carries ASCII tokens; the pure-CJK-title case is covered by
        Stage23PureCjkExactMatch below — fix 2026-07-02.)
        """
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d)
            _write_page(wiki, "concepts", "多普勒频移", "多普勒频移 (Doppler Shift)")
            chunks = [{"concepts_found": [{"name": "多普勒频移"}],
                       "entities_found": []}]
            assoc = s23.stage_2_3_detect_incremental_associations(wiki, chunks)
            self.assertIn("多普勒频移", assoc)
            self.assertIn("多普勒频移", assoc["多普勒频移"])

    def test_shared_cjk_acronym_match_kept(self):
        """Acronym-token Jaccard where the CJK parts DO overlap keeps matching."""
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d)
            _write_page(wiki, "concepts", "雷达吸波材料-ram", "雷达吸波材料 (RAM)")
            chunks = [{"concepts_found": [{"name": "RAM 吸波材料"}],
                       "entities_found": []}]
            assoc = s23.stage_2_3_detect_incremental_associations(wiki, chunks)
            self.assertIn("RAM 吸波材料", assoc)
            self.assertIn("雷达吸波材料-ram", assoc["RAM 吸波材料"])


class Stage23PureCjkExactMatch(unittest.TestCase):
    """Fix 2026-07-02: `if not words: continue` ran BEFORE the exact
    slug-form comparison, so a page whose title tokenizes to an empty ASCII
    word set (pure-CJK title) could never be detected as ALREADY COVERED —
    even on an exact name↔slug match. Exact matches must not depend on
    tokenization; only the Jaccard branch needs non-empty words.
    """

    def test_pure_cjk_exact_slug_match_detected(self):
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d)
            _write_page(wiki, "concepts", "多普勒频移", "多普勒频移")
            chunks = [{"concepts_found": [{"name": "多普勒频移"}],
                       "entities_found": []}]
            assoc = s23.stage_2_3_detect_incremental_associations(wiki, chunks)
            self.assertIn("多普勒频移", assoc)
            self.assertIn("多普勒频移", assoc["多普勒频移"])

    def test_pure_cjk_non_match_still_not_detected(self):
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d)
            _write_page(wiki, "concepts", "多普勒频移", "多普勒频移")
            chunks = [{"concepts_found": [{"name": "变频器控制"}],
                       "entities_found": []}]
            assoc = s23.stage_2_3_detect_incremental_associations(wiki, chunks)
            self.assertNotIn("变频器控制", assoc)


class Stage23AcronymGuardResolve(unittest.TestCase):
    def test_cross_domain_acronym_connection_not_resolved(self):
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d)
            _write_page(wiki, "concepts", "雷达吸波材料-ram", "雷达吸波材料 (RAM)")
            chunks = [{"connections_to_existing_wiki": [
                {"existing_page": "RAM 片选信号软件控制", "relationship": "extends"},
            ]}]
            resolved = s23.stage_2_3_resolve_proposed_connections(wiki, chunks)
            self.assertEqual(resolved, [])

    def test_full_name_connection_still_resolved(self):
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d)
            _write_page(wiki, "entities", "david-k-barton", "David K. Barton")
            chunks = [{"connections_to_existing_wiki": [
                {"existing_page": "David K. Barton", "relationship": "cites"},
            ]}]
            resolved = s23.stage_2_3_resolve_proposed_connections(wiki, chunks)
            self.assertEqual(resolved, [
                {"slug": "entities/david-k-barton", "relationship": "cites"},
            ])


if __name__ == "__main__":
    unittest.main()
