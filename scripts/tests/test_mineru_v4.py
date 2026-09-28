import pytest

import _mineru_v4 as v4
import _stage_1_1_scanned as scanned


@pytest.mark.parametrize('terminal,transient', [('completed', False), ('completed', True), ('partial', False), ('failed', False), ('canceled', False)])
def test_v1_upload_poll_and_only_accept_complete_file(tmp_path, monkeypatch, terminal, transient):
    pdf = tmp_path/'chunk.pdf';pdf.write_bytes(b'pdf')
    requests = []
    polls = 0
    def call(base, route, **kw):
        nonlocal polls
        requests.append((route, kw))
        if route == '/v1/uploads':
            assert kw['payload']['bytes'] == 3
            return {'id': 'u1', 'status': 'pending', 'upload_url': '/v1/uploads/u1/content'}
        if route.endswith('/complete'):
            return {'status': 'completed', 'file': {'id': 'f1'}}
        if route == '/v1/parse/jobs':
            assert kw['payload']['files'][0]['page_range'] == 'all'
            assert kw['payload']['tier'] == 'standard'
            return {'job_id': 'j1', 'status': 'queued'}
        if kw.get('method') == 'DELETE':
            return {'status': 'canceled'}
        polls += 1
        if transient and polls == 1:
            raise TimeoutError('cold model initialization')
        return {'job_id': 'j1', 'status': terminal, 'files': [
            {'status': 'completed', 'output_files': {'zip': {'file_id': 'z1', 'bytes': 3}}}]}
    monkeypatch.setattr(v4, '_json', call)
    monkeypatch.setattr(v4, '_request', lambda *a, **kw: b'zip')
    monkeypatch.setattr(v4.time, 'sleep', lambda *a: None)
    monkeypatch.setattr(v4, 'decode_archive', lambda data, name: {name: {'md_content': 'body'}})
    if terminal == 'completed':
        result = v4.parse(19999, pdf, 'chunk.pdf')
        assert result['chunk.pdf']['api_job']['job_id'] == 'j1'
        assert not any(k.get('method') == 'DELETE' for _, k in requests)
    else:
        with pytest.raises(RuntimeError):
            v4.parse(19999, pdf, 'chunk.pdf')
        assert requests[-1][1]['method'] == 'DELETE'


def test_timeout_cancels_own_job_before_retry(tmp_path, monkeypatch):
    pdf = tmp_path/'chunk.pdf';pdf.write_bytes(b'pdf')
    calls = []
    def request(base, route, **kw):
        calls.append((route, kw))
        if route == '/v1/uploads':
            return {'status': 'completed', 'file': {'id': 'f1'}}
        return {'job_id': 'j1', 'status': 'queued'}
    monkeypatch.setattr(v4, '_json', request)
    with pytest.raises(TimeoutError):
        v4.parse(19999, pdf, 'chunk.pdf', timeout=0)
    assert calls[-1] == ('/v1/parse/jobs/j1', {'method': 'DELETE', 'timeout': 5})


def test_local_upload_cannot_send_document_to_external_origin():
    with pytest.raises(ValueError, match='different-origin'):
        v4._request('http://127.0.0.1:19999', 'https://example.com/upload', data=b'pdf', method='PUT')


@pytest.mark.parametrize('version,module', [('4.0.7', 'mineru.parser.api_server'), ('3.4.5', 'mineru.cli.fast_api')])
def test_server_command_follows_installed_major_version(monkeypatch, version, module):
    monkeypatch.setattr(scanned.subprocess, 'check_output', lambda *a, **kw: version)
    command = scanned._mineru_server_command('/venv/python')
    assert command[2] == module
    if version.startswith('4'):
        assert '--disable-image-analysis' in command
        assert command[command.index('--tier')+1] == 'standard'


def test_cleanup_targets_only_owned_process_group(monkeypatch):
    import signal
    from types import SimpleNamespace
    calls = []
    proc = SimpleNamespace(pid=123, wait=lambda **kw: calls.append('wait'),
                           terminate=lambda: calls.append('terminate'))
    monkeypatch.setattr(scanned.os, 'getpgid', lambda pid: 123)
    monkeypatch.setattr(scanned.os, 'killpg', lambda pid, sig: calls.append((pid, sig)))
    scanned._stop_owned_mineru_server(None)
    assert not calls  # a reused external service has no owned process
    scanned._stop_owned_mineru_server(proc)
    assert calls == [(123, signal.SIGTERM), 'wait']
    calls.clear()
    monkeypatch.setattr(scanned.os, 'getpgid', lambda pid: 999)
    scanned._stop_owned_mineru_server(proc)
    assert calls == ['terminate', 'wait']  # never signal another owner's group


@pytest.mark.parametrize("indices", [[0, 1, 2], [0, 2], [0, 1, 1]])
def test_page_coverage_rejects_missing_and_repeated_pages(tmp_path, monkeypatch, indices):
    pdf = tmp_path / "chunk.pdf"
    pdf.write_bytes(b"pdf")
    canceled = []
    def request(base, route, **kw):
        if kw.get("method") == "DELETE":
            canceled.append(route)
            return {}
        if route == "/v1/uploads":
            return {"status": "completed", "file": {"id": "f1"}}
        return {"job_id": "j1", "status": "completed", "files": [
            {"status": "completed", "output_files": {"zip": {"file_id": "z1", "bytes": 3}}}]}
    monkeypatch.setattr(v4, "_json", request)
    monkeypatch.setattr(v4, "_request", lambda *a, **kw: b"zip")
    monkeypatch.setattr(v4, "decode_archive", lambda data, name: {
        name: {"middle_json": {"pages": [{"page_idx": i} for i in indices]}}})
    if indices == [0, 1, 2]:
        assert v4.parse(19999, pdf, "chunk.pdf", expected_pages=3)
        assert not canceled
    else:
        with pytest.raises(ValueError, match="source page indices"):
            v4.parse(19999, pdf, "chunk.pdf", expected_pages=3)
        assert canceled == ["/v1/parse/jobs/j1"]


def test_decoder_uses_selected_python_without_importing_mineru(tmp_path, monkeypatch):
    import base64
    import io
    import json
    import sys
    import zipfile
    # Only the child sees this renderer; the driver has no MinerU dependency.
    package = tmp_path / 'mineru'
    (package / 'parser').mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (package / 'parser' / '__init__.py').write_text('')
    (package / 'parser' / 'base.py').write_text(
        'from types import SimpleNamespace\n'
        'class ParseResult:\n'
        '    @staticmethod\n'
        '    def from_dict(value): return SimpleNamespace(middle_json=value)\n')
    (package / 'render.py').write_text(
        'def render_content_list(value): return value["blocks"]\n')
    monkeypatch.setenv('PYTHONPATH', str(tmp_path))
    monkeypatch.setenv('IMPROVED_WIKI_MINERU_PYTHON', sys.executable)
    monkeypatch.setitem(sys.modules, 'mineru', None)
    middle = {'blocks': [{'type': 'text', 'text': '跨环境解析', 'page_idx': 0}]}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('middle_json.json', json.dumps(middle))
        archive.writestr('markdown.md', '跨环境解析')
        archive.writestr('images/figure.png', b'image-bytes')
        archive.writestr('structured_content.json', '{"native": true}')
    result = v4.decode_archive(buffer.getvalue(), 'chunk.pdf')['chunk.pdf']
    assert result['content_list'] == middle['blocks']
    assert result['middle_json'] == middle
    assert result['md_content'] == '跨环境解析'
    assert result['images']['figure.png'] == base64.b64encode(b'image-bytes').decode()
    assert result['structured_content'] == {'native': True}


def test_explicit_missing_python_is_not_silently_replaced(tmp_path, monkeypatch):
    missing = tmp_path / 'missing-python'
    monkeypatch.setenv('IMPROVED_WIKI_MINERU_PYTHON', str(missing))
    assert v4.mineru_python() == missing
    with pytest.raises(RuntimeError, match='IMPROVED_WIKI_MINERU_PYTHON'):
        v4._render_content_list({})


def test_default_renderer_python_matches_server_environment(tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    monkeypatch.delenv('IMPROVED_WIKI_MINERU_PYTHON', raising=False)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    assert v4.mineru_python() == Path(sys.executable)
    python = tmp_path / '.venv' / 'bin' / 'python3'
    python.parent.mkdir(parents=True)
    python.touch()
    assert v4.mineru_python() == python


def test_renderer_failure_reports_selected_environment(monkeypatch):
    import subprocess
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr='No module named mineru')
    monkeypatch.setattr(v4.subprocess, 'run', fail)
    monkeypatch.setenv('IMPROVED_WIKI_MINERU_PYTHON', '/selected/python')
    with pytest.raises(RuntimeError, match='/selected/python: No module named mineru'):
        v4._render_content_list({})
