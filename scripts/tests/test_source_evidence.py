"""Source observations survive digestion and can be located without guessing."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys

import pytest

import _source_evidence as evidence
import _scan_evidence as scan
from _caption_evidence import save_caption_evidence, FIELDS


def fixture(root, name='a.pdf', blocks=None, start=1, end=2):
    import fitz
    src = root/'raw'/name
    src.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open() as doc:
        for _ in range(3):
            page=doc.new_page();page.insert_text((40,40),'REFERENCE 825 ohms')
        doc.save(src)
    cfg=SimpleNamespace(wiki_root=root, runtime_dir=root/'.llm-wiki', extract_tmp_dir=root/'extract')
    media=root/'wiki/media'/Path(name).stem;media.mkdir(parents=True,exist_ok=True)
    chunk=cfg.extract_tmp_dir/media.name/'_chunk_0001';chunk.mkdir(parents=True,exist_ok=True)
    if blocks is None:
        blocks=[{'type':'text','text':'unique正文 825 ohms','page_idx':0,'bbox':[10,10,900,100]},
                {'type':'equation','text':r'\bar{y}=825','page_idx':0,'bbox':[10,100,500,200]},
                {'type':'image','img_path':'images/a.png','image_caption':['Figure 1. Test'],
                 'page_idx':0,'bbox':[10,200,500,400]}]
    response={'chunk':{'content_list':blocks,'images':{}}}
    mp=scan.persist_scan_evidence(response,src,cfg,chunk,start,end)
    return cfg,src,media,chunk,mp


def caption(cfg,src,media,chunk, *, verified=True):
    from PIL import Image
    image=media/'figure.png';Image.new('RGB',(32,32),'white').save(image)
    obs={'summary':'A measured polar pattern with a null.',**{f:[] for f in FIELDS}}
    obs['axes']=['UniqueAngle 37 degrees'];obs['uncertainties']=['Scale unclear']
    text=save_caption_evidence(image,obs,{'model':'test-vlm'},'English',{})
    Path(str(image)+'.caption.txt').write_text(text)
    (chunk/'_mineru_figures.json').write_text(json.dumps([{'filename':image.name,'mineru_basename':'a.png',
       'page':1,'page_mapping_verified':verified}]))
    evidence.bind_captions(cfg,src,media)
    return image


def test_text_formula_caption_retrieval_and_locations(tmp_path):
    cfg,src,media,chunk,mp=fixture(tmp_path)
    image=caption(cfg,src,media,chunk)
    assert len(json.loads(mp.read_text())['regions'])==3  # text is first-class
    text=evidence.search(tmp_path,'unique正文')['matches'][0]
    assert text['pdf_page']==2 and text['page_idx']==1
    assert text['evidence_status']=='source_current' and text['ingestion_status']=='not_asserted'
    figure=evidence.search(tmp_path,'UniqueAngle')['matches'][0]
    assert figure['kind']=='caption' and figure['model']=='test-vlm'
    assert figure['bbox']==[10,200,500,400]
    assert figure['verification']=='model-observation-not-verified'
    assert figure['parent_evidence_id']!=figure['evidence_id']
    before=figure['evidence_id']
    evidence.bind_captions(cfg,src,media)
    assert evidence.search(tmp_path,'UniqueAngle')['matches'][0]['evidence_id']==before
    # Durable retrieval doesn't need extraction caches.
    import shutil
    shutil.rmtree(cfg.extract_tmp_dir)
    assert evidence.search(tmp_path,'UniqueAngle')['matches']
    rendered=evidence.render(tmp_path,before,tmp_path/'render')
    from PIL import Image
    assert Image.open(rendered['page_image']).width>100
    assert Image.open(rendered['region_image']).width>100


def test_changed_deleted_source_not_current_or_renderable(tmp_path):
    cfg,src,*_=fixture(tmp_path)
    eid=evidence.search(tmp_path,'825')['matches'][0]['evidence_id']
    src.write_bytes(b'new version')
    assert not evidence.search(tmp_path,'825')['matches']
    assert evidence.collect(tmp_path,history=True)['matches'][0]['evidence_status']=='stale'
    with pytest.raises(ValueError):evidence.render(tmp_path,eid,tmp_path/'renders')
    src.unlink()
    assert evidence.collect(tmp_path)['unavailable_sources'][0]['evidence_status']=='missing_source'


def test_same_named_books_and_ambiguous_page_sources(tmp_path):
    fixture(tmp_path,'A/manual.pdf')
    fixture(tmp_path,'B/manual.pdf')
    hits=evidence.collect(tmp_path,source='raw/A/manual.pdf')['matches']
    assert {h['source'] for h in hits}=={'raw/A/manual.pdf'}
    p=tmp_path/'wiki/concepts/test.md';p.parent.mkdir(parents=True)
    p.write_text('---\nsources: [manual.pdf]\n---\nTest')
    result=evidence.collect(tmp_path,page='concepts/test.md')
    assert result['ambiguous_sources'] and not result['matches']
    p.write_text('---\nsources: [raw/A/manual.pdf]\n---\nTest')
    assert {m['source'] for m in evidence.collect(tmp_path,page='concepts/test.md')['matches']}=={'raw/A/manual.pdf'}
    with pytest.raises(ValueError):evidence.collect(tmp_path,page='../../escape')


def test_unknown_page_never_guessed(tmp_path):
    fixture(tmp_path,blocks=[{'type':'text','text':'unknownlocation','bbox':[0,0,100,100]}])
    r=evidence.search(tmp_path,'unknownlocation')['matches'][0]
    assert r['pdf_page'] is None and not r['can_render']
    with pytest.raises(ValueError,match='guess'):evidence.render(tmp_path,r['evidence_id'],tmp_path/'out')


def test_caption_hash_change_and_unverified_mapping(tmp_path):
    cfg,src,media,chunk,mp=fixture(tmp_path)
    image=caption(cfg,src,media,chunk,verified=False)
    assert not evidence.search(tmp_path,'UniqueAngle')['matches']
    image=caption(cfg,src,media,chunk)
    Path(str(image)+'.caption.txt').write_text('Injected different observations')
    result=evidence.search(tmp_path,'Injected')
    assert not result['matches'] and result['diagnostics']
    # Original OCR remains retrievable.
    assert evidence.search(tmp_path,'825')['matches']


def test_review_status_and_table_remain_separate(tmp_path):
    _,_,_,_,mp=fixture(tmp_path,blocks=[{'type':'table','table_body':'<table><tr><td colspan="2">Span825</td></tr></table>',
                                      'page_idx':0,'bbox':[10,10,900,900]}])
    mp.with_name('region-review.json').write_text(json.dumps({'regions':{'r0000':{'status':'needs-review','secondary':'different','reasons':['manual']}}}))
    item=evidence.search(tmp_path,'Span825')['matches'][0]
    assert 'colspan="2"' in item['text']
    assert item['review']['status']=='needs-review' and item['review']['secondary']=='different'
    assert 'different' not in item['text']


def test_old_v1_manifest_text_retrieved_without_mutation(tmp_path):
    _,_,_,_,mp=fixture(tmp_path)
    m=json.loads(mp.read_text());m['regions']=m['regions'][1:];mp.write_text(json.dumps(m))
    before=mp.read_bytes()
    assert evidence.search(tmp_path,'unique正文')['matches'][0]['kind']=='text'
    assert mp.read_bytes()==before


def test_corrupt_artifact_is_reported_and_other_sources_survive(tmp_path):
    _,_,_,_,mp=fixture(tmp_path,'broken.pdf')
    mp.with_name('content-list.json').write_text('broken')
    fixture(tmp_path,'good.pdf')
    result=evidence.search(tmp_path,'825')
    assert result['diagnostics']
    assert {r['source'] for r in result['matches']}=={'raw/good.pdf'}


def test_cli_evidence_and_mixed_results_even_without_wiki_hits(tmp_path):
    cfg,src,media,chunk,mp=fixture(tmp_path)
    script=Path(__file__).resolve().parents[1]/'search_wiki.py'
    cmd=[sys.executable,'-B',str(script),'825','--project',str(tmp_path),'--scope','all','--keyword-only','--json']
    result=subprocess.run(cmd,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    hits=json.loads(result.stdout);assert hits[0]['result_type']=='source_evidence'
    page=tmp_path/'wiki/concepts/a.md';page.parent.mkdir(parents=True);page.write_text('# Resistor\n825 ohms')
    result=subprocess.run(cmd,capture_output=True,text=True)
    assert {h['result_type'] for h in json.loads(result.stdout)}=={'wiki','source_evidence'}
    lookup=script.with_name('evidence_lookup.py')
    result=subprocess.run([sys.executable,'-B',str(lookup),'--project',str(tmp_path),'--id',hits[0]['evidence_id'],'--json'],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert len(json.loads(result.stdout)['matches'])==1


@pytest.mark.parametrize('box',[[0,0,float('nan'),10],[0,0,10,float('inf')],[False,0,5,5],[-1,0,5,5]])
def test_invalid_bbox(box):
    assert not evidence.valid_bbox(box)


def test_query_hashes_only_matching_sources(tmp_path, monkeypatch):
    fixture(tmp_path,'a.pdf',blocks=[{'type':'text','text':'unique-a','page_idx':0}])
    fixture(tmp_path,'b.pdf',blocks=[{'type':'text','text':'unique-b','page_idx':0}])
    actual=evidence.source_hash;seen=[]
    monkeypatch.setattr(evidence,'source_hash',lambda p:(seen.append(p.name) if p.suffix=='.pdf' else None,actual(p))[1])
    evidence.search(tmp_path,'unique-a')
    # Tokenization of hyphenated query also matches 'unique'; use a unique literal.
    seen.clear()
    evidence.search(tmp_path,'absent_xyz_never')
    assert not seen
    hits=evidence.collect(tmp_path,source='raw/a.pdf')['matches'];seen.clear()
    evidence.collect(tmp_path,evidence_id=hits[0]['evidence_id'])
    assert seen==['a.pdf']


def test_caption_join_refuses_another_source_pointer(tmp_path):
    cfg,src,media,chunk,mp=fixture(tmp_path)
    data=json.loads(mp.read_text());data['source']='raw/other.pdf';mp.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='identity/version'):
        evidence.bind_captions(cfg,src,media)


def test_ocr_number_spacing_search_preserves_raw_transcription(tmp_path):
    raw = r"\Delta = 6 . 6 1 / \sqrt {f}"
    fixture(tmp_path,blocks=[{'type':'equation','text':raw,'page_idx':0}])
    result=evidence.search(tmp_path,'6.61')['matches']
    assert result and result[0]['text']==raw


def test_modified_parser_content_is_not_current_evidence(tmp_path):
    _,_,_,_,mp=fixture(tmp_path)
    cp=mp.with_name('content-list.json');blocks=json.loads(cp.read_text())
    blocks[0]['text']='forged value';cp.write_text(json.dumps(blocks))
    result=evidence.search(tmp_path,'forged')
    assert not result['matches'] and result['diagnostics']


def test_uncaptured_same_named_source_still_makes_legacy_ref_ambiguous(tmp_path):
    fixture(tmp_path,'A/manual.pdf')
    other=tmp_path/'raw/B/manual.pdf';other.parent.mkdir(parents=True);other.write_bytes(b'uncaptured')
    page=tmp_path/'wiki/concepts/x.md';page.parent.mkdir(parents=True)
    page.write_text('---\nsources: [manual.pdf]\n---\nBody')
    result=evidence.collect(tmp_path,page='concepts/x.md')
    assert result['ambiguous_sources'] and not result['matches']


def test_missing_chunk_metadata_does_not_destroy_durable_caption_binding(tmp_path):
    cfg,src,media,chunk,mp=fixture(tmp_path)
    caption(cfg,src,media,chunk)
    before=mp.with_name('caption-links.json').read_bytes()
    (chunk/'_mineru_figures.json').unlink()
    evidence.bind_captions(cfg,src,media)
    assert mp.with_name('caption-links.json').read_bytes()==before
    assert evidence.search(tmp_path,'UniqueAngle')['matches']
