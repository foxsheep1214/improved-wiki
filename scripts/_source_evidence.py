"""Read-only retrieval of parser observations and their exact PDF locations.

This is separate from the completed-ingest claim ledger. A matching source hash
means current bytes, not a completed ingest or verified OCR. No LLM/API calls.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

from _paths import atomic_write, detect_runtime_dir
from _scan_evidence import source_hash, region_text, crop_region
from _source_identity import normalize_source_ref, SourceResolver, AmbiguousSourceError, raw_source_refs
from _wiki_keyword import tokenize_query, build_snippet


def _read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _inside(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f'Artifact escapes root: {value}')
    return path


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _search_text(text: str) -> str:
    # OCR LaTeX often writes 6 . 6 1 or \bar {y}; normalize only for matching,
    # never change the stored/cited transcription. Do not join across lines.
    text = re.sub(r'(?<=[0-9.])[ \t]+(?=[0-9.])', '', text)
    return re.sub(r'(\\[A-Za-z]+)[ \t]+(?=[{])', r'\1', text).lower()


def valid_bbox(box) -> bool:
    return (isinstance(box, (list, tuple)) and len(box) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                    and math.isfinite(v) for v in box)
            and 0 <= box[0] < box[2] <= 1000 and 0 <= box[1] < box[3] <= 1000)


def block_records(manifest: dict, blocks: list[dict]):
    """Also project text from old v1 content lists without rewriting originals."""
    start, end = manifest['page_range_zero_based']
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        kind = block.get('type', 'unknown')
        if kind in ('header', 'footer', 'page_number'):
            continue
        rid = f'r{index:04d}'
        idx = block.get('page_idx')
        absolute = (start + idx if isinstance(idx, int) and not isinstance(idx, bool)
                    and 0 <= idx < end - start else None)
        labels = block.get('image_caption') or block.get('chart_caption') or block.get('table_caption') or []
        if isinstance(labels, str):
            labels = [labels]
        text = '\n'.join([region_text(block), *map(str, labels)]).strip()
        eid = 'ev-' + _digest([manifest['source'], manifest['source_sha256'],
                              manifest['response_sha256'], start, end, rid])
        yield {'evidence_id': eid, 'region_id': rid, 'kind': kind,
               'text': text, 'page_idx': absolute,
               'pdf_page': absolute + 1 if absolute is not None else None,
               'printed_page': None, 'reading_order': index,
               'bbox': block.get('bbox') if valid_bbox(block.get('bbox')) else None,
               'coordinate_system': 'normalized-0-1000',
               'verification': 'parser-observation-not-verified',
               'image_basename': Path(block.get('img_path') or '').name}


def bind_captions(config, raw_file: Path, media_dir: Path) -> int:
    """Persist source-scoped joins after caption, including validated cache hits.

    The original parser response is immutable. This small derived sidecar may
    be rebuilt; unlocated/legacy images are never assigned a guessed page.
    """
    from _caption_evidence import caption_evidence_matches
    from _stage_1_3_caption import _stage_1_3_is_caption_failed
    project = config.wiki_root.resolve()
    identity = raw_file.resolve().relative_to(project).as_posix()
    sha = source_hash(raw_file)
    count = 0
    for pointer in sorted((config.extract_tmp_dir / media_dir.name).glob('_chunk_*/_scan_evidence.json')):
        mp = _inside(config.runtime_dir, _read(pointer)['manifest'])
        if not mp.is_relative_to((config.runtime_dir / 'scan-evidence').resolve()):
            raise ValueError('Scan manifest outside scan-evidence')
        manifest = _read(mp)
        if manifest['source'] != identity or manifest['source_sha256'] != sha:
            raise ValueError('Caption join source identity/version mismatch')
        figures_path = pointer.with_name('_mineru_figures.json')
        if not figures_path.exists():
            continue  # No join metadata: retain any earlier durable binding.
        figures = _read(figures_path)
        blocks = _read(mp.with_name('content-list.json'))
        links = {}
        for record in block_records(manifest, blocks):
            if record['kind'] not in ('image', 'chart') or record['page_idx'] is None:
                continue
            matches = [f for f in figures if f.get('mineru_basename') == record['image_basename']
                       and f.get('page') == record['page_idx'] and f.get('page_mapping_verified') is True]
            if len(matches) != 1:
                continue
            f = matches[0]
            if Path(f['filename']).name != f['filename']:
                raise ValueError('Unsafe caption image filename')
            image = _inside(media_dir, f['filename'])
            cap = Path(str(image) + '.caption.txt')
            if not image.is_file() or not cap.is_file():
                continue
            text = cap.read_text().strip()
            if _stage_1_3_is_caption_failed(text) or not caption_evidence_matches(image, text):
                continue
            structured = Path(str(image) + '.caption.json')
            data = _read(structured) if structured.exists() else {}
            links[record['region_id']] = {
                'image': image.relative_to(project).as_posix(),
                'caption': cap.relative_to(project).as_posix(),
                'image_sha256': source_hash(image), 'caption_sha256': source_hash(cap),
                'structured_sha256': source_hash(structured) if structured.exists() else None,
                'model': data.get('model'), 'language': data.get('language'),
                'verification': data.get('verification', 'legacy-caption-not-verified')}
            count += 1
        atomic_write(mp.with_name('caption-links.json'), json.dumps(
            {'schema_version': 1, 'source': identity, 'source_sha256': sha,
             'regions': links}, ensure_ascii=False, indent=2))
    return count


def collect(project: Path, *, source: str = '', page: str = '', history: bool = False,
            evidence_id: str = '', query: str = '') -> dict:
    """Load compact artifacts, never base64-heavy raw parser responses.

    All source versions are inspectable with history; stale/missing sources are
    excluded by default. Raw observations do not inherit ledger completion.
    """
    project = project.resolve()
    runtime = detect_runtime_dir(project)
    manifests, diagnostics = [], []
    for path in sorted((runtime / 'scan-evidence').glob('*/*/*/manifest.json')):
        try:
            _inside(runtime, str(path))
            m = _read(path)
            identity = normalize_source_ref(m['source'], project)
            _inside(project, identity)
            if not re.fullmatch('[0-9a-f]{64}', m['source_sha256']):
                raise ValueError('Invalid source hash')
            manifests.append((path, m, identity))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            diagnostics.append({'artifact': str(path), 'error': str(exc)})
    wanted, ambiguous, missing = None, {}, []
    if page:
        from evidence_lookup import _page_sources
        # Explicit page paths cannot escape the wiki tree.
        _inside(project / 'wiki', page.removeprefix('wiki/'))
        refs = _page_sources(project, page)
        resolver = SourceResolver(project, raw_source_refs(project) | {s for _, _, s in manifests})
        wanted = set()
        for ref in refs:
            try:
                resolved = resolver.resolve(ref)
                if resolved: wanted.add(resolved)
                else: missing.append(ref)
            except AmbiguousSourceError as exc:
                ambiguous[ref] = str(exc)
    if '/' in source:
        source = normalize_source_ref(source, project)
    tokens = tokenize_query(query)
    phrase = _search_text(query.strip())
    hashes, records, unavailable = {}, [], []
    for mp, m, identity in manifests:
        if wanted is not None and identity not in wanted:
            continue
        if source and not (identity == source if '/' in source else source.lower() in identity.lower()):
            continue
        try:
            content_path = _inside(mp.parent, 'content-list.json')
            if m.get('content_list_sha256') and source_hash(content_path) != m['content_list_sha256']:
                raise ValueError('Parser content list changed since evidence capture')
            blocks = _read(content_path)
            links = _read(_inside(mp.parent, 'caption-links.json')) if mp.with_name('caption-links.json').exists() else {}
            if links and (links.get('source') != m['source'] or links.get('source_sha256') != m['source_sha256']):
                raise ValueError('Caption link provenance mismatch')
            projected = list(block_records(m, blocks))
            # Filter compact text before hashing source PDFs. Search cost should
            # not include reading every book's bytes when only one has a hit.
            if query:
                texts = [r['text'] for r in projected]
                for link in links.get('regions', {}).values():
                    try:
                        texts.append(_inside(project, link['caption']).read_text())
                    except (OSError, ValueError, KeyError) as exc:
                        diagnostics.append({'artifact': str(mp), 'error': str(exc)})
                haystack = _search_text('\n'.join(texts))
                if not (phrase and phrase in haystack or any(t in haystack for t in tokens)):
                    continue
            if evidence_id:
                ids = {r['evidence_id'] for r in projected}
                ids.update('ev-' + _digest([r['evidence_id'], links['regions'][r['region_id']]])
                           for r in projected if r['region_id'] in links.get('regions', {}))
                if evidence_id not in ids: continue
            src = _inside(project, identity)
            if identity not in hashes:
                hashes[identity] = source_hash(src) if src.is_file() else None
            status = ('missing_source' if hashes[identity] is None else
                      'source_current' if hashes[identity] == m['source_sha256'] else 'stale')
            if status != 'source_current':
                unavailable.append({'source': identity, 'source_hash': m['source_sha256'], 'evidence_status': status})
                if not history: continue
            reviews = _read(_inside(mp.parent, 'region-review.json')) if mp.with_name('region-review.json').exists() else {}
            for r in projected:
                r.update(result_type='source_evidence', source=identity, source_hash=m['source_sha256'],
                         source_path=str(src), evidence_status=status,
                         ingestion_status='not_asserted', manifest=str(mp.relative_to(project)),
                         parser_response_sha256=m['response_sha256'])
                review = reviews.get('regions', {}).get(r['region_id'])
                if review:
                    r['review'] = {k: review[k] for k in ('status','secondary','reasons','error') if k in review}
                r['can_render'] = status == 'source_current' and r['page_idx'] is not None
                if not evidence_id or r['evidence_id'] == evidence_id:
                    records.append(r)
                link = links.get('regions', {}).get(r['region_id'])
                if not link: continue
                try:
                    image, caption = _inside(project, link['image']), _inside(project, link['caption'])
                    if source_hash(image) != link['image_sha256'] or source_hash(caption) != link['caption_sha256']:
                        raise ValueError('Caption/image bytes changed since evidence binding')
                    structured = Path(str(image) + '.caption.json')
                    if link.get('structured_sha256') and source_hash(structured) != link['structured_sha256']:
                        raise ValueError('Structured caption changed since evidence binding')
                    cr = dict(r, kind='caption', text=caption.read_text(),
                              evidence_id='ev-' + _digest([r['evidence_id'], link]),
                              parent_evidence_id=r['evidence_id'], image_path=str(image),
                              model=link.get('model'), verification=link['verification'])
                    if not evidence_id or cr['evidence_id'] == evidence_id:
                        records.append(cr)
                except (OSError, ValueError, KeyError) as exc:
                    diagnostics.append({'artifact': str(mp), 'region_id': r['region_id'], 'error': str(exc)})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            diagnostics.append({'artifact': str(mp), 'error': str(exc)})
    found = {r['source'] for r in records}
    if wanted: missing.extend(sorted(wanted - found))
    return {'matches': records, 'sources': sorted(found), 'missing_sources': missing,
            'ambiguous_sources': ambiguous, 'unavailable_sources': unavailable,
            'diagnostics': diagnostics}


def search(project: Path, query: str, top: int = 20, *, source: str = '') -> dict:
    result = collect(project, source=source, query=query)
    tokens = tokenize_query(query)
    phrase = _search_text(query.strip())
    matches = []
    for r in result['matches']:
        lower = _search_text(r['text'])
        hits = [t for t in tokens if t in lower]
        exact = bool(phrase) and phrase in lower
        if not exact and not hits: continue
        score = 5 * int(exact) + len(hits) / max(1, len(tokens))
        matches.append(dict(r, score=score, path=r['manifest'],
                            title=f"{Path(r['source']).name} · PDF {r['pdf_page'] or '?'} · {r['kind']}",
                            snippet=build_snippet(r['text'], phrase if exact else hits[0]), vector_score=None))
    matches.sort(key=lambda r: (-r['score'], r['source'], r['pdf_page'] or 0, r['reading_order'], r['evidence_id']))
    result['matches'] = matches[:max(0, top)]
    return result


def render(project: Path, evidence_id: str, output_dir: Path) -> dict:
    """Render on demand from verified source bytes; never invoke an OCR/VLM."""
    result = collect(project, evidence_id=evidence_id)
    found = result['matches']
    if len(found) != 1:
        raise ValueError('Evidence not uniquely available for the current source version')
    r = found[0]
    if not r['can_render']:
        raise ValueError('Missing parser page mapping; refusing to guess the PDF page')
    import fitz
    source = Path(r['source_path'])
    if source_hash(source) != r['source_hash']:
        raise ValueError('Source changed before rendering')
    output_dir.mkdir(parents=True, exist_ok=True)
    page_path = output_dir / (evidence_id + '-page.png')
    with fitz.open(source) as doc:
        if not 0 <= r['page_idx'] < len(doc): raise ValueError('Page outside PDF')
        doc[r['page_idx']].get_pixmap(dpi=150).save(page_path)
    rendered = {'page_image': str(page_path.resolve())}
    if r['bbox'] is not None:
        crop = output_dir / (evidence_id + '-region.png')
        crop_region(source, r['page_idx'], r['bbox'], crop)
        rendered['region_image'] = str(crop.resolve())
    if source_hash(source) != r['source_hash']:
        for path in rendered.values(): Path(path).unlink(missing_ok=True)
        raise ValueError('Source changed during rendering; output discarded')
    return dict(r, **rendered)
