"""Caption evidence tolerates common VLM JSON shape drift without losing data."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _caption_evidence import FIELDS, parse_caption  # noqa: E402


class TestCaptionSchemaCompatibility(unittest.TestCase):
    def test_requested_shape_is_unchanged(self):
        response = {'summary': 'A labelled plot.', **{field: [] for field in FIELDS}}
        response['axes'] = ['x: time', 'y: voltage']
        self.assertEqual(parse_caption(json.dumps(response)), response)

    def test_scalar_null_missing_and_object_evidence(self):
        response = {
            'summary': 'A labelled plot.',
            'axes': {'x': 'time (s)', 'y': 'voltage (V)'},
            'legend': 'blue: output',
            'relationships': None,
            'formulas': [{'visible': 'V=IR'}, 'f=1/T'],
            'uncertainties': '',
        }
        result = parse_caption(json.dumps(response))
        self.assertEqual(result['visible_text'], [])
        self.assertEqual(result['axes'], [json.dumps(response['axes'])])
        self.assertEqual(result['legend'], ['blue: output'])
        self.assertEqual(result['relationships'], [])
        self.assertEqual(result['formulas'], [json.dumps({'visible': 'V=IR'}), 'f=1/T'])
        self.assertEqual(result['uncertainties'], [])
        self.assertTrue(all(isinstance(item, str) for field in FIELDS
                            for item in result[field]))

    def test_unsupported_scalar_still_fails(self):
        with self.assertRaisesRegex(ValueError, 'caption axes'):
            parse_caption(json.dumps({'summary': 'A plot.', 'axes': 42}))

    def test_summary_still_required(self):
        with self.assertRaisesRegex(ValueError, 'caption summary'):
            parse_caption(json.dumps({'axes': []}))


if __name__ == '__main__':
    unittest.main()
