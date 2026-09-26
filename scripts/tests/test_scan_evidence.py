import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import _scan_evidence as scan
import _stage_1_1_scanned as scanned
import _stage_1_3_caption as cap
from _caption_evidence import (
    FIELDS, parse_caption, save_caption_evidence, caption_evidence_matches,
)


def config(root):
    return SimpleNamespace(wiki_root=root, runtime_dir=root/'runtime',
                           caption_api_key='', caption_base_url='http://localhost:11434',
                           caption_model='vlm', caption_protocol='openai',
                           caption_timeout_seconds=30, caption_fallback_base_url='',
                           caption_fallback_model='')


def artifact(tmp_path, monkeypatch, blocks=None):
    source = tmp_path/'raw'/'book.pdf'
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(b'fake pdf version one')
    monkeypatch.setattr(scan, 'crop_region', lambda pdf, page, bbox, out: out.write_bytes(b'crop'))
    if blocks is None:
        blocks = [{'type': 'equation', 'text': r'x_{[?]', 'page_idx': 0,
                   'bbox': [10, 20, 400, 100]}]
    chunk = tmp_path/'extract'/'_chunk_0005-0006'
    chunk.mkdir(parents=True, exist_ok=True)
    response = {'chunk': {'md_content': 'source text', 'content_list': json.dumps(blocks),
                          'images': {}, 'middle_json': {'untouched': True}}}
    path = scan.persist_scan_evidence(response, source, config(tmp_path), chunk, 5, 6)
    return path, source, chunk, response


def test_preserve_versioned_raw_response_and_true_page_mapping(tmp_path, monkeypatch):
    path, source, chunk, response = artifact(tmp_path, monkeypatch)
    record = json.loads(path.read_text())
    assert record['source'] == 'raw/book.pdf'
    assert record['regions'][0]['pdf_page'] == 6
    assert record['regions'][0]['page_idx'] == 5
    assert json.loads((path.parent/'mineru-response.json').read_text()) == response
    assert (chunk/'_mineru_content_list.json').exists()  # no figures still persists
    assert (path.parent/'r0000.png').read_bytes() == b'crop'
    before = path.read_bytes()
    again = scan.persist_scan_evidence(response, source, config(tmp_path), chunk, 5, 6)
    assert again == path and again.read_bytes() == before
    source.write_bytes(b'fake pdf version two')
    newer = scan.persist_scan_evidence(response, source, config(tmp_path), chunk, 5, 6)
    assert newer != path and path.exists()


def test_missing_page_is_not_assigned_to_chunk_start(tmp_path, monkeypatch):
    path, *_ = artifact(tmp_path, monkeypatch, [{'type': 'table', 'table_body': '<table/>',
                                               'bbox': [0, 0, 100, 100]}])
    region = json.loads(path.read_text())['regions'][0]
    assert region['pdf_page'] is None and region['crop'] is None
    assert 'no page guess' in region['crop_error']


def test_html_merged_cells_are_retained_in_evidence_and_digest(tmp_path, monkeypatch):
    html = '<table><tr><td colspan="2">Header</td></tr><tr><td>$x_1$</td><td>2</td></tr></table>'
    path, *_ = artifact(tmp_path, monkeypatch, [{'type': 'table', 'table_body': html,
                                              'page_idx': 0, 'bbox': [0, 0, 900, 900]}])
    assert (path.parent/'r0000.html').read_text() == html
    assert scanned._convert_html_tables_to_markdown(html) == html
    assert scan.anomaly_reasons({'type': 'table', 'table_body': html}) == []


@pytest.mark.parametrize('block,reason', [
    ({'type': 'equation', 'text': ''}, 'empty-transcription'),
    ({'type': 'equation', 'text': r'\frac{x}{[?]}'}, 'unreadable-marker'),
    ({'type': 'equation', 'text': r'x_{i'}, 'unbalanced-latex-braces'),
    ({'type': 'equation', 'text': r'}x{'}, 'unbalanced-latex-braces'),
    ({'type': 'table', 'table_body': 'lost table'}, 'missing-table-structure'),
    ({'type': 'table', 'table_body': '<table><tr><td>a</td></tr><tr><td>b</td><td>c</td></tr></table>'}, 'inconsistent-table-columns'),
])
def test_only_concrete_anomalies_trigger_review(block, reason):
    assert reason in scan.anomaly_reasons(block)
    assert scan.anomaly_reasons({'type': 'equation', 'text': r'\bar{y}=\{x_i\}'}) == []


def test_independent_transcription_never_overwrites_original(tmp_path, monkeypatch):
    path, source, _, _ = artifact(tmp_path, monkeypatch)
    original = path.read_bytes()
    monkeypatch.setattr(cap, 'CAPTION_BATCH_LOCK_FILE', tmp_path/'caption.lock')
    calls = []
    def request(img, provider, folder, contexts):
        calls.append(img)
        assert 'x_{[?]' not in img['_review_prompt']  # independent reading
        return r'x_i', None
    monkeypatch.setattr(cap, '_stage_1_3_caption_one_image', request)
    result = scan.review_regions(path, config(tmp_path))
    item = result['regions']['r0000']
    assert item['status'] == 'needs-review'
    assert item['original'] == r'x_{[?]' and item['secondary'] == r'x_i'
    assert path.read_bytes() == original
    scan.review_regions(path, config(tmp_path))
    assert len(calls) == 1  # restart reuses the completed second opinion


def test_clean_regions_do_not_call_vlm_but_manual_selection_can(tmp_path, monkeypatch):
    path, *_ = artifact(tmp_path, monkeypatch, [{'type': 'equation', 'text': r'\bar{y}',
                                              'page_idx': 0, 'bbox': [0, 0, 900, 900]}])
    monkeypatch.setattr(cap, 'CAPTION_BATCH_LOCK_FILE', tmp_path/'caption.lock')
    calls = []
    monkeypatch.setattr(cap, '_stage_1_3_caption_one_image',
                        lambda *a: (calls.append(a) or r'\vec{y}', None))
    assert not scan.review_regions(path, config(tmp_path))['regions']
    assert not calls
    result = scan.review_regions(path, config(tmp_path), selected={'r0000'})
    assert len(calls) == 1 and result['regions']['r0000']['status'] == 'needs-review'


def test_review_error_and_limit_stay_explicit(tmp_path, monkeypatch):
    blocks = [{'type': 'equation', 'text': '[?]', 'page_idx': 0,
               'bbox': [0, 0, 900, 900]} for _ in range(2)]
    path, *_ = artifact(tmp_path, monkeypatch, blocks)
    monkeypatch.setattr(cap, 'CAPTION_BATCH_LOCK_FILE', tmp_path/'caption.lock')
    monkeypatch.setattr(cap, '_stage_1_3_caption_one_image', lambda *a: (None, 'offline'))
    result = scan.review_regions(path, config(tmp_path), limit=1)['regions']
    assert result['r0000']['status'] == 'unavailable'
    assert result['r0001']['status'] == 'deferred'


def test_changed_source_is_not_used_for_manual_crop(tmp_path, monkeypatch):
    path, source, *_ = artifact(tmp_path, monkeypatch, [{'type': 'equation', 'text': 'x',
                                                      'page_idx': 0, 'bbox': [0, 0, 900, 900]}])
    source.write_bytes(b'changed source')
    monkeypatch.setattr(cap, '_stage_1_3_caption_one_image', lambda *a: pytest.fail('must not call'))
    item = scan.review_regions(path, config(tmp_path), selected={'r0000'})['regions']['r0000']
    assert item['status'] == 'unavailable' and 'source changed' in item['error']


def observations():
    return {'summary': 'A plot of measured signal power with two curves.',
            **{field: [] for field in FIELDS}, 'axes': ['radius: -30, -20, -10, 0'],
            'uncertainties': ['The unit is not clear.']}


def test_structured_caption_keeps_evidence_and_rejects_stale_pair(tmp_path):
    image = tmp_path/'figure.png'
    image.write_bytes(b'image')
    data = parse_caption(json.dumps(observations()))
    text = save_caption_evidence(image, data, {'model': 'qwen'}, 'English', {})
    assert 'radius: -30' in text and 'not clear' in text
    assert not cap._stage_1_3_is_caption_failed(text)
    assert caption_evidence_matches(image, text)
    assert not caption_evidence_matches(image, 'different caption')
    image.write_bytes(b'changed image')
    assert not caption_evidence_matches(image, text)
    record = json.loads(Path(str(image)+'.caption.json').read_text())
    assert record['verification'] == 'model-observation-not-verified'


def test_short_chinese_summary_with_detailed_evidence_is_valid():
    assert not cap._stage_1_3_is_caption_failed('极坐标图。\n\naxes:\n- 半径刻度为 -30、-20、-10、0。')
    assert cap._stage_1_3_is_caption_failed('抱歉，无法描述。\n\naxes:\n- The axis cannot be read.')


@pytest.mark.parametrize('page,verified', [(None, False), (2, True)])
def test_image_page_fallback_is_not_claimed_as_verified(tmp_path, page, verified):
    import base64
    from _stage_1_2_images import _stage_1_2_harvest_images
    cfg = config(tmp_path)
    cfg.raw_root = tmp_path/'raw'
    cfg.wiki_dir = tmp_path/'wiki'
    cfg.extract_tmp_dir = tmp_path/'extract'
    raw = cfg.raw_root/'book.pdf'
    raw.parent.mkdir();raw.write_bytes(b'pdf')
    chunk = cfg.extract_tmp_dir/'chunk';chunk.mkdir(parents=True)
    block = {'type': 'image', 'img_path': 'images/a.png'}
    if page is not None:
        block['page_idx'] = page
    result = {'chunk': {'images': {'a.png': 'data:image/png;base64,' + base64.b64encode(b'image').decode()},
                        'content_list': [block]}}
    saved = _stage_1_2_harvest_images(result, 0, raw, cfg, chunk)
    assert saved[0]['page_mapping_verified'] is verified


@pytest.mark.parametrize('bad', ['{}', '{"summary": "test"}', 'plain unsupported output'])
def test_caption_schema_rejects_missing_detail_fields(bad):
    with pytest.raises((ValueError, TypeError)):
        parse_caption(bad)


def test_caption_transport_retries_truncation_and_saves_complete_schema(tmp_path, monkeypatch):
    from PIL import Image
    import io
    import urllib.request
    image = tmp_path/'figure.png'
    Image.new('RGB', (50, 50), 'white').save(image)
    sent = []
    def post(request, timeout):
        body = json.loads(request.data)
        sent.append(body)
        return io.BytesIO(json.dumps({'choices': [{'finish_reason': 'length' if len(sent) == 1 else 'stop',
                'message': {'content': json.dumps(observations())}}]}).encode())
    monkeypatch.setattr(urllib.request, 'urlopen', post)
    monkeypatch.setattr(cap.time, 'sleep', lambda *a: None)
    provider = {'base_url': 'http://localhost', 'model': 'qwen', 'protocol': 'openai', 'timeout': 5, 'api_key': ''}
    text, error = cap._stage_1_3_caption_one_image(
        {'filename': image.name, '_structured': True, '_caption_language': 'Chinese'},
        provider, tmp_path, {})
    assert error is None and len(sent) == 2
    assert 'Chinese' in sent[0]['messages'][1]['content'][0]['text']
    assert 'radius' in text
    assert Path(str(image)+'.caption.json').exists()


def test_persistent_evidence_is_created_by_extract_entrypoint(tmp_path, monkeypatch):
    source = tmp_path/'book.pdf';source.write_bytes(b'pdf')
    monkeypatch.setattr(scanned, '_stage_1_2_harvest_images', lambda *a: [])
    results = {'chunk': {'md_content': 'Formula $x_i$ without a figure',
                         'content_list': [{'type': 'equation', 'text': 'x_i', 'page_idx': 0}]}}
    text, md_path = scanned._stage_1_1_scanned_extract_md(
        results, Path('chunk.pdf'), tmp_path/'extract', 0, 1, source, config(tmp_path))
    assert md_path.read_text() == text
    pointer = json.loads((md_path.parent/'_scan_evidence.json').read_text())
    assert (config(tmp_path).runtime_dir/pointer['manifest']).exists()
