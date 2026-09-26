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
