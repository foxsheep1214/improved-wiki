"""Source moves preserve immutable history and current-path validation."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _ingest_events import (
    IngestEventError, _project_source_reclassifications,
    append_ingest_event, ingest_events_for_source, load_ingest_events,
)

def event(source="raw/Paper/A/x.pdf", sha="a" * 64):
    return dict(schema_version=1, event="ingest_completed", run_id="original",
                source=source, source_hash=sha,
                source_page="wiki/sources/" + source[4:-4] + ".md",
                completed_at="2026-09-25T00:00:00.000+08:00",
                completed_at_ms=1790265600000, mode="ingest")

def move(old="raw/Paper/A/x.pdf", new="raw/Paper/B/x.pdf"):
    result = event(new)
    result.update(event="repair_completed", run_id="move:"+old,
                  mode="repair", repair_kind="source_reclassification",
                  previous_source=old)
    return result

class ReclassificationHistory(unittest.TestCase):
    def test_ledger_immutable_and_run_time_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = SimpleNamespace(runtime_dir=Path(temp))
            original = event()
            append_ingest_event(cfg, original)
            append_ingest_event(cfg, move())
            path = Path(temp) / "ingest-events.jsonl"
            before = path.read_bytes()
            result = ingest_events_for_source(load_ingest_events(cfg), "raw/Paper/B/x.pdf")
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["run_id"], "original")
            self.assertEqual(result[0]["completed_at_ms"], original["completed_at_ms"])
            self.assertEqual(result[0]["source_at_event"], original["source"])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(json.loads(before.splitlines()[0]), original)
            self.assertFalse(append_ingest_event(cfg, original)[1])

    def test_hash_isolation_and_chained_moves(self):
        records = [event(), event(sha="b"*64), move(), move("raw/Paper/B/x.pdf", "raw/Paper/C/x.pdf")]
        result = _project_source_reclassifications(records)
        self.assertEqual(result[0]["source"], "raw/Paper/C/x.pdf")
        self.assertEqual(result[1]["source"], "raw/Paper/A/x.pdf")
        self.assertEqual(records[0]["source"], "raw/Paper/A/x.pdf")

    def test_rejects_conflicting_move_from_vacated_path(self):
        with self.assertRaises(IngestEventError):
            _project_source_reclassifications([event(), move(), move(new="raw/Paper/C/x.pdf")])

    def test_round_trip_and_another_move_are_valid(self):
        records = [event(), move(), move('raw/Paper/B/x.pdf', 'raw/Paper/A/x.pdf'),
                   move('raw/Paper/A/x.pdf', 'raw/Paper/C/x.pdf')]
        result = _project_source_reclassifications(records)
        self.assertTrue(all(e['source'] == 'raw/Paper/C/x.pdf' for e in result))
        self.assertEqual(result[0]['source_at_event'], 'raw/Paper/A/x.pdf')

    def test_new_ingest_at_reused_path_is_a_separate_lineage(self):
        new = event()
        new['run_id'] = 'second-incarnation'
        result = _project_source_reclassifications([
            event(), move(), new, move(new='raw/Paper/C/x.pdf')])
        self.assertEqual(result[0]['source'], 'raw/Paper/B/x.pdf')
        self.assertEqual(result[2]['source'], 'raw/Paper/C/x.pdf')

    def test_append_preflights_projection_without_rewriting_old_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = SimpleNamespace(runtime_dir=Path(temp))
            path = cfg.runtime_dir / 'ingest-events.jsonl'
            original_bytes = (json.dumps(event(), separators=(',', ':')) + '\n').encode()
            path.write_bytes(original_bytes)
            append_ingest_event(cfg, move())
            self.assertTrue(path.read_bytes().startswith(original_bytes))
            before = path.read_bytes()
            conflict = move(new='raw/Paper/C/x.pdf')
            conflict['run_id'] = 'conflicting-second-move'
            with self.assertRaises(IngestEventError):
                append_ingest_event(cfg, conflict)
            self.assertEqual(path.read_bytes(), before)
            append_ingest_event(cfg, move('raw/Paper/B/x.pdf','raw/Paper/A/x.pdf'))
            self.assertEqual(load_ingest_events(cfg)[0]['source'], 'raw/Paper/A/x.pdf')

    def test_rejects_invalid_paths(self):
        invalid = move(old="raw/../outside.pdf")
        with self.assertRaises(IngestEventError):
            _project_source_reclassifications([event(), invalid])

if __name__ == "__main__":
    unittest.main()
