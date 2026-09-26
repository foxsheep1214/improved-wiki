"""Context budget precedence and resume safety across budget changes."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import _core
import _ingest_chunks
from _config import Config
from _context_budget import (
    CONTEXT_ENV, apply_context_budget, resolve_context_tokens,
    save_project_context_tokens,
)
from _progress import file_sha256, load_progress, save_progress, stages_path
from _task_manifest import (
    TaskManifestError, _stable_hash, ensure_task_manifest, task_manifest_path,
)

SCRIPTS = Path(__file__).resolve().parents[1]


def _config(root: Path, context: int) -> Config:
    rt = root / ".llm-wiki"
    cfg = Config(
        wiki_root=root, raw_root=root / "raw", wiki_dir=root / "wiki",
        runtime_dir=rt, cache_path=rt / "ingest-cache.json",
        progress_dir=rt / "ingest-progress", extract_tmp_dir=rt / "extract-tmp",
        llm_model="", caption_api_key="", caption_base_url="", caption_model="",
        chunk_overlap=3000, source_budget=0, target_chars=0, target_tokens=0,
        max_tokens=8192)
    cfg.apply_context(context)
    return cfg


def _raw(root: Path) -> Path:
    path = root / "raw/Book/file.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"source bytes")
    return path


# ── precedence ───────────────────────────────────────────────────────────────

def test_precedence_cli_env_project_default(tmp_path, monkeypatch):
    monkeypatch.delenv(CONTEXT_ENV, raising=False)
    assert resolve_context_tokens(runtime_dir=tmp_path)[1] == "conservative default"
    save_project_context_tokens(tmp_path, 200000)
    assert resolve_context_tokens(runtime_dir=tmp_path)[0] == 200000
    monkeypatch.setenv(CONTEXT_ENV, "128000")
    assert resolve_context_tokens(runtime_dir=tmp_path)[0] == 128000
    assert resolve_context_tokens(1000000, runtime_dir=tmp_path)[0] == 1000000


def test_apply_uses_project_setting(tmp_path, monkeypatch):
    monkeypatch.delenv(CONTEXT_ENV, raising=False)
    save_project_context_tokens(tmp_path, 200000)
    values = []
    apply_context_budget(type("C", (), {"runtime_dir": tmp_path,
                                        "apply_context": values.append})())
    assert values == [200000]


@pytest.mark.parametrize("content", ["not json", '{"context_tokens": 1000}', "[]"])
def test_invalid_project_setting_is_an_error(tmp_path, monkeypatch, content):
    monkeypatch.delenv(CONTEXT_ENV, raising=False)
    (tmp_path / "context-budget.json").write_text(content)
    with pytest.raises(ValueError):
        resolve_context_tokens(runtime_dir=tmp_path)


def test_set_context_tokens_cli(tmp_path):
    env = dict(os.environ, IMPROVED_WIKI_ROOT=str(tmp_path))
    env.pop(CONTEXT_ENV, None)
    ok = subprocess.run([sys.executable, str(SCRIPTS / "ingest.py"),
                         "--set-context-tokens", "200000"],
                        cwd=tmp_path, env=env, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    saved = json.loads((tmp_path / ".llm-wiki/context-budget.json").read_text())
    assert saved == {"context_tokens": 200000}
    bad = subprocess.run([sys.executable, str(SCRIPTS / "ingest.py"),
                          "--set-context-tokens", "32000"],
                         cwd=tmp_path, env=env, capture_output=True, text=True)
    assert bad.returncode == 2
    assert json.loads((tmp_path / ".llm-wiki/context-budget.json").read_text()) == saved


# ── chunk plan identity ──────────────────────────────────────────────────────

def _plan(cfg: Config, text: str) -> dict:
    meta, _ = _ingest_chunks._build_chunk_meta(text, cfg)
    return _ingest_chunks._build_chunk_plan(text, cfg, meta)


_DIGEST = {"book_meta": {"title": "Book"}, "outline": [], "key_entities": [],
           "key_concepts": [{"name": "buck", "definition": "d"}], "key_claims": []}


def _analyses(plan: dict) -> list[dict]:
    total = plan["chunk_total"]
    return [{
        "chunk_index": c["index"], "chunk_total": total,
        "concepts_found": [{"name": "buck", "importance": "core",
                            "definition": "d", "key_details": ["x"]}],
        "entities_found": [], "claims": [], "formulas": [], "structured_data": [],
        "connections_to_existing_wiki": [], "schema_typed_candidates": [],
        "updated_global_digest": _DIGEST,
        "_chunk_index": c["index"], "_chunk_id": c["chunk_id"],
        "_chunk_text_sha256": c["text_sha256"],
    } for c in plan["chunks"]]


def test_budget_only_change_keeps_chunk_checkpoint(tmp_path):
    text = "Paragraph about buck converters.\n\n" * 400
    old = _plan(_config(tmp_path, 200000), text)
    same_chunks = _plan(_config(tmp_path, 1000000), text)
    assert old["context_size"] != same_chunks["context_size"]
    saved = {"chunk_plan_v2": old, "chunk_analyses": _analyses(old)}
    assert _ingest_chunks._chunk_checkpoint_mismatch(saved, same_chunks) is None
    smaller = _plan(_config(tmp_path, 64000), text)
    assert "target_tokens" in _ingest_chunks._chunk_checkpoint_mismatch(saved, smaller)


def test_restore_binds_saved_plan_after_budget_only_change(tmp_path, monkeypatch):
    text = "Paragraph about buck converters.\n\n" * 400
    raw = _raw(tmp_path)
    cfg = _config(tmp_path, 200000)
    ensure_task_manifest(raw, cfg)
    h = file_sha256(raw)
    saved_plan = _plan(cfg, text)
    save_progress(cfg, h, {"chunk_plan_v2": saved_plan,
                           "chunk_analyses": _analyses(saved_plan),
                           "global_digest": _DIGEST})
    _core.mark_stage_done(cfg, h, "stage_2_2_done")

    monkeypatch.setattr(_ingest_chunks, "_verify_stage_2_2_chunks", lambda *_a, **_k: None)
    monkeypatch.setattr(_ingest_chunks, "_verify_stage_2_2_digest", lambda *_a, **_k: None)
    cfg_big = _config(tmp_path, 1000000)
    with pytest.raises(_ingest_chunks.PrepareStopAfter):
        _ingest_chunks._run_chunk_pipeline(
            text, {}, raw, cfg_big, "", load_progress(cfg_big, h), False,
            analyze_only=True)
    assert _core.is_stage_done(cfg_big, h, "stage_2_2_done")
    manifest = json.loads(task_manifest_path(cfg_big, h).read_text())
    assert manifest["resume"]["chunk_plan_sha256"] == _stable_hash(saved_plan)


# ── manifest drift guard ─────────────────────────────────────────────────────

def _start_stage_2_2(tmp_path: Path, context: int):
    raw = _raw(tmp_path)
    cfg = _config(tmp_path, context)
    ensure_task_manifest(raw, cfg)
    h = file_sha256(raw)
    _core.mark_stage_done(cfg, h, "stage_1_3_done")
    save_progress(cfg, h, {"wiki_index_snapshot_2_2": "# index"})
    return raw, h


def test_changed_chunking_during_stage_2_2_is_rejected(tmp_path):
    raw, h = _start_stage_2_2(tmp_path, 200000)
    before = task_manifest_path(_config(tmp_path, 200000), h).read_bytes()
    with pytest.raises(TaskManifestError, match="--context-tokens 200000"):
        ensure_task_manifest(raw, _config(tmp_path, 64000))
    assert task_manifest_path(_config(tmp_path, 200000), h).read_bytes() == before


def test_source_budget_pinned_until_generation_cached(tmp_path):
    raw, h = _start_stage_2_2(tmp_path, 200000)
    with pytest.raises(TaskManifestError, match="source_budget"):
        ensure_task_manifest(raw, _config(tmp_path, 1000000))
    cfg = _config(tmp_path, 200000)
    _core.mark_stage_done(cfg, h, "stage_2_3_done")
    ensure_task_manifest(raw, _config(tmp_path, 1000000))  # same chunk targets


def test_explicit_delete_releases_the_pin(tmp_path):
    raw, h = _start_stage_2_2(tmp_path, 200000)
    stages_path(_config(tmp_path, 200000), h).unlink()
    ensure_task_manifest(raw, _config(tmp_path, 64000))


def test_phase_1_only_task_accepts_a_new_budget(tmp_path):
    raw = _raw(tmp_path)
    cfg = _config(tmp_path, 64000)
    ensure_task_manifest(raw, cfg)
    _core.mark_stage_done(cfg, file_sha256(raw), "stage_1_3_done")
    ensure_task_manifest(raw, _config(tmp_path, 200000))
