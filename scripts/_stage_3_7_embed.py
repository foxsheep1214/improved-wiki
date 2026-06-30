"""Stage 3.7 embedding (post-write).

Runs after Stage 3 writes wiki pages to disk: embeds new pages into the
local LanceDB for semantic retrieval (mandatory; **pauses the ingest** if
the local Ollama/lancedb/bge-m3 stack is missing — no silent fallback).

Stage 3.7 is the FINAL ingest stage: after it, _finalize_book sets the
completion marker. (The former Stage 4.1 post-ingest validation audit was
removed for NashSU alignment — NashSU has no such stage; its only ingest-time
check, schema routing, runs at write time in Stage 3.1.)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from _core import Config


def _stage_3_7_check_embed_capability(base_url: str, model: str) -> tuple[bool, str]:
    """Probe local embedding capability: lancedb installed + Ollama reachable + model pulled.

    Returns (ok, reason). reason is empty when ok, otherwise a human-readable
    cause used to build the install reminder.
    """
    try:
        import lancedb  # noqa: F401
    except ImportError:
        return False, "lancedb 未安装"

    import urllib.request
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    try:
        with urllib.request.urlopen(f"{root}/api/tags", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return False, f"无法连接本地 Ollama（{root}）"

    names = {m.get("model", "").split(":")[0] for m in data.get("models", [])}
    if model.split(":")[0] not in names:
        return False, f"Ollama 已运行，但模型 {model} 未拉取"
    return True, ""


def stage_3_7_embed_new_pages(config: Config, files_written: list[str]) -> None:
    """Stage 3.7: embed wiki pages for semantic retrieval (mandatory).

    NashSU parity (ingest.ts L1127-1146). Always attempts embedding against
    local Ollama bge-m3 (default http://127.0.0.1:11434/v1). If the local
    capability is missing (Ollama not running, model not pulled, or lancedb
    not installed), **pauses the ingest** — no silent fallback, no degraded
    keyword-only retrieval (policy 2026-06-24: a missing required dependency
    is a hard stop, not a warn-and-continue). Pages are already on disk, so
    re-running after fixing the stack resumes from here with no re-extraction.
    """
    base_url = os.environ.get("EMBEDDING_BASE_URL", "http://127.0.0.1:11434/v1")
    model = os.environ.get("EMBEDDING_MODEL", "bge-m3")

    ok, reason = _stage_3_7_check_embed_capability(base_url, model)
    if not ok:
        print(f"\n⚠️  [stage 3.7] Embeddings 不可用：{reason}")
        print(f"⚠️  [stage 3.7] PAUSING ingest — no silent fallback. Semantic retrieval "
              f"is a required stage, not optional. Fix and re-run (pages are cached, "
              f"resumes here):")
        print("  1. brew install ollama          # 如未安装")
        print("  2. ollama serve                 # 如未启动")
        print(f"  3. ollama pull {model}")
        print("  4. pip install lancedb")
        print(f"  5. 重跑 ingest（页面已落盘，从此处恢复，无需重新提取/生成）\n")
        raise RuntimeError(
            f"Embedding stack unavailable ({reason}) — Stage 3.7 cannot run. "
            f"No fallback: start Ollama, pull {model}, and pip install lancedb, "
            f"then re-run. The ingest pauses here; pages already written are "
            f"cached and the run resumes from this stage."
        )

    skip_files = {"index.md", "log.md", "overview.md", "schema.md"}
    # files_written paths are relative to wiki_root and already carry the
    # leading "wiki/" segment (e.g. "wiki/concepts/foo.md"). Resolve against
    # wiki_root; joining wiki_dir would double the "wiki/" prefix and the
    # existence check would silently fail, skipping embeddings entirely.
    # Fall back to wiki_dir for any caller that passes wiki-dir-relative paths.
    new_files = []
    for f in files_written:
        if Path(f).name in skip_files:
            continue
        p = config.wiki_root / f
        if not p.exists():
            p = config.wiki_dir / f
        if p.exists():
            new_files.append(str(p))
    if not new_files:
        return

    print(f"[stage 3.7] Embedding {len(new_files)} new pages...")
    import subprocess
    script = Path(__file__).parent / "build_embeddings.py"
    # build_embeddings.py `embed` re-chunks and embeds EVERY uncached page in the
    # whole wiki (incremental via a per-chunk sha cache), not just `new_files`.
    # On a healthy run only the new pages' chunks are uncached, so it returns in
    # seconds. But the FIRST embed after a backlog — e.g. a project that predates
    # the Stage 3.7 path-bug fix (2026-06-30) and therefore never actually
    # embedded — must backfill the entire wiki, which can take many minutes. A
    # fixed 300s cap turns that legitimate one-time backfill into a false
    # RuntimeError under the no-fallback policy (each re-run only chips away
    # within one 300s window and never reaches the completion marker). Scale the
    # cap with the wiki's page count (the actual embed workload), floored at
    # 600s, so a large backfill has room to finish while a genuinely hung embed
    # still eventually trips. The cap only bounds slow runs — a fast incremental
    # embed returns immediately regardless.
    try:
        page_count = sum(1 for _ in config.wiki_dir.rglob("*.md"))
    except Exception:
        page_count = len(new_files)
    embed_timeout = max(600, page_count * 2)
    proc = subprocess.run(
        [sys.executable, str(script), "--project", str(config.wiki_root), "embed"],
        capture_output=True, text=True, timeout=embed_timeout,
    )
    if proc.returncode != 0:
        # No silent fallback (consistent with the capability gate above): a failed
        # embed must not be reported as complete. Pages are already written and
        # cached, so a re-run resumes from this stage.
        tail = (proc.stderr or proc.stdout or "").strip()[-1000:]
        raise RuntimeError(
            f"Stage 3.7 embedding failed (build_embeddings.py exit "
            f"{proc.returncode}). Pages are written + cached; fix the embedding "
            f"stack and re-run to resume.\n{tail}"
        )
    print(f"[stage 3.7] Embedding complete")
