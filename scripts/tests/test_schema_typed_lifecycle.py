"""Tests for the schema-typed generation lifecycle."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _ingest_prepare as prepare  # noqa: E402
import _stage_2_3_incremental as stage23  # noqa: E402
from _core import slugify  # noqa: E402


_RESEARCH_SCHEMA = """\
## Page Types

| Type | Directory |
|---|---|
| source | wiki/sources/ |
| concept | wiki/concepts/ |
| comparison | wiki/comparisons/ |
| synthesis | wiki/synthesis/ |
| thesis | wiki/thesis/ |
| methodology | wiki/methodology/ |
| finding | wiki/findings/ |
"""


def _write_page(wiki: Path, route: str, stem: str, page_type: str, title: str) -> None:
    (wiki / route).mkdir(parents=True, exist_ok=True)
    (wiki / route / f"{stem}.md").write_text(
        f"---\ntype: {page_type}\ntitle: \"{title}\"\n---\n\nbody\n",
        encoding="utf-8",
    )


class TestUnifiedTypedLifecycle(unittest.TestCase):
    def test_comparisons_are_counted_from_stage_2_4_file_blocks(self):
        blocks = [
            ("concepts/a.md", "a"),
            ("wiki/comparisons/a-vs-b.md", "ab"),
            ("comparisons/c-vs-d.md", "cd"),
            ("synthesis/combined.md", "s"),
        ]
        self.assertEqual(prepare._count_comparison_blocks(blocks), 2)

    def test_all_research_types_deduplicate_in_their_declared_route(self):
        cases = (
            ("comparison", "comparisons", "Antenna A versus Antenna B"),
            ("synthesis", "synthesis", "Unified Aperture View"),
            ("thesis", "thesis", "Bandwidth Predicts Resolution"),
            ("methodology", "methodology", "Calibrated Range Measurement"),
            ("finding", "findings", "Observed Sidelobe Reduction"),
        )
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d)
            candidates = []
            for page_type, route, title in cases:
                stem = slugify(title)
                _write_page(wiki, route, stem, page_type, title)
                candidates.append({"name": title, "type": page_type})

            associations = stage23.stage_2_3_detect_incremental_associations(
                wiki,
                [{"schema_typed_candidates": candidates}],
                schema_text=_RESEARCH_SCHEMA,
            )

            for page_type, route, title in cases:
                self.assertEqual(
                    associations[title],
                    [f"{route}/{slugify(title)}"],
                    page_type,
                )

    def test_typed_candidate_is_not_suppressed_by_same_named_concept(self):
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d)
            title = "Observed Sidelobe Reduction"
            _write_page(wiki, "concepts", slugify(title), "concept", title)
            associations = stage23.stage_2_3_detect_incremental_associations(
                wiki,
                [{
                    "concepts_found": [{"name": title}],
                    "schema_typed_candidates": [{"name": title, "type": "finding"}],
                }],
                schema_text=_RESEARCH_SCHEMA,
            )
            self.assertNotIn(title, associations)

    def test_proposed_connections_scan_every_declared_research_route(self):
        with tempfile.TemporaryDirectory() as d:
            wiki = Path(d)
            _write_page(
                wiki,
                "methodology",
                "calibrated-range-measurement",
                "methodology",
                "Calibrated Range Measurement",
            )
            resolved = stage23.stage_2_3_resolve_proposed_connections(
                wiki,
                [{"connections_to_existing_wiki": [{
                    "existing_page": "Calibrated Range Measurement",
                    "relationship": "applies",
                }]}],
                schema_text=_RESEARCH_SCHEMA,
            )
            self.assertEqual(resolved, [{
                "slug": "methodology/calibrated-range-measurement",
                "relationship": "applies",
            }])


if __name__ == "__main__":
    unittest.main()
