"""Evidence is scoped to a source identity, content version and completed run."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _evidence_ledger import write_evidence_ledger, load_ledgers
from _ingest_events import append_ingest_event
from _progress import file_sha256
from evidence_lookup import lookup
from test_source_reclassification_history import event, move


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def cfg(root):
    return SimpleNamespace(runtime_dir=root/'.llm-wiki', wiki_root=root)


def evidence(root, source, content, run_id, *, complete=True):
    raw = write(root, source, content)
    sha = file_sha256(raw)
    result = write_evidence_ledger(cfg(root), source, sha,
        [{'claims': [{'claim': content, 'evidence': 'section 1'}]}], [], run_id=run_id)
    if complete:
        e = event(source, sha)
        e['run_id'] = run_id
        append_ingest_event(cfg(root), e)
        write(root, f'.llm-wiki/ingest-progress/{sha[:16]}.stages.json', json.dumps({
            'ingested': e['completed_at_ms'], 'ingested__payload': {'run_id': run_id}}))
    return result, sha


def page(root, sources):
    write(root, 'wiki/concepts/x.md', '---\ntype: concept\nsources: '
          + json.dumps(sources) + '\n---\nContent\n')


class EvidenceVersions(unittest.TestCase):
    def test_page_query_hashes_only_its_requested_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            evidence(root, 'raw/a.pdf', 'A', 'run-a')
            evidence(root, 'raw/b.pdf', 'B', 'run-b')
            page(root, ['raw/a.pdf'])
            with mock.patch('evidence_lookup.file_sha256', wraps=file_sha256) as hashing:
                self.assertEqual(lookup(root, page='concepts/x')['sources'], ['raw/a.pdf'])
            self.assertEqual(hashing.call_count, 1)
            self.assertEqual(hashing.call_args.args[0].name, 'a.pdf')

    def test_exact_source_path_excludes_same_named_other_document(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            evidence(root, 'raw/A/manual.pdf', 'A_ONLY', 'run-a')
            evidence(root, 'raw/B/manual.pdf', 'B_ONLY', 'run-b')
            page(root, ['raw/A/manual.pdf'])
            result = lookup(root, page='concepts/x')
            self.assertEqual([m['text'] for m in result['matches']], ['A_ONLY'])
            self.assertEqual(result['sources'], ['raw/A/manual.pdf'])

    def test_missing_exact_source_does_not_fall_back_to_wrong_same_name(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            evidence(root, 'raw/B/manual.pdf', 'B_ONLY', 'run-b')
            page(root, ['raw/A/manual.pdf'])
            result = lookup(root, page='concepts/x')
            self.assertFalse(result['matches'])
            self.assertEqual(result['missing_sources'], ['raw/A/manual.pdf'])

    def test_ambiguous_legacy_source_is_reported_not_combined(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            evidence(root, 'raw/A/manual.pdf', 'A', 'run-a')
            evidence(root, 'raw/B/manual.pdf', 'B', 'run-b')
            page(root, ['manual.pdf'])
            result = lookup(root, page='concepts/x')
            self.assertFalse(result['matches'])
            self.assertIn('manual.pdf', result['ambiguous_sources'])

    def test_unique_legacy_and_block_yaml_sources_work(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            evidence(root, 'raw/A/manual.pdf', 'A', 'run-a')
            write(root, 'wiki/concepts/x.md', '---\nsources:\n  - manual.pdf\n---\nBody\n')
            self.assertEqual(lookup(root, page='concepts/x')['sources'], ['raw/A/manual.pdf'])

    def test_reingest_returns_current_version_history_has_both_identified(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old, old_sha = evidence(root, 'raw/a.pdf', 'OBSOLETE', 'old')
            old_bytes = old.read_bytes()
            new, new_sha = evidence(root, 'raw/a.pdf', 'CORRECTED', 'new')
            page(root, ['raw/a.pdf'])
            result = lookup(root, page='concepts/x')
            self.assertEqual([m['text'] for m in result['matches']], ['CORRECTED'])
            self.assertEqual(result['matches'][0]['run_id'], 'new')
            self.assertEqual(result['matches'][0]['source_hash'], new_sha)
            self.assertTrue(result['matches'][0]['completed_at'])
            history = lookup(root, page='concepts/x', history=True)['matches']
            self.assertEqual({m['source_hash'] for m in history}, {old_sha, new_sha})
            self.assertEqual({m['evidence_status'] for m in history}, {'stale', 'current'})
            self.assertEqual(old.read_bytes(), old_bytes)

    def test_uncompleted_source_is_never_reported_as_current(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            evidence(root, 'raw/a.pdf', 'INCOMPLETE', 'pending', complete=False)
            result = lookup(root, source='a.pdf')
            self.assertFalse(result['matches'])
            self.assertEqual(result['unavailable_sources'][0]['evidence_status'], 'uncompleted')
            self.assertEqual(lookup(root, source='a.pdf', history=True)['matches'][0]['run_id'], 'pending')

    def test_event_without_completion_marker_is_not_current(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, sha = evidence(root, 'raw/a.pdf', 'A', 'run-a')
            (root/f'.llm-wiki/ingest-progress/{sha[:16]}.stages.json').unlink()
            self.assertFalse(lookup(root, source='a.pdf')['matches'])

    def test_same_hash_reingest_and_two_sources_do_not_overwrite_ledgers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a, _ = evidence(root, 'raw/a.pdf', 'SAME_BYTES', 'old')
            before = a.read_bytes()
            b, _ = evidence(root, 'raw/a.pdf', 'SAME_BYTES', 'new')
            c, _ = evidence(root, 'raw/b.pdf', 'SAME_BYTES', 'other', complete=False)
            self.assertEqual(len({a,b,c}), 3)
            self.assertEqual(a.read_bytes(), before)
            current = lookup(root, source='raw/a.pdf')['matches']
            self.assertEqual([m['run_id'] for m in current], ['new'])

    def test_deleted_source_remains_only_in_explicit_history(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            evidence(root, 'raw/a.pdf', 'A', 'run-a')
            (root/'raw/a.pdf').unlink()
            self.assertFalse(lookup(root, source='a.pdf')['matches'])
            history = lookup(root, source='a.pdf', history=True)
            self.assertEqual(history['matches'][0]['evidence_status'], 'missing_source')

    def test_reclassification_keeps_version_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, sha = evidence(root, 'raw/Paper/A/x.pdf', 'A', 'run-a')
            target = root/'raw/Paper/B/x.pdf'
            target.parent.mkdir(parents=True)
            (root/'raw/Paper/A/x.pdf').rename(target)
            relocation = move()
            relocation['source_hash'] = sha
            append_ingest_event(cfg(root), relocation)
            page(root, ['raw/Paper/B/x.pdf'])
            result = lookup(root, page='concepts/x')
            self.assertEqual(result['matches'][0]['source'], 'raw/Paper/B/x.pdf')
            self.assertEqual(result['matches'][0]['source_at_evidence'], 'raw/Paper/A/x.pdf')

    def test_v1_unbound_ledger_is_available_as_history_not_current(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path, _ = evidence(root, 'raw/a.pdf', 'A', 'run-a')
            record = json.loads(path.read_text())
            record.pop('run_id')
            record['schema_version'] = 1
            path.write_text(json.dumps(record))
            self.assertFalse(lookup(root, source='a.pdf')['matches'])
            self.assertEqual(len(lookup(root, source='a.pdf', history=True)['matches']), 1)

    def test_writer_uses_active_run_from_task_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sha = 'a'*64
            write(root, f'.llm-wiki/ingest-progress/{sha[:16]}.task.json',
                  '{"run": {"run_id": "manifest-run"}}')
            path = write_evidence_ledger(cfg(root), 'raw/a.pdf', sha,
                [{'claims': [{'claim': 'A'}]}], [])
            self.assertEqual(json.loads(path.read_text())['run_id'], 'manifest-run')


if __name__ == '__main__':
    unittest.main()
