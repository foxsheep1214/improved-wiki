"""Tests for normalize_raw_names._parse_simple_yaml — block-list parsing.

The NAMING.md rules block uses YAML block-list syntax (``- item`` on its own
line). The parser previously skipped any line without a colon, so block lists
were silently dropped → ``vendors`` came back as an empty dict and every
datasheet was falsely flagged "未识别的 Vendor".
"""
import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import normalize_raw_names as nrn  # noqa: E402


BLOCK_YAML = """\
vendors:
  - TI
  - ADI
  - ST
rules:
  Datasheet:
    pattern: "Vendor - PartNumber"
    min_parts: 2
    vendor_field: 0
vendor_prefixes:
  TI:
    - ADC - ADS - AFE
    - TMAG - TMP
"""

INLINE_YAML = """\
vendors: - TI - ADI - ST
rules:
  Datasheet:
    min_parts: 2
    vendor_field: 0
"""


class TestParseSimpleYaml(unittest.TestCase):
    def test_block_list_vendors_parsed(self):
        rules = nrn._parse_simple_yaml(BLOCK_YAML)
        self.assertEqual(rules["vendors"], ["TI", "ADI", "ST"])

    def test_block_list_vendor_prefixes_parsed(self):
        rules = nrn._parse_simple_yaml(BLOCK_YAML)
        self.assertEqual(rules["vendor_prefixes"]["TI"],
                         ["ADC", "ADS", "AFE", "TMAG", "TMP"])

    def test_block_scalars_and_nested_dict(self):
        rules = nrn._parse_simple_yaml(BLOCK_YAML)
        self.assertEqual(rules["rules"]["Datasheet"]["min_parts"], 2)
        self.assertEqual(rules["rules"]["Datasheet"]["vendor_field"], 0)
        self.assertEqual(rules["rules"]["Datasheet"]["pattern"],
                         "Vendor - PartNumber")

    def test_inline_list_still_supported(self):
        rules = nrn._parse_simple_yaml(INLINE_YAML)
        self.assertEqual(rules["vendors"], ["TI", "ADI", "ST"])

    def test_last_duplicate_key_wins(self):
        # NAMING.md has two `vendors:` blocks; YAML semantics = last wins.
        text = ("vendors:\n  - TI\n  - ADI\nrules:\n  Datasheet:\n"
                "    min_parts: 2\nvendors:\n  - TI\n  - AMD\n")
        rules = nrn._parse_simple_yaml(text)
        self.assertEqual(rules["vendors"], ["TI", "AMD"])


if __name__ == "__main__":
    unittest.main()
