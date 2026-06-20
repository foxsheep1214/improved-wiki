# Scanned PDF OCR Pipeline (minerU Local VLM, 2026-06-17)

适用于**扫描版 PDF**（无文本层，PyMuPDF `get_text()` 全空）或**混合型 PDF**（有文本层但多页有大图，>30% 抽样页判定为 `mixed`）的本地 OCR pipeline。由 `ingest.py` Stage 1.2 自动执行。

## 何时使用这条 pipeline

- PDF 是**扫描版**（无 text layer）：`fitz.open(p).get_text()` 返回空或近乎空。
- PDF 是**混合型**（有 text layer 但图表密度高）：>30% 抽样页有全页大图 → 走 OCR 避免 Johnson 事故（文本达标但图表全丢）。
- PDF 内有中文/数学公式/硬件图等需要**像素级识别**的内容。
- 不适用：纯 text-layer PDF（直接 PyMuPDF `get_text()` 即可，毫秒级）。

## 完整流程

```
1. PyMuPDF 预检（5 秒）
   ├─ 全页 `get_text()` 字符数 → 判断 scanned/text/mixed
   ├─ mixed 判定：抽样页检测全页大图，>30% → 走 OCR
   └─ 纯 text PDF → 跳过 OCR，直接 PyMuPDF 提取
2. PyMuPDF 渲染所有页 PNG + 切分 chunks（50 页/chunk）
3. 每个 chunk 调用本地 minerU VLM：
   ~/.venv/bin/mineru -p <chunk>.pdf -o <chunk>_out \
     -b vlm-engine -l ch
4. 并发控制：_wait_for_mineru_slot() 确保 ≤2 个 minerU 实例同时运行
5. minerU 输出 Markdown（含文字 + 自动提取图片）→ 解析为 p<NNN>.txt
6. 重试：每个 chunk 最多 3 次 retry，自动 kill stale 进程
7. 超时：单 chunk 30 分钟
8. 异常保护：>30% chunks 失败 → 全本 abort
```

## 关键技术决策

### 为什么用本地 minerU 而不是云端 API

- **零成本**：无 API 调用费用，minerU VLM 在 Apple Silicon 上免费运行。
- **自动提取图片**：minerU `vlm-engine` 同时输出文字层和分离的图片文件，Stage 1.3 caption 直接用。
- **中文优化**：`-l ch` 对中文/日文扫描件识别质量高。
- **无需 API key**：不需要配置任何云端密钥。
- **隐私**：所有数据在本地处理。

**权衡**：VLM 实例内存开销大，串行执行避免内存竞争。`ingest.py` 通过 `_wait_for_mineru_slot()` 自动排队，等待时显示当前占用文件 + 累计等待时间。

### 并发限制：MINERU_MAX_CONCURRENT = 1

定义在 `ingest.py:792`：

```python
MINERU_MAX_CONCURRENT = 1  # 串行执行，避免 VLM 内存竞争

def _count_running_mineru() -> int:
    """每对 CLI+FastAPI = 1 个 OCR 任务，系统级计数"""
    alive = count_mineru_processes()
    return alive // 2  # 每个 job = 2 进程

def _wait_for_mineru_slot(poll_interval: int = 120):
    """阻塞直到有空槽位。等待时输出：
       - ⏳ 并发槽已满 (1/1)
       - 当前占用文件（如「图解传热学_chunk_0000-0050.pdf」）
       - 累计等待时间（X 秒 / X.X 分钟）
       - 下次重试间隔
       槽位释放时输出：✅ slot freed after X.X min — proceeding"""
    while _count_running_mineru() >= MINERU_MAX_CONCURRENT:
        print(f"[mineru] ⏳ 并发槽已满 (1/1)「当前文件」— 已等待 X 分钟，120s 后重试...")
        sleep(poll_interval)
```

> 串行执行确保单个 OCR 任务获得全部系统资源。不要手动提高此值。

### Chunk 大小：50 页

`MINERU_CHUNK_SIZE = 50`。单 chunk 耗时 5-30 分钟（取决于页复杂度）。更大的 chunk 会超过 30 分钟 timeout。

### 为什么用 minerU CLI 而不是 Python API

`subprocess.run(mineru_bin, ...)` 比 Python SDK 更稳定：
- 进程隔离：crash 不影响 ingest.py 主进程
- 超时控制：`timeout=1800` 直接杀进程
- 重试简单：stderr 匹配 retry patterns 即可

## Stage 1.2 输出物

| 产物 | 位置 | 验证标准 |
|------|------|---------|
| 每页文字 | `p<NNN>.txt`（1:1 对应） | chars > 100 |
| 提取图片 | `images/`（minerU 自动） | 图片数 ≥ 原文引用数 |
| 全本合并文字 | `_full_text.txt` | chars > 2000 |
| 统计文件 | `_mineru_stats.json` | 用于崩溃恢复 |

## Chunk 的 retry 策略

`ingest.py:952-994` 实现的 retry 逻辑：

1. **3 次重试** per chunk
2. **Retry patterns**（stderr 匹配）：
   - `onnection` / `failed` / `timeout` / `refused` — 网络/服务瞬断
   - `NoneType` / `get` — minerU 内部解析错误
   - `Task failed` / `RemoteDisconnected` / `ServerDisconnected` — FastAPI 端崩溃
   - `semaphore` / `resource_tracker` — minerU shutdown crash
   - `concurrency` / `limited` — minerU VLM concurrency limit (transient, retry-safe)
3. **重试间隔**：先 `_kill_mineru_servers()` + sleep 2s 清理残留进程
4. **Permanent failure**（不重试）：无效 PDF、文件缺失等非瞬态错误
5. **全本 abort 阈值**：>30% chunks 失败 → `RuntimeError`

## 崩溃恢复

`_mineru_stats.json` 记录每个 chunk 的状态（pending/success/failed）。中断后重新运行 `ingest.py` 会自动跳过已完成的 chunk，只重跑失败的。

## 相关的其他 pipeline

- **Stage 1.3 Caption**：minerU 提取的图片 → minimax `anthropic/v1/messages` 多图批量 caption（5 张/批）。详见 `references/image-caption-strategy.md`。
- **纯 text PDF**：跳过 Stage 1.2 全流程，PyMuPDF `get_text()` 直接提取 → Stage 2.1 开始。
- **混合型 PDF 判定**：见 `references/ingest-stages-mandatory.md` Stage 1.1 的第四信号"隐藏 OCR 层检测"。

## 修订记录

- **2026-06-11**：初版，源于 HardwareWiki 无源器件篇 312 页 OCR 全本实战（minimax 云端版）
- **2026-06-17**：重写为 minerU 本地 VLM 版，反映 `ingest.py` 实际实现。移除 minimax OCR 内容（已过时），补充并发控制、chunk retry、崩溃恢复、mixed PDF 判定。
