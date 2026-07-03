"""Tests for Stage 2.7 query-generation skip on datasheet/standard sources.

Regression for the bug where ``detect_template_type(file_path, config)`` was
called with the wrong arity (``config`` passed where ``raw_root`` is expected).
It always raised, was swallowed to ``src_type = None``, and the datasheet/
standard skip never fired — so query pages were generated even for pure-fact
sources. The fix prefers the already-resolved ``template_name`` (honoring
``--type``) and falls back to a correct
``detect_template_type(file_path, config.raw_root, None)`` call, normalizing the
``digest-`` prefix before comparing.

Run:  python3 scripts/tests/test_stage_2_7_skip.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import _core  # noqa: E402
from _stage_2_7_query_generation import stage_2_7_query_generation  # noqa: E402

# Minimal inputs the skip path returns before ever reading. A single concept
# block ensures the "no concepts generated" skip is not what returns — this
# isolates the datasheet/standard *type* skip under test.
_DIGEST = {"concepts": []}
_CHUNKS: list[dict] = []
_BLOCKS = [("wiki/concepts/x.md", "---\ntitle: X\n---\nbody")]


class TestStage27TypeSkip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._prev_root = os.environ.get("IMPROVED_WIKI_ROOT")
        os.environ["IMPROVED_WIKI_ROOT"] = str(self.root)
        self.config = _core.Config.from_env()

    def tearDown(self):
        if self._prev_root is None:
            os.environ.pop("IMPROVED_WIKI_ROOT", None)
        else:
            os.environ["IMPROVED_WIKI_ROOT"] = self._prev_root
        self._tmp.cleanup()

    def _call(self, file_path, template_name):
        return stage_2_7_query_generation(
            _DIGEST, _CHUNKS, _BLOCKS, file_path, self.config,
            template_name=template_name,
        )

    def test_skip_prefixed_datasheet(self):
        # Folder detection yields a "digest-"-prefixed template name.
        self.assertEqual(self._call(self.root / "raw/Datasheet/x.pdf", "digest-datasheet"), ([], ""))

    def test_skip_bare_datasheet_override(self):
        # `--type datasheet` is bare; override is returned verbatim.
        self.assertEqual(self._call(self.root / "raw/anything/x.pdf", "datasheet"), ([], ""))

    def test_skip_prefixed_standard(self):
        self.assertEqual(self._call(self.root / "raw/Standard/x.pdf", "digest-standard"), ([], ""))

    def test_skip_via_path_fallback_when_no_template_name(self):
        # template_name="" → correct detect_template_type(file, config.raw_root, None).
        # The pre-fix call raised here and never skipped.
        dsheet = self.root / "raw" / "Datasheet" / "x.pdf"
        dsheet.parent.mkdir(parents=True, exist_ok=True)
        dsheet.write_text("x", encoding="utf-8")
        self.assertEqual(self._call(dsheet, ""), ([], ""))


if __name__ == "__main__":
    unittest.main()
