# ✅ Ingest Pipeline 优化 - 正确编号版（x.y 两段式）

## 🎯 关键修正

**编号规则**：仅允许 **x.y** 两段式格式，**后续所有编号顺延**（不使用 x.y.z）

---

## 📋 4 个新增 Stage（x.y 格式）

### Stage 2.3 · Incremental Association Detection ⭐ 高优先
- **文件**：`_stage_2_3_incremental.py`
- **作用**：检测新源 concept/entity 与 wiki 已有页面的关联
- **跳过**：wiki 为空时自动跳过
- **原 2.3 → 现 2.4**（后续顺延）

### Stage 2.5 · Concept Dedup & Merge ⭐ 高优先
- **文件**：`_stage_2_5_dedup.py`
- **作用**：同一本书内的概念去重合并
- **跳过**：单 chunk 书
- **原 2.4 → 现 2.6，原 2.5 → 现 2.7**（后续顺延）

### Stage 2.8 · Cross-source Query Resolution ⭐ 高优先
- **文件**：`_stage_2_8_query_resolve.py`
- **作用**：自动关闭已有答案的 query
- **跳过**：Stage 2.7 无 query 或 wiki 为空
- **原 2.6 → 现 2.9**（后续顺延）

### Stage 3.5 · Quality Scoring Card ⭐ 中优先
- **文件**：`_stage_3_5_quality.py`
- **作用**：量化 ingest 质量评分（0.0-1.0）
- **跳过**：无（总是执行）
- **原 3.5(Embeddings) → 现 3.6**（后续顺延）

---

## 📊 完整编号映射

```
原有流程：
  2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.1, 3.2, 3.3, 3.4, [3.5], 4.1

新增流程：
  2.1, 2.2, 
  [2.3 新], 2.4(原2.3), 
  [2.5 新], 2.6(原2.4), 2.7(原2.5), 
  [2.8 新], 2.9(原2.6), 
  3.1-3.4(不变), 
  [3.5 新], 3.6(原3.5), 
  4.1(不变)
```

| 新 | 名称 | 原 | 类型 |
|----|------|----|----|
| 2.3 | Incremental Association | - | 新增 |
| 2.4 | Source/Concept/Entity Generation | 2.3 | 顺延 |
| 2.5 | Concept Dedup & Merge | - | 新增 |
| 2.6 | Source Page | 2.4 | 顺延 |
| 2.7 | Query Auto-Generation | 2.5 | 顺延 |
| 2.8 | Cross-source Query Resolution | - | 新增 |
| 2.9 | Comparison Auto-Generation | 2.6 | 顺延 |
| 3.5 | Quality Scoring Card | - | 新增 |
| 3.6 | Embeddings | [3.5] | 顺延 |

---

## 📁 已创建的文件

✅ **模块文件**：
- `_stage_2_3_incremental.py` (增量关联检测)
- `_stage_2_5_dedup.py` (概念去重合并)
- `_stage_2_8_query_resolve.py` (跨源查询解析)
- `_stage_3_5_quality.py` (质量评分卡)

⏳ **需要**：
- 在 ingest.py 中集成这 4 个 stage
- 更新 ingest-stages-mandatory.md 的完整文档（使用新编号）
- 编写 4 个验证函数

---

## 📈 优化效果预期

- **概念去重** (Stage 2.5)：减少 15-25% 重复页面
- **增量学习** (Stage 2.3)：减少 10-20% 孤儿页面  
- **跨源查询** (Stage 2.8)：减少 20-30% 冗余 query
- **质量评分** (Stage 3.5)：快速识别 <0.65 分 ingest

---

## 🔧 后续工作

1. **ingest.py 集成**（1-2 天）
   - Stage 2.3：检测关联
   - Stage 2.5：去重规则
   - Stage 2.8：查询解析
   - Stage 3.5：质量评分

2. **验证和测试**（1-2 天）
   - 单 chunk 书
   - 多 chunk 书
   - 已有 wiki 场景
   - 质量评分输出

---

**状态**：✅ 设计+代码+正确编号完成，⏳ 等待 ingest.py 集成
