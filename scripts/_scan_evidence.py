"""Durable, source-versioned MinerU evidence and non-destructive region review.

OCR and second opinions are observations, never automatic corrections. Artifacts
live outside extract-tmp so cleaning working caches cannot erase the originals.
"""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

from _paths import atomic_write


def _json(path: Path, value) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2))


@lru_cache(maxsize=32)
def _file_hash(path: str, size: int, mtime_ns: int) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def source_hash(path: Path) -> str:
    stat = path.stat()
    return _file_hash(str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def content_blocks(results: dict) -> list[dict]:
    blocks = []
    for value in results.values():
        if not isinstance(value, dict):
            continue
        items = value.get('content_list', [])
        if isinstance(items, str):
            items = json.loads(items)
        if not isinstance(items, list):
            raise ValueError('MinerU content_list must be an array')
        blocks.extend(item for item in items if isinstance(item, dict))
    return blocks


def region_text(block: dict) -> str:
    return str(block.get('table_body') or block.get('text') or '')


def anomaly_reasons(block: dict) -> list[str]:
    """Only observable defects, not guessed mathematical correctness."""
    if block.get('type') not in ('equation', 'table'):
        return []
    text = region_text(block)
    reasons = []
    if not text.strip():
        reasons.append('empty-transcription')
    if '\ufffd' in text or '[?]' in text or re.search(r'\[待.{0,8}\]', text):
        reasons.append('unreadable-marker')
    braces = re.sub(r'\\[{}]', '', text)
    balance = 0
    for char in braces:
        balance += (char == '{') - (char == '}')
        if balance < 0:
            break
    if balance != 0:
        reasons.append('unbalanced-latex-braces')
    if block.get('type') == 'table':
        if not re.search(r'<table\b', text, re.I):
            reasons.append('missing-table-structure')
        elif not re.search(r'\b(?:rowspan|colspan)\s*=', text, re.I):
            rows = re.findall(r'<tr\b[^>]*>(.*?)</tr>', text, re.I | re.S)
            widths = [len(re.findall(r'<t[dh]\b', row, re.I)) for row in rows]
            if widths and len(set(widths)) > 1:
                reasons.append('inconsistent-table-columns')
        if text.lower().count('<table') != text.lower().count('</table>'):
            reasons.append('unclosed-table')
    return reasons


def crop_region(pdf: Path, page_idx: int, bbox, destination: Path) -> None:
    import fitz
    if (not isinstance(bbox, (list, tuple)) or len(bbox) != 4
            or not all(isinstance(n, (int, float)) for n in bbox)
            or not (0 <= bbox[0] < bbox[2] <= 1000
                    and 0 <= bbox[1] < bbox[3] <= 1000)):
        raise ValueError('expected normalized MinerU bbox in [0, 1000]')
    with fitz.open(pdf) as doc:
        if not 0 <= page_idx < len(doc):
            raise ValueError('page_idx outside source PDF')
        page = doc[page_idx]
        r = page.rect
        clip = fitz.Rect(bbox[0]*r.width/1000, bbox[1]*r.height/1000,
                         bbox[2]*r.width/1000, bbox[3]*r.height/1000)
        page.get_pixmap(clip=clip, dpi=300).save(destination)


def persist_scan_evidence(results: dict, source: Path, config, chunk_out: Path,
                          start: int, end: int) -> Path:
    """Save exact OCR response, table HTML, locations and inspectable crops."""
    try:
        identity = source.resolve().relative_to(config.wiki_root.resolve()).as_posix()
    except ValueError:
        identity = str(source.resolve())
    version = source_hash(source)
    payload = json.dumps(results, ensure_ascii=False, sort_keys=True)
    response_hash = hashlib.sha256(payload.encode()).hexdigest()
    key = hashlib.sha256(identity.encode()).hexdigest()[:16]
    folder = (config.runtime_dir / 'scan-evidence' / key / version /
              f'{start:04d}-{end:04d}-{response_hash[:16]}')
    folder.mkdir(parents=True, exist_ok=True)
    manifest_path = folder / 'manifest.json'
    if not manifest_path.exists():
        atomic_write(folder / 'mineru-response.json', payload)
        blocks = content_blocks(results)
        _json(folder / 'content-list.json', blocks)
        regions = []
        for index, block in enumerate(blocks):
            kind = block.get('type')
            if kind not in ('equation', 'table', 'image', 'chart'):
                continue
            page_idx = block.get('page_idx')
            valid_page = (isinstance(page_idx, int) and not isinstance(page_idx, bool)
                          and 0 <= page_idx < end-start)
            absolute = start + page_idx if valid_page else None
            reasons = anomaly_reasons(block)
            region = {'id': f'r{index:04d}', 'type': kind,
                      'page_idx': absolute,
                      'pdf_page': absolute + 1 if valid_page else None,
                      'bbox': block.get('bbox'), 'original': region_text(block),
                      'reasons': reasons, 'crop': None}
            if kind == 'table':
                atomic_write(folder / f'{region["id"]}.html', region['original'])
            if kind == 'table' or reasons:
                try:
                    if not valid_page:
                        raise ValueError('missing or invalid page_idx; no page guess made')
                    crop = folder / f'{region["id"]}.png'
                    crop_region(source, absolute, region['bbox'], crop)
                    region['crop'] = crop.name
                except Exception as exc:
                    region['crop_error'] = str(exc)
            regions.append(region)
        _json(manifest_path, {'schema_version': 1, 'source': identity,
                             'source_sha256': version,
                             'response_sha256': response_hash,
                             'page_range_zero_based': [start, end],
                             'regions': regions})
    _json(chunk_out / '_scan_evidence.json', {
        'manifest': str(manifest_path.relative_to(config.runtime_dir))})
    # Keep the context even for formula/table-only pages with no harvested image.
    _json(chunk_out / '_mineru_content_list.json', content_blocks(results))
    return manifest_path


def review_regions(manifest_path: Path, config, *, selected: set[str] | None = None,
                   limit: int = 8) -> dict:
    """Transcribe suspicious crops independently; never overwrite OCR.

    selected enables an explicit second opinion for a visually discovered error
    (e.g. bar versus arrow) that deterministic detectors cannot identify.
    """
    from _stage_1_3_caption import (
        _caption_batch_slot, _stage_1_3_provider_bundles,
        _stage_1_3_caption_one_image,
    )
    record = json.loads(manifest_path.read_text())
    folder = manifest_path.parent
    path = folder / 'region-review.json'
    reviews = json.loads(path.read_text()) if path.exists() else {'schema_version': 1, 'regions': {}}
    requested = [r for r in record['regions'] if r['type'] in ('equation', 'table')
                 and (r['reasons'] or selected and r['id'] in selected)]
    if selected and selected - {r['id'] for r in requested}:
        raise ValueError('unknown or non-formula/table region selected')
    providers = _stage_1_3_provider_bundles(config)
    # The configured fallback is usually the local independent Qwen model.
    label, provider = providers[-1]
    calls = 0
    for region in requested:
        rid = region['id']
        previous = reviews['regions'].get(rid, {})
        if previous.get('status') == 'needs-review':
            continue
        result = {'status': 'deferred', 'original': region['original'],
                  'reasons': region['reasons'], 'pdf_page': region['pdf_page'],
                  'bbox': region['bbox'], 'provider': label, 'model': provider['model']}
        reviews['regions'][rid] = result
        if calls >= limit:
            result['error'] = 'per-run review limit reached; explicit retry required'
            continue
        crop = folder / (region['crop'] or f'{rid}.png')
        try:
            if not crop.exists():
                source = Path(record['source'])
                if not source.is_absolute():
                    source = config.wiki_root / source
                if source_hash(source) != record['source_sha256']:
                    raise ValueError('source changed; refusing to crop a different version')
                crop_region(source, region['page_idx'], region['bbox'], crop)
            result['crop'] = crop.name
            if not provider['base_url'] or not provider['model']:
                raise ValueError('no configured review provider')
            calls += 1
            prompt = ('Transcribe this cropped source region faithfully. Output only '
                      'LaTeX for a formula, or an HTML table with LaTeX in its cells. '
                      'Preserve bars versus arrows, conjugates, indices, signs, units '
                      'and equation numbers. Do not solve, simplify, correct the source, '
                      'or reconstruct unreadable content from domain knowledge. '
                      'Mark unreadable symbols with [?].')
            with _caption_batch_slot():
                secondary, error = _stage_1_3_caption_one_image(
                    {'filename': crop.name, 'path': str(crop),
                     '_review_prompt': prompt}, provider, folder, {})
            if not secondary:
                raise RuntimeError(error or 'empty review response')
            result.update(status='needs-review', secondary=secondary,
                          note='Second opinion only; agreement is not validation. Original retained.')
        except Exception as exc:
            result.update(status='unavailable', error=str(exc))
        _json(path, reviews)
    if requested:
        _json(path, reviews)
    return reviews


def review_source_regions(out_dir: Path, config) -> str:
    """Called after releasing the MinerU lock, before digest generation."""
    notes = []
    for pointer in sorted(out_dir.glob('_chunk_*/_scan_evidence.json')):
        reference = json.loads(pointer.read_text())['manifest']
        path = config.runtime_dir / reference
        result = review_regions(path, config)
        for rid, item in result['regions'].items():
            notes.append(f"PDF page {item.get('pdf_page')}, {rid}: {item['status']}; "
                         f"{', '.join(item['reasons']) or 'manual check'}; "
                         f"evidence: {reference}")
    if not notes:
        return ''
    print(f'[scan-review] {len(notes)} regions require verification; originals retained')
    return ('\n\n[OCR REVIEW REQUIRED — The following source regions are uncertain. '
            'Do not treat their formulas/table cells as verified facts or replace '
            'them with model guesses. Independent transcriptions and crops are '
            'stored beside the evidence manifests.]\n' + '\n'.join(notes))
