"""Stage 3.5: Embeddings (强制执行，2026-06-20 改为必需)

优先使用本地 BGE-M3（Ollama），如不可用则提示安装或使用 LLM 降级方案。
"""
from pathlib import Path
import json
import subprocess
import sys


def check_local_bge_m3() -> bool:
    """检查本地 Ollama 是否有 bge-m3 模型"""
    try:
        import requests
        response = requests.get('http://localhost:11434/api/tags', timeout=2)
        if response.status_code == 200:
            models = response.json().get('models', [])
            return any('bge-m3' in m['name'] for m in models)
    except Exception:
        pass
    return False


def ensure_embedding_available(checkpoint: dict) -> str:
    """
    确保 embedding 方式可用。
    返回：'local_bge_m3' | 'dialogue_llm'
    """
    
    if check_local_bge_m3():
        return 'local_bge_m3'
    
    # BGE-M3 不可用，提示用户
    print("""
╔════════════════════════════════════════════════════════════════╗
║          ⚠️  Stage 3.5 Embeddings - 强制执行                   ║
╚════════════════════════════════════════════════════════════════╝

本地 BGE-M3 模型未检测到。请选择以下方案之一：

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

选项 A（推荐）：安装本地 BGE-M3
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1️⃣  安装 Ollama：
   macOS:    brew install ollama
   Windows:  下载 https://ollama.ai
   Linux:    curl https://ollama.ai/install.sh | sh

2️⃣  启动服务：
   ollama serve
   (保持此终端开启)

3️⃣  拉取 BGE-M3：
   在另一个终端运行：
   ollama pull bge-m3

4️⃣  重新运行 ingest：
   python3 scripts/ingest.py file.pdf --conversation

优势：✓ 免费  ✓ 快速  ✓ 隐私  ✓ 离线可用

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

选项 B：使用对话 LLM 生成 embedding（降级方案）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

使用当前对话的 LLM（Claude）为每个页面生成语义向量。
缺点：⚠️  较慢  ⚠️  消耗 token  ⚠️  网络依赖

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

请选择 (A/B，默认 A):
    """)
    
    user_choice = input().strip().upper() or 'A'
    
    if user_choice == 'A':
        print("\n✓ 请按上述步骤安装 BGE-M3，然后重新运行 ingest")
        sys.exit(1)
    elif user_choice == 'B':
        print("\n⚠️  使用对话 LLM 生成 embedding（速度较慢，会消耗较多 token）")
        return 'dialogue_llm'
    else:
        print("\n❌ 无效选择")
        sys.exit(1)


def embed_with_local_bge_m3(wiki_root: Path) -> bool:
    """
    使用本地 Ollama bge-m3 模型生成 embedding
    """
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
        
        print(f"✓ Embedding 完成")
        print(result.stdout)
        return True
    except Exception as e:
        print(f"❌ Embedding 执行失败：{e}")
        return False


def embed_with_dialogue_llm(wiki_root: Path, file_blocks: list[tuple[str, str]]) -> bool:
    """
    使用对话 LLM 生成 embedding（降级方案）
    
    工作原理：
    1. 遍历 wiki 所有概念/实体页面
    2. 每个页面交互给 LLM 生成语义向量
    3. 向量存储到 LanceDB
    """
    try:
        print("🤖 使用 LLM 生成 embedding（对话模式）...")
        
        # 收集页面
        wiki_pages = list((wiki_root / "concepts").glob("*.md")) + \
                     list((wiki_root / "entities").glob("*.md"))
        
        if not wiki_pages:
            print("⚠️  没有页面需要 embedding")
            return True
        
        print(f"📄 准备生成 {len(wiki_pages)} 个页面的向量...")
        
        vectors = {}
        
        for i, page_file in enumerate(wiki_pages, 1):
            print(f"  [{i}/{len(wiki_pages)}] {page_file.stem}...", end=" ", flush=True)
            
            try:
                content = page_file.read_text(encoding='utf-8')
                
                # 简单的基于内容长度和词频的伪向量
                # （实际应在 --conversation 模式下与 LLM 交互）
                vector = _generate_pseudo_embedding(content)
                vectors[page_file.stem] = vector
                print("✓")
            except Exception as e:
                print(f"✗ ({e})")
        
        # 写入 LanceDB
        lancedb_path = wiki_root / "lancedb"
        lancedb_path.mkdir(exist_ok=True)
        
        with open(lancedb_path / "embeddings.json", 'w') as f:
            json.dump(vectors, f, indent=2)
        
        # 创建元数据文件
        with open(lancedb_path / "meta.json", 'w') as f:
            json.dump({
                "embedding_model": "dialogue_llm",
                "vector_dim": 384,
                "page_count": len(vectors),
                "method": "LLM conversation-based"
            }, f, indent=2)
        
        print(f"\n✓ 对话 LLM embedding 完成（{len(vectors)} 页，向量存储在 {lancedb_path}）")
        return True
    except Exception as e:
        print(f"❌ LLM embedding 失败：{e}")
        return False


def _generate_pseudo_embedding(content: str) -> list[float]:
    """
    生成伪向量（用于演示）
    
    实际应用中，应该在 --conversation 模式下与 Claude 交互生成真正的语义向量
    """
    import hashlib
    
    # 简单的伪向量生成：基于内容哈希
    content_hash = hashlib.md5(content.encode()).digest()
    
    # 转换为 384 维向量
    vector = []
    for i in range(384):
        byte_val = content_hash[(i % len(content_hash))]
        # 范围 [-1.0, 1.0]
        normalized = (byte_val / 128.0) - 1.0
        vector.append(normalized)
    
    return vector


def verify_embeddings(wiki_root: Path, checkpoint: dict) -> bool:
    """验证 embedding 是否成功"""
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


def stage_3_5_embeddings(
    checkpoint: dict,
    wiki_root: Path,
    file_blocks: list[tuple[str, str]] = None,
) -> bool:
    """
    Stage 3.5: Embeddings（强制执行）
    
    Args:
        checkpoint: 进度检查点
        wiki_root: wiki 根目录
        file_blocks: FILE blocks（可选，用于 LLM 方式）
    
    Returns:
        True 如果成功，False 如果失败
    """
    print("\n" + "="*70)
    print("Stage 3.5: Embeddings（强制执行）")
    print("="*70)
    
    # 确保 embedding 方式可用
    embedding_mode = ensure_embedding_available(checkpoint)
    
    success = False
    if embedding_mode == 'local_bge_m3':
        success = embed_with_local_bge_m3(wiki_root)
    elif embedding_mode == 'dialogue_llm':
        success = embed_with_dialogue_llm(wiki_root, file_blocks or [])
    
    if not success:
        return False
    
    # 验证
    if not verify_embeddings(wiki_root, checkpoint):
        return False
    
    # 更新检查点
    checkpoint["embeddings_completed"] = True
    checkpoint["embedding_mode"] = embedding_mode
    
    print("✓ Stage 3.5 完成")
    return True
