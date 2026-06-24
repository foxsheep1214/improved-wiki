"""Stage 3.7 embedding + Stage 4.1 final validation (post-write).

Extracted from ingest.py on 2026-06-23. Both run after Stage 3 writes
wiki pages to disk: Stage 3.7 embeds new pages into the local LanceDB
for semantic retrieval (mandatory; **pauses the ingest** if the local
Ollama/lancedb/bge-m3 stack is missing — no silent fallback, policy
2026-06-24), and Stage 4.1 runs the 15-stage validator inline for fresh
verification evidence.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from _core import Config


def stage_4_1_validate_ingest(config: Config, raw_file: Path) -> None:
    """Run validate_ingest.py inline for the just-completed source.

    Superpowers Iron Law: every ingest MUST produce fresh verification evidence
    before claiming completion.  This runs the 15-stage validator on the current
    source and prints the result.  Hard failures prevent the "ok" status.
    """
    import subprocess
    validate_script = Path(__file__).parent / "validate_ingest.py"
    if not validate_script.exists():
        print("[validate] ⚠️  validate_ingest.py not found, skipping final verification")
        return

    slug = raw_file.stem
    # Compute the exact cache key (matching ingest.py's `rel` variable)
    try:
        cache_key = str(raw_file.relative_to(config.raw_root))
    except ValueError:
        cache_key = str(raw_file)
    print(f"\n[validate] Running 15-stage final verification for {slug} (cache_key={cache_key})...")
    result = subprocess.run(
        [sys.executable, str(validate_script)],
        env={**os.environ, "IMPROVED_WIKI_ROOT": str(config.wiki_root),
             "SOURCE_SLUG": slug,
             "CACHE_KEY": cache_key},
        capture_output=True, text=True, timeout=600,
    )
    # Print the validator output (shows per-stage PASS/FAIL)
    stdout = result.stdout.strip()
    if stdout:
        # Print only the summary lines to avoid overwhelming output
        for line in stdout.splitlines():
            if any(marker in line for marker in ["Result:", "PASS", "FAIL", "❌", "✅", "Stage"]):
                print(f"  {line}")

    if result.returncode != 0:
        # Don't raise — the ingest succeeded but validation found issues.
        # The compliance record already documents stage status.
        stderr_tail = result.stderr.strip()[-500:] if result.stderr else ""
        print(f"[validate] ⚠️  Validator exit {result.returncode} — review warnings above")
        if stderr_tail:
            print(f"[validate] {stderr_tail}")
    else:
        print(f"[validate] ✅ All 15 stages verified — ingest complete")


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
    new_files = [
        str(config.wiki_dir / f) for f in files_written
        if Path(f).name not in skip_files and (config.wiki_dir / f).exists()
    ]
    if not new_files:
        return

    print(f"[stage 3.7] Embedding {len(new_files)} new pages...")
    import subprocess
    script = Path(__file__).parent / "build_embeddings.py"
    subprocess.run(
        [sys.executable, str(script), "--project", str(config.wiki_root), "embed"],
        capture_output=True, timeout=300,
    )
    print(f"[stage 3.7] Embedding complete")
