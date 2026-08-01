"""Stage 3.7 embedding (post-write).

Runs after Stage 3 writes wiki pages to disk: page-scoped replacement of the
new/updated pages' chunks in LanceDB.  This matches NashSU 0.6.6's ingest
lifecycle; a full-wiki rebuild is a separate explicit ``build_embeddings.py
embed`` operation.

Embedding remains mandatory in improved-wiki: a missing configured backend or
an incomplete touched page pauses the ingest instead of silently degrading.

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
import urllib.parse
import urllib.request

from _config import Config
from _page_ref import PageRef, PageRefError


def _stage_3_7_check_embed_capability(base_url: str, model: str) -> tuple[bool, str]:
    """Probe LanceDB and, for the default local Ollama endpoint, its model.

    NashSU 0.6.6 supports Google, Volcengine, and arbitrary OpenAI-compatible
    endpoints.  Those endpoints are validated by the real embedding request;
    applying an Ollama-specific ``/api/tags`` probe to them is incorrect.
    """
    try:
        import lancedb  # noqa: F401
    except ImportError:
        return False, "lancedb 未安装"

    parsed = urllib.parse.urlparse(base_url)
    host = (parsed.hostname or "").lower()
    is_default_ollama = (
        host in {"localhost", "127.0.0.1", "::1"}
        and (parsed.port or 11434) == 11434
    )
    if not is_default_ollama:
        if not base_url.strip():
            return False, "embedding endpoint 未配置"
        if not model.strip():
            return False, "embedding model 未配置"
        return True, ""

    root = f"{parsed.scheme or 'http'}://{parsed.netloc}"
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

    The page-scoped upsert mirrors NashSU 0.6.6 ``embedPage``: only paths
    written by this ingest are re-chunked and replaced.  improved-wiki keeps
    its stricter completion policy: every touched chunk must embed and the
    post-write page row count must match before ``ingested`` may be set.
    """
    base_url = (
        os.environ.get("EMBEDDING_ENDPOINT")
        or os.environ.get("EMBEDDING_BASE_URL")
        or "http://127.0.0.1:11434/v1"
    )
    model = os.environ.get("EMBEDDING_MODEL", "bge-m3")

    ok, reason = _stage_3_7_check_embed_capability(base_url, model)
    if not ok:
        print(f"\n⚠️  [stage 3.7] Embeddings 不可用：{reason}")
        print("⚠️  [stage 3.7] PAUSING ingest — no silent fallback. Semantic retrieval "
              "is a required stage, not optional. Fix and re-run (pages are written, "
              "resumes here):")
        print("  1. brew install ollama          # 如未安装")
        print("  2. ollama serve                 # 如未启动")
        print(f"  3. ollama pull {model}")
        print("  4. pip install lancedb")
        print("  5. 重跑 ingest（页面已落盘，从此处恢复，无需重新提取/生成）\n")
        raise RuntimeError(
            f"Embedding stack unavailable ({reason}) — Stage 3.7 cannot run. "
            f"No fallback: start Ollama, pull {model}, and pip install lancedb, "
            f"then re-run. The ingest pauses here; pages already written remain "
            f"on disk and the run resumes from this stage."
        )

    skip_files = {"index.md", "log.md", "overview.md", "purpose.md", "schema.md"}
    # files_written paths are relative to wiki_root and already carry the
    # leading "wiki/" segment (e.g. "wiki/concepts/foo.md"). Resolve against
    # wiki_root; joining wiki_dir would double the "wiki/" prefix and the
    # existence check would silently fail, skipping embeddings entirely.
    # Fall back to wiki_dir for any caller that passes wiki-dir-relative paths.
    new_files = []
    for f in files_written:
        try:
            ref = PageRef.parse(f, config.wiki_root, config.wiki_dir)
        except PageRefError as exc:
            raise RuntimeError(
                f"Stage 3.7 received an invalid page reference {f!r}: {exc}"
            ) from exc
        if ref.name in skip_files:
            continue
        if ref.absolute_path.exists():
            new_files.append(str(ref.absolute_path))
    if not new_files:
        return

    print(f"[stage 3.7] Replacing embeddings for {len(new_files)} written pages...")
    import subprocess
    script = Path(__file__).parent / "build_embeddings.py"
    # One page may contain several chunks.  Scale with the actual touched-page
    # set, not the whole wiki: Stage 3.7 no longer performs a full rebuild.
    embed_timeout = max(600, len(new_files) * 30)
    command = [
        sys.executable,
        str(script),
        "--project",
        str(config.wiki_root),
        "upsert",
    ]
    for path in new_files:
        command.extend(["--page", path])
    try:
        proc = subprocess.run(
            command,
            capture_output=True, text=True, timeout=embed_timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Stage 3.7 embedding timed out after {embed_timeout}s "
            f"({len(new_files)} touched pages). Pages remain written; re-run "
            f"to resume."
        )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-1000:]
        raise RuntimeError(
            f"Stage 3.7 embedding failed (build_embeddings.py exit "
            f"{proc.returncode}). Pages are written; fix the embedding "
            f"stack and re-run to resume.\n{tail}"
        )
    if proc.stdout.strip():
        print(proc.stdout.rstrip())
    print("[stage 3.7] Incremental embedding complete")
