# Multimodal VLM pitfalls — MiniMax M3 + mmx CLI 实测踩坑

2026-06-11 第一次 HardwareWiki 电源篇图 captioning 时摸出来的具体非平凡经验。每个 pitfall 都有验证记录。

> **round ii 澄清 (2026-06-20，2026-06-23 更新)**：本技能的视觉任务分工是——**图片 caption（Stage 1.3）走 MiniMax VLM**（`anthropic/v1/messages` 多图批量，需 `MINIMAX_CN_API_KEY`）；**OCR / 文档 VLM 解析（Phase 0）走本地 minerU**（免费，API server 默认 **hybrid-engine** 后端——`/file_parse` 默认 backend=hybrid-engine、parse_method=auto，文字版自动 txt、扫描版自动 VLM OCR；不再用 `-b vlm-engine` CLI）。本文件历史上有几处把 MiniMax 的多图批量叫做"OCR"——那是早期对"figure 图片描述"的口语叫法，实际都是 **caption**。真正的文本 OCR 自始至终是本地 minerU。读下方 endpoint 矩阵 / 决策树时，把"OCR/caption"里的 OCR 部分理解为 caption 即可。

---

## Pitfall 1: macOS M3 跑 Qwen2-VL 缺 3 个包

**症状**：装好 `transformers` 后报 `ImportError: AutoVideoProcessor requires the Torchvision library` / `ValueError: Using a device_map requires accelerate`。

**必须装的 3 个包**（按顺序）：
```bash
~/.venv/bin/pip install --quiet torch torchvision transformers accelerate
```

**注意**：
- **必须** `torchvision`（不是可选依赖）—— `transformers` 自动检测时硬要求
- **必须** `accelerate` —— `device_map="mps"` 需要它
- torch 装完后 mps 可用：`torch.backends.mps.is_available() == True`（macOS 13+ / M1+）

**lesson**：本地跑 Qwen2-VL 系列 VLM 前，先 `pip install torch torchvision transformers accelerate` 4 件套。

---

## Pitfall 2: Anthropic messages API 单请求可传多图（**省 60-75% 时间**）

> **重要前置（2026-06-11 修正）**：这个 batching 优化**仅适用于直接调 MiniMax M3 的 Anthropic 协议 endpoint**（`requests.post(anthropic/v1/messages, ...)`）。**`mmx vision describe --image X` CLI 是 sequential 单图调用**——它不批量。312 页在 mmx CLI 下就是 312 次顺序调用（≈24 sec/张 = 125 min），不是 39 次 batched HTTP 调用。详见 `references/session-lessons.md` §14 的完整决策矩阵。
>
> 当用户指定走 mmx CLI（MiniMax CN 国内端，参考 §15 §16）时，本节优化**不适用**——按 mmx 的实际 ~24 sec/页线性扩展。

**症状**：单图单请求跑 738 张图，估算 90 分钟。

**修法**：单请求多图，**1 次调用传 8 张图**：
```python
content = [{"type": "text", "text": "8 张图按序描述..."}]
for i, img in enumerate(images):
    img_b64 = base64.standard_b64encode(open(img).read()).decode()
    content.append({
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}
    })
    content.append({"type": "text", "text": f"[/图{i+1}]"})

payload = {
    "model": "MiniMax-M3",
    "max_tokens": 2048,  # 8 张图都给 256 token
    "system": "你是...输出 JSON 数组...",
    "messages": [{"role": "user", "content": content}]
}
```

**实测 (2026-06-11, 电源篇 738 张)**：
- 单图版：8 张图 × 7.5 秒 = 60 秒
- 8 图批量版：22.6 秒 = **省 62%**
- 实际跑：92 batches × ~14-22 秒 = 21-35 分钟
- 节省 token：system prompt 共享 + 单图视觉 token 重复

**优化要点**：
- 强制 LLM 输出**结构化 JSON 数组**（`[{"idx": 1, "caption": "..."}, ...]`），不要让 LLM 自由发挥
- `max_tokens` 调到 `batch_size × 256`（每张图预留 256 token）
- prompt 末尾明确"8 个对象都要有，idx 与图顺序一致"

**lesson**：批量多模态任务，**永远先问"API 是否支持单请求多图"**。Anthropic / OpenAI / 主流 VLM API 都支持。1 个请求 vs N 个请求的 token 差 + 延迟差是数量级的。

**但**：当你**已经被用户的 backend choice 锁定**（例如 MiniMax CN 国内端 → mmx CLI），本节优化不可用。**先确认用户选定的 backend**，再决定走哪个 batching 路径。

---

## Pitfall 3: Anthropic Message Batches API（50% 折扣，24h 异步）

**症状**：738 张图按 8 张/批还要 92 次 API 调用，**单次成本高**。

**修法**：用 Anthropic Message Batches API：
- 单批最多 10,000 请求
- 成本减半（50% 折扣）
- 24 小时内异步返回

**用法（参考）**：
```python
# 提交 batch
batch_resp = requests.post(
    f"{BASE}/v1/messages/batches",
    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
    json={"requests": [
        {"custom_id": f"img-{i}", "params": {<标准 messages 参数>}}
        for i in range(92)
    ]}
)
batch_id = batch_resp.json()["id"]

# 24h 内轮询
while True:
    status = requests.get(f"{BASE}/v1/messages/batches/{batch_id}", headers={...}).json()
    if status["processing_status"] == "ended":
        # 下载结果
        results_url = status["results_url"]
        ...
        break
    time.sleep(60)
```

**适用场景**：可以等 24 小时的非实时任务（夜间 ingest、批量归档、数据准备）。

**lesson**：超大批量（>100 请求）+ 不要求实时 → 优先用 Batches API 拿 50% 折扣。适合夜间跑或 cron 跑。

---

## Pitfall 4: MiniMax embo-01 embedding 端点需要 `type=db` 或 `type=query` 字段

> **已废弃 (2026-06-19，2026-06-20 确认代码已清除)**：Embedding 不再走 MiniMax（见 SKILL.md「LLM execution model」）。`build_embeddings.py` 现在只有一个 OpenAI 兼容的 `/v1/embeddings` 路径（默认本地 Ollama `bge-m3`，可用 `EMBEDDING_*` 环境变量指向任何自选端点），MiniMax `embo-01` 路径已不存在。本节保留作历史记录。

**症状 (verified 2026-06-11, HardwareWiki Stage 6 build_embeddings)**：调 MiniMax embedding 端点 `https://api.minimaxi.com/v1/embeddings` 用 OpenAI 兼容格式（`{"model": "embo-01", "input": "test"}`）返回：
```json
{"vectors": null, "base_resp": {"status_code": 2013, "status_msg": "invalid params, binding: expr_path=type, cause=missing required parameter"}}
```

错误说 "missing required parameter: type"——OpenAI 格式里没这字段。

**修法**：用 MiniMax 自家格式（`texts` 复数 + 必传 `type`）：
```python
req = urllib.request.Request(
    "https://api.minimaxi.com/v1/embeddings",
    data=json.dumps({
        "model": "embo-01",
        "texts": ["hello world"],  # 注意是 texts（复数）不是 input
        "type": "db",  # 入库用 db，查询用 query
    }).encode("utf-8"),
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    method="POST",
)
# → {"vectors": [[0.012, -0.034, ...]], "base_resp": {...}}
# 向量维度: 1536
```

**关键字段对照**：
| 用途 | `type` 值 | `texts` 含义 |
|---|---|---|
| 文档入库 | `"db"` | 待嵌入的整段文本（可以长） |
| 实时查询 | `"query"` | 用户的查询词（通常短） |
| **不要传** | `"text"`/`"embedding"`/`"passage"` | （其他值会触发不同的 2013 错误） |

**lesson**：MiniMax 自家 API 端点跟 OpenAI 兼容但**字段名不同**（`texts` vs `input`），还**多了一个 `type` 必传字段**。第一次调必报错。**MiniMax 整体经验**：
- LLM 文本生成 → 由当前对话模型完成（conversation mode），不走 MiniMax
- Embedding → 不再走 MiniMax（可选，独立配置）
- TTS/视频/图像 → 也各有自己的 endpoint

不能假设"用 OpenAI/Anthropic 兼容"就行。

---

## Pitfall 5: minimax 国内端两条 endpoint 不要混用（2026-06-11 无源器件篇 OCR 教训）

**症状**：以为 minimax 只有 `v1/coding_plan/vlm`（mmx CLI 内部用）一条 VLM 入口，试了 `image_url=[img1, img2, ...]` 数组返回 2013 invalid_params。试 `anthropic/v1/messages` + `X-Api-Key` 返回 1004 "Please carry the API secret key in the X-Api-Key field"。几轮试错后才找到正解。

**minimax endpoint 矩阵（必读）**：

| endpoint | 多图支持 | auth header | 适用 |
|---|---|---|---|
| `https://api.minimaxi.com/anthropic/v1/messages` | ✅ 单请求 content blocks 数组（Anthropic 协议原生）| `Authorization: Bearer<key>` 或 `x-api-key: <key>`（均可用） | **caption 批量任务（首选；OCR/文档解析走本地 minerU，不用此 endpoint）** |
| `https://api.minimaxi.com/v1/coding_plan/vlm` | ❌ 只支持单图（`image_url` 必须是单字符串，不能传数组）| `Authorization: Bearer<key>` | mmx CLI 内部用 / 单图 sequential |

**常见错配 → 错误对照**：
- `v1/coding_plan/vlm` + `image_url=[img1, img2, ...]` 数组 → 2013 invalid_params ✅ verified 2026-06-17
- `v1/coding_plan/vlm` + `image` 字段 local path → 不接 local path

**正解**：
- **批量多图 OCR/caption**：`anthropic/v1/messages` + `Authorization: Bearer<key>` 或 `x-api-key: <key>`（两者均可用，2026-06-17 验证）+ `content=[{"type":"text","text":prompt}, {"type":"image","source":{"type":"base64","media_type":"image/png","data":b64}}, ...]`
- **单图 sequential**（mmx CLI）：`v1/coding_plan/vlm` + `Authorization: Bearer<key>` + `image_url="data:image/png;base64,..."` 单字符串

**实测 2026-06-11 无源器件篇**：
- 5 张图 OCR via `anthropic/v1/messages` + Bearer: 23.5 秒成功，input 10505 + output 2727 tokens，end_turn 自然结束
- 5 张图 OCR via `v1/coding_plan/vlm` + `image_url=[5 张]`: 2013 invalid_params

**lesson**：调 minimax 任何 endpoint 前**先看 endpoint matrix**。两个 endpoint 都用 Bearer 但多图支持完全不同，错误描述还很误导——别在试错上耗几轮。

---

## 综合 Pipeline 决策树

```
MiniMax M3 跑 caption?  (直接 HTTP，anthropic 协议；OCR/文档 VLM 解析走本地 minerU)
├─ < 50 张 → 单图单请求即可
├─ 50-1000 张 → 单请求 5-8 图批量（省 60%+ 时间，ingest.py `_caption_images()` 自动并行）
└─ > 1000 张 + 不急 → Anthropic Message Batches（50% 折扣）

mmx CLI 跑 caption?  (MiniMax CN 国内端，sequential；mmx = MiniMax，只做 caption)
├─ < 50 张 → mmx vision describe --image foo.jpg --prompt "..."（~24 sec/图，含 CLI 启动）
├─ 50-300 张 → mmx CLI 可容忍，但 20-120 min 总耗时
└─ > 300 张 → 不要用 mmx CLI，切到 HTTP batching（除非用户明确指定 mmx）

Embedding 跑向量入库?  (不再走 MiniMax；可选，独立配置)
├─ Wiki < 100 页 → 不需要（纯 keyword 搜索够用）
├─ Wiki 100-1000 页 → 用自选 embedding 端点（Stage 3.7 可选）
└─ Wiki > 1000 页 → 同上 + cross-encoder re-rank
```

完整 3 种方式对比见 `references/image-caption-strategy.md`。
mmx CLI 在 OCR 任务上的细节见 `references/session-lessons.md` §16。

## Hardcode 推荐

**Skill venv 装好 4 包**（Pitfall 1）：`torch torchvision transformers accelerate` —— 一次装好，永久受益。

**多模态脚本模板**：
1. MiniMax 批量用 Pitfall 2 的 8 图/请求 + JSON 输出 prompt（ingest.py `_caption_images()` 内置并行）
2. Batches API 用 Pitfall 3 的模板
3. Embedding 不再走 MiniMax（可选，独立配置；Pitfall 4 已废弃）

**mmx CLI 使用**（用户明确指定 mmx 时的 canonical 写法，参考 §16）：
```bash
mmx vision describe \
  --image foo.jpg \
  --prompt "请用中文描述这张图..." \
  --region cn \
  --output text \
  --timeout 180
# ~24 sec/图（CLI 启动 + 网络 + LLM）
```

---

## Pitfall 6: 历史 caption "解析失败" 大部分可通过重试修复（2026-06-17）

**症状**：1,515/18,709 (8.1%) caption 内容为 `（图N，解析失败）`。怀疑是灰度图 VLM 兼容性问题，但 A/B 对照测试（同图灰度 vs RGB）证明 MiniMax M3 对两种模式都能正常返回。

**A/B 对照验证**（2026-06-17，郑军奇 EMC 书 p119-fig2.jpeg，349×210，mode=L）：
- 灰度版 (mode=L): ✅ OK (6.2s) "ESD防护电路示意图，展示了音频接口金属外壳..."
- RGB版 (mode=L→RGB): ✅ OK (3.9s) "电路框图，展示音频接口ESD防护设计..."
- 结论：灰度图 NOT 被拒绝

**真正原因推测**：
1. 早期 MiniMax M3 版本 VLM 能力较弱（那批图是数月前 ingest）
2. 旧 `_caption_one_batch_mineru` 的 prompt 缺少页号/尺寸等上下文
3. 重试即可修复——今天重试的 6 张"解析失败"图 100% 成功

**分布**（HardwareWiki，1515 张可用 `_caption_images()` 重试）：
```
944  嵌入式系统设计 - 2013 - Marwedel   (62%)
 82  High Speed Signal Propagation
 80  模拟电子技术基础
 75  传感器原理及应用
 55  EMC电磁兼容设计与测试案例分析
 ...
```

**修复**：
- `_caption_images()` 缓存过滤现在检测并重试失败的 caption
- `_is_caption_failed()` 检测 "解析失败" / "无法识别" 等 VLM 错误模式
- `_preprocess_image_for_caption()` 做 RGB 归一化 + 超大图缩放（防御性，无害）

**lesson**：旧 ingest 的 caption 失败不一定有 bug——VLM 本身在进化，直接重试往往就通过了。

---

## 修订记录

- **2026-06-11**：初版，基于 738 张电源篇实测
- **2026-06-11**：删除 Pitfall 1+2（minerU 1.2B 专属）以及所有 minerU 备份内容，按用户指令"删 minerU 备份"；Pitfall 重新编号 1=缺包/2=8 张批量/3=Batches/4=embedding
- **2026-06-11**：Pitfall 2 加前置说明（mmx CLI 不适用 batching）；决策树 mmx 段加入 50-300 / >300 张阈值；canonical mmx invocation 加 `--region cn --output text --timeout 180` 三个 flag 解释
- **2026-06-11**：新增 Pitfall 5 minimax 国内端 endpoint 矩阵（`anthropic/v1/messages` 多图 vs `v1/coding_plan/vlm` 单图）——无源器件篇 OCR 实战沉淀，避免下次在两条 endpoint 之间再试错
- **2026-06-17**：新增 Pitfall 6（历史 caption 解析失败可通过重试修复）；全量 pitfall 审计：P4/emob-01 格式仍旧有效；P5/x-api-key 现已支持（过时声明修正）；P5/vlm 数组拒绝仍旧有效；P6 灰度声明修正（A/B 测试证明灰度可正常处理）