"""Stage 3.6: Embeddings (强制执行，2026-06-20 改为必需)

本地有 bge-m3（Ollama）时同步生成向量；不可用时优雅跳过——真正的 embedding
由 ingest 主流程的 ``_auto_embed_new_pages`` 负责（``build_embeddings.py``，
走 ``EMBEDDING_BASE_URL`` / ``EMBEDDING_MODEL``，默认 Ollama 的
``http://127.0.0.1:11434/v1``）。本 stage 仅在本地有 bge-m3 时作为同步触发，
避免重复造一套 embedding 入口。

历史：曾有一个 "对话 LLM 降级" 分支（``embed_with_dialogue_llm``），用内容
md5 哈希生成 384 维伪向量写进 ``embeddings.json``——既非真语义向量也不入
LanceDB，纯 demo 残留，已于 2026-06-21 移除。同时移除了 ``input()`` 交互式
提示（阻塞管线）。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def check_local_bge_m3() -> bool:
    """检查本地 Ollama 是否有 bge-m3 模型。"""
    try:
        import requests
        response = requests.get('http://localhost:11434/api/tags', timeout=2)
        if response.status_code == 200:
            models = response.json().get('models', [])
            return any('bge-m3' in m['name'] for m in models)
    except Exception:
        pass
    return False


def embed_with_local_bge_m3(wiki_root: Path) -> bool:
    """使用本地 Ollama bge-m3 模型生成 embedding。"""
    try:
        print("📊 使用本地 BGE-M3 生成 embedding...")
        result = subprocess.run([
            'python3',
            'scripts/build_embeddings.py',
            '--project', str(wiki_root),
            'embed'
        ], capture_output=True, text=True, timeout=3600)

        if result.returncode != 0:
            print(f"❌ BGE-M3 embedding 失败：{result.stderr}")
            return False

        print("✓ Embedding 完成")
        print(result.stdout)
        return True
    except Exception as e:
        print(f"❌ Embedding 执行失败：{e}")
        return False


def verify_embeddings(wiki_root: Path, checkpoint: dict) -> bool:
    """验证 embedding 是否成功。"""
    lancedb_path = wiki_root / "lancedb"

    if not lancedb_path.exists():
        print("❌ LanceDB 目录不存在")
        return False

    embeddings_file = lancedb_path / "embeddings.json"
    if not embeddings_file.exists():
        print("❌ embeddings.json 不存在")
        return False

    try:
        with open(embeddings_file) as f:
            vectors = json.load(f)

        if not vectors:
            print("❌ 没有向量数据")
            return False

        print(f"✓ 向量验证通过（{len(vectors)} 个向量）")
        return True
    except Exception as e:
        print(f"❌ 向量验证失败：{e}")
        return False


def stage_3_6_embeddings(
    checkpoint: dict,
    wiki_root: Path,
    file_blocks: list[tuple[str, str]] | None = None,
) -> bool:
    """Stage 3.6: Embeddings（强制执行）。

    本地 BGE-M3 可用时同步生成向量；不可用时优雅跳过（真正的向量由 ingest
    主流程 ``_auto_embed_new_pages`` 通过 ``EMBEDDING_BASE_URL`` 生成）。

    Args:
        checkpoint: 进度检查点。
        wiki_root: wiki 根目录。
        file_blocks: 保留以兼容旧调用方，当前未使用。

    Returns:
        True 如果成功或跳过；False 如果 BGE-M3 可用但生成/验证失败。
    """
    _ = file_blocks  # 兼容旧签名，不再使用

    print("\n" + "=" * 70)
    print("Stage 3.6: Embeddings（强制执行）")
    print("=" * 70)

    if not check_local_bge_m3():
        print("⚠️  本地 BGE-M3 未检测到，跳过 Stage 3.6 同步 embedding。")
        print("    真正的向量由 ingest 主流程的 _auto_embed_new_pages 生成"
              "（需设置 EMBEDDING_BASE_URL）。")
        print("    如需本 stage 同步生成，安装 Ollama 后 `ollama pull bge-m3`。")
        checkpoint["embeddings_completed"] = False
        checkpoint["embedding_mode"] = "skipped"
        return True

    if not embed_with_local_bge_m3(wiki_root):
        return False

    if not verify_embeddings(wiki_root, checkpoint):
        return False

    checkpoint["embeddings_completed"] = True
    checkpoint["embedding_mode"] = "local_bge_m3"
    print("✓ Stage 3.6 完成")
    return True
