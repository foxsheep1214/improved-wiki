"""Local MinerU V1 transport and lossless adaptation to the wiki extract contract."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


def _request(base, route, *, method='GET', payload=None, data=None, headers=None, timeout=30):
    url = urllib.parse.urljoin(base + '/', route)
    if urllib.parse.urlsplit(url)[:2] != urllib.parse.urlsplit(base)[:2]:
        raise ValueError('MinerU local API returned a different-origin URL')
    headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    # Local document bytes must never be redirected to an external service.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise ValueError('Unexpected redirect from local MinerU API')
    with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req, timeout=timeout) as response:
        return response.read()


def _json(base, route, **kwargs):
    value = json.loads(_request(base, route, **kwargs))
    if not isinstance(value, dict):
        raise ValueError('Expected a MinerU API object')
    return value


def health(port):
    try:
        data = _json(f'http://127.0.0.1:{port}', '/v1/health', timeout=3)
        return data if data.get('status') == 'ok' and str(data.get('version', '')).startswith('4.') else None
    except Exception:
        return None


def decode_archive(data: bytes, filename: str) -> dict:
    from mineru.parser.base import ParseResult
    from mineru.render import render_content_list
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate archive members')
        for name in names:
            if PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:
                raise ValueError('Unsafe archive member')
        middle = json.loads(archive.read('middle_json.json'))
        parsed = ParseResult.from_dict(middle)
        blocks = render_content_list(parsed.middle_json)
        images = {}
        for name in names:
            if PurePosixPath(name).suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp'):
                basename = PurePosixPath(name).name
                if basename in images:
                    raise ValueError('Ambiguous image basename')
                images[basename] = base64.b64encode(archive.read(name)).decode()
        value = {'md_content': archive.read('markdown.md').decode('utf-8'),
                 'content_list': blocks, 'images': images, 'middle_json': middle,
                 'api_version': 4, 'archive_sha256': hashlib.sha256(data).hexdigest()}
        for name, key in [('model_output.json', 'model_output'),
                          ('structured_content.json', 'structured_content')]:
            if name in names:
                value[key] = json.loads(archive.read(name))
        return {filename: value}


def parse(port: int, pdf: Path, filename: str, *, timeout: float = 1200,
          expected_pages: int | None = None, tier: str = 'standard') -> dict:
    """Upload, parse all chunk pages at Standard quality, retain native outputs.

    A partial/failed/canceled result is never accepted as a completed chunk.
    On timeout/error, cancel our job before the outer caller retries.
    """
    base = f'http://127.0.0.1:{port}'
    if tier not in ('flash', 'standard'):
        raise ValueError('Unsupported wiki parsing tier')
    raw = pdf.read_bytes()
    upload = _json(base, '/v1/uploads', method='POST', payload={
        'filename': filename, 'bytes': len(raw), 'mime_type': 'application/pdf',
        'purpose': 'parse', 'sha256sum': hashlib.sha256(raw).hexdigest()})
    if upload.get('status') != 'completed':
        _request(base, upload['upload_url'], method='PUT', data=raw,
                 headers=upload.get('upload_headers', {}), timeout=120)
        upload = _json(base, f"/v1/uploads/{upload['id']}/complete", method='POST')
    file_id = upload['file']['id']
    job = _json(base, '/v1/parse/jobs', method='POST', payload={
        'files': [{'source': {'type': 'file_id', 'file_id': file_id}, 'page_range': 'all'}],
        'tier': tier, 'ocr_mode': 'auto', 'output_formats': ['zip']})
    job_id = job['job_id']
    deadline = time.monotonic() + timeout
    try:
        while job.get('status') in ('queued', 'running', 'pending', 'processing'):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f'MinerU V1 job {job_id} exceeded {timeout}s')
            time.sleep(min(2, remaining))
            try:
                job = _json(base, f'/v1/parse/jobs/{job_id}', timeout=min(30, remaining))
            except TimeoutError:
                # Cold model initialization can briefly block API polling.
                # Keep the same job and enforce the overall deadline above.
                continue
        if job.get('status') != 'completed':
            errors = [f.get('error') for f in job.get('files', []) if f.get('error')]
            detail = json.dumps(errors or job.get('error', ''), ensure_ascii=False)
            raise RuntimeError(f"MinerU V1 job {job_id}: {job.get('status')}; {detail}")
        files = job.get('files', [])
        if len(files) != 1 or files[0].get('status') != 'completed':
            raise RuntimeError('MinerU V1 did not return exactly one completed file')
        ref = files[0]['output_files']['zip']
        data = _request(base, f"/v1/files/{ref['file_id']}/content", timeout=120)
        if len(data) != ref['bytes']:
            raise ValueError('Incomplete MinerU ZIP download')
        results = decode_archive(data, filename)
        if expected_pages is not None:
            pages = results[filename]['middle_json']['pages']
            indices = [p.get('page_idx') for p in pages]
            if indices != list(range(expected_pages)):
                raise ValueError('MinerU V1 returned incomplete or unexpected source page indices')
        results[filename]['api_job'] = job
        return results
    except BaseException:
        try:
            _json(base, f'/v1/parse/jobs/{job_id}', method='DELETE', timeout=5)
        except Exception:
            pass
        raise
