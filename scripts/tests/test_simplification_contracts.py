"""Cross-entry contracts: identity, YAML, pure detection, migration and exec locks."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import select
import subprocess
import sys
from types import SimpleNamespace

import pytest

from _context_budget import CONTEXT_ENV, apply_context_budget, context_tokens
from _frontmatter import parse_frontmatter, write_frontmatter
from _frontmatter_array import merge_array_fields_into_content, parse_frontmatter_array
from _paths import detect_runtime_dir
from _progress import ProjectLock, file_sha256
from _source_identity import AmbiguousSourceError, SourceResolver
from _stage_3_write import _stage_3_2_canonicalize_sources_field, _stage_3_2_is_owned_only_by_source
from migrate_runtime import migrate
import validate_ingest as validator

SCRIPTS = Path(__file__).resolve().parents[1]


def put(path, text='{}'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def page(sources):
    return write_frontmatter({'type': 'concept', 'sources': sources}, 'Body\n')


def tree_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('other', ['raw/B/manual.pdf', 'raw/A/Manual.pdf', 'raw/sources/A/manual.pdf'])
def test_writer_retains_distinct_full_source_identities(tmp_path, other):
    original = 'raw/A/manual.pdf'
    resolver = SourceResolver(tmp_path, [original, other])
    out = _stage_3_2_canonicalize_sources_field(page([original]), other, resolver)
    assert parse_frontmatter_array(out, 'sources') == [original, other]
    assert not _stage_3_2_is_owned_only_by_source(out, other, resolver)
    merged = merge_array_fields_into_content(page([other]), page([original]), ['sources'])
    assert parse_frontmatter_array(merged, 'sources') == [original, other]


def test_writer_legacy_unique_alias_and_ambiguity(tmp_path):
    source = 'raw/A/manual.pdf'
    resolver = SourceResolver(tmp_path, [source])
    assert _stage_3_2_is_owned_only_by_source(page(['manual']), source, resolver)
    assert parse_frontmatter_array(_stage_3_2_canonicalize_sources_field(
        page(['manual.pdf']), source, resolver), 'sources') == [source]
    resolver = SourceResolver(tmp_path, [source, 'raw/B/manual.pdf'])
    with pytest.raises(AmbiguousSourceError):
        _stage_3_2_canonicalize_sources_field(page(['manual']), source, resolver)


def test_validator_never_selects_first_same_named_cache_entry(tmp_path, monkeypatch):
    cache = put(tmp_path / '.llm-wiki/ingest-cache.json', json.dumps({'entries': {
        'A/manual.pdf': {'hash': 'a'}, 'B/manual.pdf': {'hash': 'b'}}}))
    monkeypatch.setattr(validator, 'CACHE_PATH', cache)
    monkeypatch.setattr(validator, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(validator, 'CACHE_KEY', '')
    assert validator._validate_find_cache_entry('raw/B/manual.pdf')['hash'] == 'b'
    with pytest.raises(AmbiguousSourceError):
        validator._validate_find_cache_entry('manual')
    assert validator._validate_find_cache_entry('raw/C/manual.pdf') is None


def test_query_cache_identity_needs_unique_actual_source(tmp_path, monkeypatch):
    monkeypatch.setattr(validator, 'PROJECT_ROOT', tmp_path)
    put(tmp_path / 'wiki/queries/research.md')
    assert validator._validate_cache_source('queries/research.md') == 'wiki/queries/research.md'
    put(tmp_path / 'raw/queries/research.md')
    with pytest.raises(ValueError, match='Ambiguous'):
        validator._validate_cache_source('queries/research.md')


@pytest.mark.parametrize('tags', ['tags: [rf, "thermal, cooling"]', 'tags:\n  - rf\n  - "thermal, cooling"'])
def test_all_frontmatter_readers_agree_on_arrays_and_scalars(tags):
    from _lint_suggest import _lint_frontmatter
    from graph import _parse_frontmatter
    content = f'---\n{tags}\ncreated: 2026-09-25\nempty: null\nliteral: "null"\nactive: false\n---\nBody\n'
    fm, body = parse_frontmatter(content)
    assert fm['tags'] == ['rf', 'thermal, cooling']
    assert fm['created'] == '2026-09-25'
    assert fm['empty'] is None and fm['literal'] == 'null'
    assert fm['active'] == 'false'
    assert _lint_frontmatter(content) == fm
    assert _parse_frontmatter(content) == (fm, body)
    assert parse_frontmatter_array(content, 'tags') == fm['tags']
    assert parse_frontmatter(write_frontmatter(fm, body)) == (fm, body)


def test_invalid_frontmatter_does_not_invent_fields():
    assert parse_frontmatter('---\ntags: [broken\n---\nBody')[0] == {}


def test_detect_legacy_runtime_is_read_only_and_mixed_state_fails(tmp_path):
    put(tmp_path / '.iwiki-runtime/ingest-progress/shared.json', 'old')
    before = tree_bytes(tmp_path)
    assert detect_runtime_dir(tmp_path) == tmp_path / '.iwiki-runtime'
    assert tree_bytes(tmp_path) == before
    put(tmp_path / '.llm-wiki/ingest-progress/shared.json', 'new')
    before = tree_bytes(tmp_path)
    with pytest.raises(RuntimeError, match='Both'):
        detect_runtime_dir(tmp_path)
    assert tree_bytes(tmp_path) == before


def test_migration_conflict_is_non_mutating(tmp_path):
    put(tmp_path / '.iwiki-runtime/ingest-progress/shared.json', 'old')
    put(tmp_path / '.llm-wiki/ingest-progress/shared.json', 'new')
    before = tree_bytes(tmp_path)
    with pytest.raises(ValueError, match='Conflicting'):
        migrate(tmp_path, '.iwiki-runtime', apply=True)
    assert tree_bytes(tmp_path) == before


def test_migration_preview_and_identical_duplicate_apply(tmp_path):
    old = put(tmp_path / '.iwiki-runtime/ingest-progress/shared.json', 'same')
    new = put(tmp_path / '.llm-wiki/ingest-progress/shared.json', 'same')
    before = tree_bytes(tmp_path)
    assert len(migrate(tmp_path, '.iwiki-runtime')) == 1
    assert tree_bytes(tmp_path) == before
    migrate(tmp_path, '.iwiki-runtime', apply=True)
    assert not old.exists() and new.read_text() == 'same'
    assert (tmp_path / '.iwiki-runtime/ingest.lock').exists()
    assert detect_runtime_dir(tmp_path) == tmp_path / '.llm-wiki'


def test_legacy_wiki_migration_leaves_content_and_empty_content_dirs(tmp_path):
    put(tmp_path / 'wiki/.ingest-cache.json', '{"entries":{}}')
    content = put(tmp_path / 'wiki/concepts/c.md', 'untouched')
    (tmp_path / 'wiki/entities/empty').mkdir(parents=True)
    migrate(tmp_path, 'wiki', apply=True)
    assert content.read_text() == 'untouched'
    assert (tmp_path / 'wiki/entities/empty').is_dir()
    assert (tmp_path / '.llm-wiki/ingest-cache.json').read_text() == '{"entries":{}}'


def test_migration_rejects_symlinks(tmp_path):
    target = put(tmp_path / 'outside', 'leave')
    old = tmp_path / '.iwiki-runtime'
    old.mkdir()
    (old / 'ingest-cache.json').symlink_to(target)
    with pytest.raises(ValueError, match='Symlink'):
        migrate(tmp_path, '.iwiki-runtime', apply=True)
    assert target.read_text() == 'leave'


@pytest.mark.parametrize('guard', ['ingest.lock', 'worker.json.lease', 'watch.lock', 'ingest-queue.lock'])
def test_migration_refuses_active_writer_or_worker(tmp_path, guard):
    source = put(tmp_path / '.iwiki-runtime/ingest-cache.json', 'preserve')
    lock = put(source.parent / guard)
    with lock.open('a+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises((RuntimeError, BlockingIOError)):
            migrate(tmp_path, '.iwiki-runtime', apply=True)
    assert source.read_text() == 'preserve'
    assert not (tmp_path / '.llm-wiki/ingest-cache.json').exists()


def test_context_default_ignores_old_cache_and_writes_no_state(tmp_path, monkeypatch):
    monkeypatch.delenv(CONTEXT_ENV, raising=False)
    put(tmp_path / '.llm-wiki/probed-context.json', '{"context":1000000,"model":""}')
    before = tree_bytes(tmp_path)
    values = []
    apply_context_budget(SimpleNamespace(apply_context=values.append, runtime_dir=tmp_path / '.llm-wiki'))
    assert values == [64000]
    assert tree_bytes(tmp_path) == before
    monkeypatch.setenv(CONTEXT_ENV, '128000')
    assert context_tokens() == 128000
    assert context_tokens(256000) == 256000


@pytest.mark.parametrize('value', ['UNKNOWN', 'model-20260925', '128k', '64000.5', 128000.5, 0, 63999, 10000001])
def test_context_rejects_unverified_or_unsupported_values(value):
    with pytest.raises(ValueError):
        context_tokens(value)


def test_partial_write_context_change_preserves_manifest(tmp_path):
    from _config import Config
    from _task_manifest import TaskManifestError, ensure_task_manifest, task_manifest_path
    cfg = Config(wiki_root=tmp_path, raw_root=tmp_path/'raw', wiki_dir=tmp_path/'wiki',
                 runtime_dir=tmp_path/'.llm-wiki', cache_path=tmp_path/'.llm-wiki/ingest-cache.json',
                 progress_dir=tmp_path/'.llm-wiki/ingest-progress', extract_tmp_dir=tmp_path/'.llm-wiki/extract-tmp',
                 llm_model='', caption_api_key='', caption_base_url='', caption_model='',
                 chunk_overlap=3000, source_budget=100000, target_chars=60000, target_tokens=30000, max_tokens=8192)
    raw = put(tmp_path / 'raw/Book/file.pdf', 'source bytes')
    cfg.apply_context(128000)
    ensure_task_manifest(raw, cfg)
    digest = file_sha256(raw)
    path = task_manifest_path(cfg, digest)
    before = path.read_bytes()
    put(cfg.runtime_dir / f'write-ledger-{digest[:16]}.json', '{"pages":{"concepts/a.md":"written"}}')
    cfg.apply_context(64000)
    with pytest.raises(TaskManifestError, match='--context-tokens 128000'):
        ensure_task_manifest(raw, cfg)
    assert path.read_bytes() == before


def test_exec_lock_survives_child_and_releases_after_kill(tmp_path, monkeypatch):
    monkeypatch.delenv('IMPROVED_WIKI_PROJECT_LOCK_FD', raising=False)
    put(tmp_path / '.llm-wiki/ingest-events.jsonl', '')
    command = [sys.executable, str(SCRIPTS / '_maintenance_lock.py'), str(tmp_path),
               sys.executable, '-u', '-c', 'import sys; print("ready"); sys.stdin.read()']
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    contender = ProjectLock(SimpleNamespace(runtime_dir=tmp_path/'.llm-wiki'))
    try:
        assert select.select([child.stdout], [], [], 5)[0], 'child failed to become ready'
        assert child.stdout.readline().strip() == 'ready'
        assert not contender.acquire()
    finally:
        child.kill()
        child.communicate(timeout=5)
    assert contender.acquire()
    contender.release()


def test_forged_lint_reentry_cannot_skip_lock(tmp_path, monkeypatch):
    monkeypatch.delenv('IMPROVED_WIKI_PROJECT_LOCK_FD', raising=False)
    env = dict(os.environ, IMPROVED_WIKI_ROOT=str(tmp_path), PATH=str(Path(sys.executable).parent)+os.pathsep+os.environ['PATH'])
    result = subprocess.run(['bash', str(SCRIPTS/'wiki-lint.sh'), '--internal-locked', '--structural-only'], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0 and 'No inherited project lock' in result.stderr


def test_shell_and_python_select_ledger_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv('IMPROVED_WIKI_PROJECT_LOCK_FD', raising=False)
    put(tmp_path / '.llm-wiki/ingest-events.jsonl', '')
    legacy = put(tmp_path / 'wiki/ingest-cache.json', '{"legacy":true}')
    put(tmp_path / 'wiki/concepts/a.md', '---\ntype: concept\ntitle: A\n---\nBody')
    env = dict(os.environ, IMPROVED_WIKI_ROOT=str(tmp_path), IMPROVED_WIKI_PYTHON=sys.executable)
    result = subprocess.run(['bash', str(SCRIPTS/'wiki-lint.sh'), '--structural-only'], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert detect_runtime_dir(tmp_path) == tmp_path / '.llm-wiki'
    assert (tmp_path / '.llm-wiki/ingest.lock').exists()
    assert not (tmp_path / 'wiki/ingest.lock').exists()
    assert legacy.read_text() == '{"legacy":true}'


def test_hidden_legacy_state_requires_explicit_migration(tmp_path):
    put(tmp_path / 'wiki/.ingest-cache.json', '{"entries":{}}')
    before = tree_bytes(tmp_path)
    with pytest.raises(RuntimeError, match='--from wiki'):
        detect_runtime_dir(tmp_path)
    assert tree_bytes(tmp_path) == before


def test_migration_refuses_durable_spine_reservation(tmp_path):
    put(tmp_path / '.iwiki-runtime/spine-reservation.json', '{"source_hash":"reserved"}')
    with pytest.raises(RuntimeError, match='Reserved write spine'):
        migrate(tmp_path, '.iwiki-runtime', apply=True)
    assert not (tmp_path / '.llm-wiki/spine-reservation.json').exists()


@pytest.mark.parametrize('kind', ['book', 'paper', 'datasheet', 'applicationnote',
                                 'designexample', 'presentation', 'standard', 'news'])
def test_each_domain_template_is_included_in_both_prompts(tmp_path, kind):
    from _stage_2_analyze import _stage_2_2_build_template_section
    from _stage_2_4_generation import _stage_2_4_build_all_prompt
    template = (SCRIPTS.parent / f'templates/digest-{kind}.md').read_text()
    # The shared protocol remains active; domain guidance includes its final rule.
    raw = tmp_path / 'raw' / kind.title() / 'file.pdf'
    cfg = SimpleNamespace(wiki_dir=tmp_path/'wiki', wiki_root=tmp_path,
                          raw_root=tmp_path/'raw', output_language='auto')
    assert template in _stage_2_2_build_template_section(template, raw)
    prompt = _stage_2_4_build_all_prompt([], raw, cfg, template=template)
    assert template in prompt
    assert '---FILE:' in prompt and 'source page is mandatory' in prompt.lower()


def test_guidance_is_never_cut_in_middle(tmp_path):
    from _stage_2_analyze import _stage_2_2_build_template_section
    from _stage_2_4_generation import _stage_2_4_build_all_prompt
    template = '# Guidance\n' + ('Complete evidence condition.\n' * 160) + 'FINAL CONDITION'
    raw = tmp_path / 'raw/Book/file.pdf'
    cfg = SimpleNamespace(wiki_dir=tmp_path/'wiki', wiki_root=tmp_path,
                          raw_root=tmp_path/'raw', output_language='auto')
    assert template in _stage_2_2_build_template_section(template, raw)
    assert template in _stage_2_4_build_all_prompt([], raw, cfg, template=template)
