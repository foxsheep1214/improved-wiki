"""Source identity and maintenance locking regressions from the workflow audit."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import shutil
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_ingest_events import _config
from test_cross_source_dedup import _make_wiki, _mock_llm
from _source_lifecycle import delete_source
from _source_identity import AmbiguousSourceError
from _progress import ProjectLock
from _maintenance_lock import maintenance_write_lock
import cross_source_dedup as ds


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def owned(root, name, source):
    return write(root, f'wiki/concepts/{name}.md',
                 '---\ntype: concept\nsources: ' + json.dumps([source]) + '\n---\nBody\n')


class SourceLifecycleSafety(unittest.TestCase):
    def test_delete_uses_path_and_extension_not_stem(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = _config(root)
            raw = write(root, 'raw/A/manual.pdf', 'A')
            write(root, 'raw/B/manual.pdf', 'B')
            write(root, 'raw/A/manual.docx', 'DOCX')
            target = owned(root, 'target', 'raw/A/manual.pdf')
            same_name = owned(root, 'same-name', 'raw/B/manual.pdf')
            same_stem = owned(root, 'same-stem', 'raw/A/manual.docx')
            delete_source(raw, cfg, keep_media=True)
            self.assertFalse(target.exists())
            self.assertTrue(same_name.exists())
            self.assertTrue(same_stem.exists())

    def test_delete_does_not_casefold_another_source_cache_key(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw = write(root, 'raw/Book/Manual.pdf', 'A')
            cache = write(root, '.llm-wiki/ingest-cache.json',
                          '{"entries":{"Book/manual.pdf":{"hash":"other-source"}}}')
            before = cache.read_bytes()
            delete_source(raw, _config(root), keep_media=True)
            self.assertEqual(cache.read_bytes(), before)

    def test_legacy_unique_basename_and_raw_relative_path_are_supported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw = write(root, 'raw/Book/manual.pdf', 'A')
            a = owned(root, 'legacy', 'manual.pdf')
            b = owned(root, 'relative', 'Book/manual.pdf')
            delete_source(raw, _config(root), keep_media=True)
            self.assertFalse(a.exists())
            self.assertFalse(b.exists())

    def test_ambiguous_legacy_reference_stops_before_any_page_or_cache_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw = write(root, 'raw/A/manual.pdf', 'A')
            write(root, 'raw/B/manual.pdf', 'B')
            owned(root, 'uncertain', 'manual.pdf')
            owned(root, 'exact', 'raw/A/manual.pdf')
            write(root, 'wiki/sources/A/manual.md', '# Source\n')
            write(root, '.llm-wiki/ingest-cache.json', '{"entries": {"A/manual.pdf": {"hash": "a"}}}')
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            with self.assertRaises(AmbiguousSourceError):
                delete_source(raw, _config(root), keep_media=True)
            for path, content in before.items():
                self.assertEqual((root/path).read_bytes(), content)

    def test_source_api_and_cli_refuse_active_project_writer(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw = write(root, 'raw/a.pdf', 'source')
            page = owned(root, 'a', 'raw/a.pdf')
            cfg = _config(root)
            with ProjectLock(cfg):
                with self.assertRaisesRegex(RuntimeError, 'writer is active'):
                    delete_source(raw, cfg)
                result = subprocess.run(
                    [sys.executable, str(Path(ds.__file__).parent/'ingest.py'),
                     '--delete', '--keep-media', str(raw)], cwd=root,
                    env=dict(os.environ, IMPROVED_WIKI_ROOT=str(root)),
                    capture_output=True, text=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('writer is active', result.stderr)
            self.assertTrue(page.exists())

    def test_reservation_blocks_delete_after_handoff_releases_physical_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw = write(root, 'raw/a.pdf', 'source')
            page = owned(root, 'a', 'raw/a.pdf')
            write(root, '.llm-wiki/spine-reservation.json', '{"source_path": "raw/a.pdf"}')
            with self.assertRaisesRegex(RuntimeError, 'reserved'):
                delete_source(raw, _config(root))
            self.assertTrue(page.exists())

    def test_dry_run_preserves_pages_without_taking_a_writer_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw = write(root, 'raw/a.pdf', 'source')
            page = owned(root, 'a', 'raw/a.pdf')
            cfg = _config(root)
            with ProjectLock(cfg):
                self.assertEqual(delete_source(raw, cfg, dry_run=True), 1)
            self.assertTrue(page.exists())


class DedupSnapshotSafety(unittest.TestCase):
    def test_later_conflict_keeps_audit_of_already_applied_group(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wiki = _make_wiki(root)
            owned(root, 'second-a', 'a.pdf')
            owned(root, 'second-b', 'b.pdf')
            groups = [{'slugs': pair, 'confidence': 'high', 'reason': 'fixture'}
                      for pair in (['paos', '聚磷菌'], ['second-a', 'second-b'])]
            llm = _mock_llm()
            calls = 0
            def merge(system, prompt):
                nonlocal calls
                calls += 1
                if calls == 2:
                    p = wiki/'concepts/second-a.md'
                    p.write_text(p.read_text() + '\nNEW_EVIDENCE\n')
                return llm(system, prompt)
            with mock.patch.object(ds, '_detect_groups', return_value=groups):
                with self.assertRaisesRegex(RuntimeError, 'Wiki changed'):
                    ds.run_phase2(root, merge, embedding_prefilter=False)
            report = json.loads((root/'.llm-wiki/dedup-report.json').read_text())
            self.assertTrue(report['partial'])
            self.assertEqual(len(report['phase2']['applied']), 1)
            self.assertTrue((wiki/'concepts/second-b.md').exists())

    def _lint_with_dedup_driver(self, root, body):
        driver = root / 'driver'
        driver.mkdir()
        scripts = Path(ds.__file__).parent
        for original in scripts.iterdir():
            if original.name == 'cross_source_dedup.py':
                continue
            if original.name == 'wiki-lint.sh':
                shutil.copyfile(original, driver/original.name)
            else:
                (driver/original.name).symlink_to(original)
        (driver/'cross_source_dedup.py').write_text(
            'import sys\nfrom pathlib import Path\n'
            f'sys.path[:0] = [{str(scripts)!r}, {str(Path(__file__).parent)!r}]\n'
            'from cross_source_dedup import run_phase2\n'
            'from test_cross_source_dedup import _mock_llm\n' + body)
        return subprocess.run(
            ['/bin/bash', str(driver/'wiki-lint.sh'), '--no-semantic',
             '--no-emit-review', '--no-fix', '--no-fix-links', '--no-sweep',
             '--no-delete-orphans'], cwd=root,
            env=dict(os.environ, IMPROVED_WIKI_ROOT=str(root)),
            capture_output=True, text=True, timeout=20)

    def test_lint_passes_real_lock_to_dedup_without_deadlocking(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_wiki(root)
            result = self._lint_with_dedup_driver(root,
                f'run_phase2(Path({str(root)!r}), _mock_llm(), embedding_prefilter=False)\n')
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertFalse((root/'wiki/entities/聚磷菌.md').exists())
            with ProjectLock(_config(root)):
                pass  # lint released the borrowed descriptor too

    def test_lint_propagates_dedup_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_wiki(root)
            result = self._lint_with_dedup_driver(root, 'raise SystemExit(2)\n')
            self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
            self.assertIn('lint remains incomplete', result.stderr)

    def test_changed_page_is_preserved_and_merge_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wiki = _make_wiki(root)
            target = wiki/'concepts/vfa.md'
            original_index = (wiki/'index.md').read_bytes()
            def detect(*args, **kwargs):
                target.write_text(target.read_text() + '\nNEW_EVIDENCE\n')
                return [{'slugs': ['paos', '聚磷菌'], 'confidence': 'high', 'reason': 'fixture'}]
            with mock.patch.object(ds, '_detect_groups', side_effect=detect):
                with self.assertRaisesRegex(RuntimeError, 'Wiki changed'):
                    ds.run_phase2(root, _mock_llm(), embedding_prefilter=False)
            self.assertIn('NEW_EVIDENCE', target.read_text())
            self.assertTrue((wiki/'entities/聚磷菌.md').exists())
            self.assertEqual((wiki/'index.md').read_bytes(), original_index)

    def test_new_referring_page_during_merge_is_not_left_with_deleted_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_wiki(root)
            llm = _mock_llm()
            def concurrent_llm(system, prompt):
                if 'likely refer to the same' not in system:
                    owned(root, 'new-link', 'a.pdf').write_text('See [[聚磷菌]].\n')
                return llm(system, prompt)
            with self.assertRaisesRegex(RuntimeError, 'Wiki changed'):
                ds.run_phase2(root, concurrent_llm, embedding_prefilter=False)
            self.assertTrue((root/'wiki/entities/聚磷菌.md').exists())

    def test_standalone_dedup_joins_project_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_wiki(root)
            with ProjectLock(_config(root)):
                with self.assertRaisesRegex(RuntimeError, 'writer is active'):
                    ds.run_phase2(root, _mock_llm(), embedding_prefilter=False)

    def test_inherited_descriptor_remains_locked_after_child_maintenance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_wiki(root)
            cfg = _config(root)
            cfg.runtime_dir.mkdir()
            lock = ProjectLock(cfg)
            self.assertTrue(lock.acquire())
            try:
                code = ('import sys; from pathlib import Path; '
                        'sys.path[:0] = sys.argv[1:3]; '
                        'from test_cross_source_dedup import _mock_llm; '
                        'from cross_source_dedup import run_phase2; '
                        'run_phase2(Path(sys.argv[3]), _mock_llm(), embedding_prefilter=False)')
                result = subprocess.run(
                    [sys.executable, '-c', code, str(Path(ds.__file__).parent),
                     str(Path(__file__).parent), str(root)],
                    env=dict(os.environ, IMPROVED_WIKI_PROJECT_LOCK_FD=str(lock._fd)),
                    pass_fds=(lock._fd,), capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                contender = ProjectLock(cfg)
                self.assertFalse(contender.acquire())
                self.assertFalse((root/'wiki/entities/聚磷菌.md').exists())
            finally:
                lock.release()

    def test_fake_or_wrong_project_descriptor_cannot_bypass_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = _config(root)
            cfg.runtime_dir.mkdir()
            (cfg.runtime_dir/'ingest.lock').touch()
            with open(root/'unrelated', 'w') as f:
                with mock.patch.dict(os.environ, IMPROVED_WIKI_PROJECT_LOCK_FD=str(f.fileno())):
                    with self.assertRaisesRegex(RuntimeError, 'another project'):
                        with maintenance_write_lock(cfg):
                            self.fail('incorrect lock accepted')


if __name__ == '__main__':
    unittest.main()
