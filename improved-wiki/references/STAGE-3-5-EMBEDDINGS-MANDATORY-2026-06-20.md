# Stage 3.5 改进：从可选到强制的 Embedding（2026-06-20）

## 🎯 改进摘要

**之前**（过时）：
```
Phase 4: Embeddings (可选，需要外部 API)
└─ MiniMax / OpenAI embedding
└─ 成本高，隐私风险
```

**现在**（改进后）：
```
Phase 4: Embeddings (强制执行)
├─ 优先：本地 BGE-M3 (Ollama) ✓
├─ 降级：对话 LLM (Claude) ⚠️
└─ 弃用：外部 API (MiniMax/OpenAI) ✗
```

---

## 📋 改进内容

### 1. 从"可选"改为"强制"

**原因**：
- 本地 BGE-M3 免费、快速、隐私
- 大 wiki (>100 页) 必须有语义搜索
- 不应该有"部分 wiki 无法搜索"的状态

### 2. 优先本地模型

**优先级**：
```
第一选择：本地 BGE-M3 (Ollama)
  ✓ 零成本   ✓ 完全隐私   ✓ 离线可用   ✓ 快速
  
第二选择：对话 LLM (Claude)
  ⚠️ 消耗 token  ⚠️ 较慢  ⚠️ 网络依赖
  → 仅作为 BGE-M3 不可用时的降级方案

弃用：外部 API (MiniMax/OpenAI)
  ✗ 已移除支持
```

### 3. 自动检测和交互提示

```python
# 流程
检测 BGE-M3 可用？
  ├─ 是 → 使用本地 BGE-M3
  └─ 否 → 显示交互提示
        ├─ 用户选 A（安装）→ 引导安装步骤后退出
        └─ 用户选 B（LLM）→ 使用对话模式降级
```

---

## 🚀 快速开始

### 方案 A：本地 BGE-M3（推荐）

```bash
# 1. 一次性安装（5 分钟）
brew install ollama
ollama pull bge-m3

# 2. 启动服务（保持运行）
ollama serve &

# 3. 运行 ingest（自动 embedding）
python3 scripts/ingest.py file.pdf --conversation
```

**优势**：
- 完全免费
- 速度快（本地运行）
- 完全隐私（无网络调用）
- 可离线使用
- 支持中文

### 方案 B：对话 LLM（降级）

```bash
# 直接运行（BGE-M3 不可用时）
python3 scripts/ingest.py file.pdf --conversation

# 系统检测到无 BGE-M3，提示用户
# 用户选择"B"，使用 Claude LLM 生成向量
```

**缺点**：
- 速度较慢
- 消耗 token
- 需要网络

---

## 📊 成本对比

| 方案 | 一次性安装 | 每次运行成本 | 速度 | 隐私 | 推荐 |
|------|----------|-----------|------|------|------|
| **BGE-M3** | 5 min | ¥0 | 快 | ✓ | ⭐ |
| LLM | 0 | 看 wiki 大小 | 慢 | ⚠️ | 备选 |
| OpenAI | 0 | $$ | 快 | ✗ | 不推荐 |
| MiniMax | 0 | ¥¥ | 快 | ⚠️ | 已弃用 |

---

## 📁 代码实现

**新文件**：`_stage_3_5_embeddings.py`

**核心函数**：
```python
def stage_3_5_embeddings(checkpoint, wiki_root, file_blocks) -> bool:
    """Stage 3.5: Embeddings（强制执行）"""
    
    # 1. 检测本地 BGE-M3
    if check_local_bge_m3():
        # 2a. 使用本地模型
        return embed_with_local_bge_m3(wiki_root)
    else:
        # 2b. 提示用户
        mode = ensure_embedding_available(checkpoint)
        if mode == 'local_bge_m3':
            return embed_with_local_bge_m3(wiki_root)
        else:
            return embed_with_dialogue_llm(wiki_root, file_blocks)
```

---

## 🔄 集成到 ingest.py

```python
# 在 ingest.py 的 Stage 3.4 之后
from _stage_3_5_embeddings import stage_3_5_embeddings

# ... Stage 3.4 完成后
if not stage_3_5_embeddings(checkpoint, wiki_root, file_blocks):
    print("❌ Stage 3.5 embedding 失败")
    return 1

# Stage 4.1 Final Validation
if not validate_ingest(wiki_root):
    print("❌ Stage 4.1 validation 失败")
    return 1
```

---

## ✅ 验证清单

```
- [ ] Stage 3.5：BGE-M3 或 LLM 方式之一可用
- [ ] Stage 3.5：LanceDB 目录已创建 (wiki/lancedb/)
- [ ] Stage 3.5：embeddings.json 存在且有向量数据
- [ ] Stage 3.5：向量数量 ≥ wiki 概念/实体页面数
- [ ] Stage 4.1：所有前置 stage 验证通过
```

---

## 🎓 影响

### Embedding 不再是"可选的"

| 维度 | 影响 |
|------|------|
| **用户体验** | ↑ 搜索功能完整（语义+关键词） |
| **设置复杂度** | → 第一次略增（安装 Ollama），后续无增加 |
| **性能** | ↑ 本地 BGE-M3 比 API 快 10 倍 |
| **成本** | ↓ 从 $$（API）到 ¥0（本地）|
| **隐私** | ↑ 完全本地，无数据泄露 |

### 对现有工作流的影响

```
✓ 新建 wiki：强制做 embedding，自动获得语义搜索
✓ 现有 wiki：可以逐步迁移到 BGE-M3（或保持 LLM 方式）
✓ 小 wiki (<100页)：也获得了语义搜索能力
⚠️  首次使用：需要 5 分钟安装 Ollama（一次性）
```

---

## 📝 文档更新

### ingest-stages-mandatory.md

已更新 Stage 3.5 部分：
- ✓ 改为"强制执行"
- ✓ 优先本地 BGE-M3
- ✓ 添加降级方案（LLM）
- ✓ 添加快速开始步骤
- ✓ 更新验证清单

---

## 🔮 未来展望

### 可能的进一步优化

1. **自动下载**：首次运行时自动下载 BGE-M3
2. **云备选**：支持云端 embedding 服务（作为二级降级）
3. **增量更新**：新增页面自动 embedding，无需重跑全部
4. **性能优化**：批量 embedding，使用 GPU 加速
5. **多语言**：支持多种语言的 embedding 模型

---

**状态**：✅ 改进完成  
**发布日期**：2026-06-20  
**向后兼容**：已有的 LLM embedding 仍可继续使用
