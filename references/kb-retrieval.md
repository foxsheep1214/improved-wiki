# 知识库检索工作流（消费侧）

> 适用场景：从 improved-wiki 管理的知识库项目中检索技术知识——诊断、根因分析、原理澄清、参数查询。
>
> 核心原则：**先搜后答** — 知识库有内容必须引用，知识库无内容必须明示。

## 1. 指定知识库项目

improved-wiki 管理多个独立的知识库项目。检索前先确定目标：

```bash
# 列出所有项目
ls ~/Documents/知识库/

# 每个项目有 wiki/ 子目录（markdown 页面）和 .llm-wiki/（运行时状态）
```

当前可用项目：`HardwareWiki`、`RadarWiki`、`自然科学知识库`（与 SKILL.md 一致）

## 2. 强制 4 步工作流

```
步骤 ①  关键词搜索
        方式 A: 用 search_wiki.py 脚本
            search_wiki.py "查询关键词" --project <项目路径>
        方式 B: 直接用文件内容搜索工具（如 Grep）
            Grep pattern=<关键词> path=<项目路径>/wiki/
        至少试 2-3 个同义词（中文 + 英文 + 别名）

步骤 ②  命中处理
        命中 ≥ 1 → 用文件读取工具（Read）读具体段（按行号定位）
        命中 = 0 → 进入步骤 ③

步骤 ③  知识库无内容（明示）
        明确标注："知识库无相关内容，使用 LLM 通用知识"
        不可含糊带过、不可凭印象编造引用

步骤 ④  输出格式（用户可观测）
        引用知识库:   ✓ [HardwareWiki:wiki/concepts/buck-converter.md §瞬态响应]
        引用 datasheet: ✓ [raw:TI LM5069 §7.5]
        引用 LLM 通用:  ✗ 知识库无，LLM 通用
```

## 3. 搜索方式

improved-wiki 使用 **hybrid 检索**：keyword（CJK bigram 加权，恒在线、免索引）+ 可选 vector（本地 Ollama bge-m3 + LanceDB）+ RRF(K=60) 融合。**未建索引也能搜**——只用 keyword 路径即可；建 LanceDB 索引只是为了启用 vector 增强：

```bash
build_embeddings.py --project <项目> embed     # 一次性建索引（仅为启用 vector 路径）
search_wiki.py "查询" --project <项目>          # hybrid；vector 不可用时报警并继续 keyword
search_wiki.py "查询" --project <项目> --keyword-only   # 显式只用 keyword，免 Ollama/索引
```

vector 路径的优势：即使查询词和页面用词不同，语义相近也能命中（如 "输出振荡" 匹配到 "PWM 调制方式"）。若 embedding provider/LanceDB 不可用，`search_wiki.py` 会把原因写到 stderr，并按 NashSU 行为在本次查询继续 keyword-only；`--keyword-only` 用于主动跳过 vector。这个搜索降级不适用于 ingest：Stage 3.7 仍是强制完整门禁。

## 4. 关键词搜索策略

### 4.1 中英文混搜

不要只用中文，也不要只用英文。至少 2-3 个同义词：

```
问题：buck 输出 LC 谐振
搜索词：
  - "ringing"  ← 英文
  - "overshoot"  ← 英文
  - "LC 谐振"  ← 中文
  - "damping"  ← 英文理论
```

### 4.2 理论词 vs 应用场景词

| 类别 | 应该用 | 避免用 |
|------|--------|--------|
| 理论方法 | ringing, damping, step response, ζ, resonance, Q factor | "电路出问题了" |
| 器件参数 | Vth, Rds(on), C_iss, SOA, OCP | "MOSFET 烧了" |
| 设计规范 | IPC-2221, creepage, derating | "板子怎么画" |
| 测试测量 | TDR, S-parameter, eye diagram | "测出来不对" |

## 5. 搜索工具使用

### 5.1 search_wiki.py — hybrid 检索（keyword + vector + RRF）

```bash
# 人类可读输出（交互调试用）
python3 "$SKILL_DIR/scripts/search_wiki.py" "buck ringing" \
  --project ~/Documents/知识库/HardwareWiki

# Agent 调用：--json 输出 JSON 数组，便于解析
python3 "$SKILL_DIR/scripts/search_wiki.py" "LC谐振" \
  --project ~/Documents/知识库/HardwareWiki --json

# 更多结果
python3 "$SKILL_DIR/scripts/search_wiki.py" "LC谐振导致振铃" \
  --project ~/Documents/知识库/HardwareWiki --top 10 --json
```

`--json` 返回格式（每条结果）：
```json
[{"path": "concepts/buck-converter.md", "title": "Buck 变换器", "snippet": "...", "score": 0.032, "vector_score": 0.891, "title_match": true}]
```

`snippet` 取自正文（不含 frontmatter）。命中 dedup 留下的 `type: redirect` 跳转页时，
结果已替换为 `redirect:` 指向的正式页，并多一个 `redirected_from` 字段记录原跳转页；
同一正式页只出现一次。只在正文写"见 [[…]]"、没有 `redirect:` 字段的旧跳转页保持原样。

结果窗口的 15–30% 留给前 20 条结果的一跳链接邻居（NashSU 图扩展），这些条目带
`graph_related_to`（由哪些结果连过来）；关键词/向量都没命中的邻居，snippet 为
`Graph neighbor of …`，需要 Read 原文才知道内容。

**Agent 标准工作流**：
1. `search_wiki.py "query" --project <项目> --json` → 解析 JSON
2. 取前 N 条的 `path` → `Read <项目>/wiki/<path>` 读全文
3. 引用具体段落回答

**建 vector 索引（一次性，仅为启用 vector 路径；keyword 无需索引）**：
```bash
python3 "$SKILL_DIR/scripts/build_embeddings.py" --project ~/Documents/知识库/HardwareWiki embed
```
未建索引时 hybrid 会报警并自动返回 keyword 结果；也可用 `--keyword-only` 主动避免 vector 探测。

### 5.2 出处回查：evidence_lookup.py

wiki 页只在 frontmatter 记 `sources:`（页面级），一页可能合并多本书。要引用到章节/图表/
逐字数值时，用摄取时保存的出处账本回查：

```bash
python3 "$SKILL_DIR/scripts/evidence_lookup.py" --project <项目> \
  --page concepts/closed-loop-comet-calibration.md --grep "phase error"
```

输出该页各来源的 claim（带 `§2.5.2 and Figure 2.20` 这类锚点和置信度）、带编号的公式、
逐字表格（如器件规格表）。`--source <原文件名片段>` 按来源查；`--json` 供解析。
完整来源路径精确匹配；旧 basename 引用只能唯一解析，同名歧义列在
`ambiguous_sources`，不会合并不同来源的证据。

默认只返回当前原文件 hash、完成事件、`ingested` marker 的 `run_id` 一致的账本。
每条结果带 `source_hash`、`run_id`、`completed_at`、`evidence_status`。
旧版、未完成、已删除来源的证据由显式 `--history` 返回，并保留状态标记；
新增批次不会覆盖其他来源或之前批次的账本。没有 run_id 的旧版账本只能按历史查询，
不自动认定为当前已完成证据。

账本只覆盖该功能上线后 ingest 的来源，升级不会回填旧书。
`missing_sources` 表示所选查询范围内没有可用证据；`unavailable_sources`
说明被当前版本筛选排除的账本，不能把两者都解释成“从未消化”。

### 5.3 补充：Read 精读

搜索结果给出文件路径后，用文件读取工具（Read）按行号读具体段落：
```
Read wiki/concepts/buck-降压变换器.md offset=20 limit=50
```

## 6. 引用规范

### 6.1 来源标记格式

```
✓ [HardwareWiki:wiki/concepts/buck-converter.md L1403-1424 §4.1 瞬态响应]
✓ [RadarWiki:wiki/concepts/脉冲压缩.md §匹配滤波]
✓ [TI LM5069 datasheet §7.5 Electrical Characteristics]
✗ 知识库无相关内容，以下为 LLM 通用知识
```

### 6.2 强制规则

1. **必须标具体项目名、段/章节/行号**，不能只标文件名
2. **多来源时分别标注**，不要合并
3. **LLM 通用知识必须标 ✗**，不可冒充知识库引用
4. **跨项目引用**：标两个项目时用 `HardwareWiki:wiki/XX + RadarWiki:wiki/YY` 格式

## 7. 边界情况处理

### 7.1 知识库有但搜不到
- **可能原因**：关键词太具体 / 别名不同 / 文件在子目录
- **对策**：换同义词 / 列出 wiki/ 目录看实际标题 / 用 glob 找文件名

### 7.2 知识库有但内容过时
- **标记**："[知识库已存在但与最新标准/器件不符 — 标注日期]"
- **补充**：给出最新版本来源

### 7.3 知识库无但有类似内容
- **标记**："知识库无直接相关，[HardwareWiki:wiki/XX] 提到部分相关内容"
- **引用**：列出最接近的内容作为参考

## 8. 反模式（必避）

```
❌ 反模式 1: 直接凭 LLM 训练知识回答技术问题（核心反模式）
❌ 反模式 2: 用"应用场景"词（电路/示波器/PCB）而非"理论方法"词
❌ 反模式 3: 引用时不标具体项目名和段落（只标文件名）
❌ 反模式 4: 知识库无相关内容时含糊带过
❌ 反模式 5: 凭印象编造 wiki 文件路径（应该用 Read 验证）
❌ 反模式 6: 一次 grep 命中 0 就放弃（应该换关键词 2-3 次）
```

## 9. 快速检查清单

回复技术问题前，自检：

```
[ ] 1. 指定了正确的知识库项目（HardwareWiki / RadarWiki / ...）
[ ] 2. Grep 内容搜索 或 search_wiki.py 执行了吗（≥2 个同义词）
[ ] 3. 命中 ≥ 1 → 用 Read 读具体段 → 行号记录了吗
[ ] 4. 命中 = 0 → 明确标"知识库无"了吗
[ ] 5. 引用是否标了项目名 + 具体段（不是只标文件名）
[ ] 6. LLM 通用知识是否标了 ✗
[ ] 7. 输出格式用户能一眼区分（✓ vs ✗）
```

满足全部 7 条才发出。
