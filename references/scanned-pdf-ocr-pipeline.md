# PDF Text+Image Extraction Pipeline (minerU Local API Server, 2026-06-23)

适用于**所有 PDF**——文本层/扫描版/混合型，2026-06-23 起统一走这条 pipeline，不再有"纯文本走 PyMuPDF 直抽、只有扫描版才上 OCR"的两分路。由 `_stage_1_extract.py`（facade）的 `stage_1_1_extract_text()` 路由判定类型后调用，实际执行函数 `_stage_1_1_extract_text_scanned()` 及下方所有 `_stage_1_1_scanned_*` / 锁 / manifest 辅助函数现居 `scripts/_stage_1_1_scanned.py`（2026-06-24 从 `_stage_1_extract.py` 拆出；函数名是历史遗留——现在文本版/混合版也会调它）。

## 何时使用这条 pipeline

- 任意类型的 PDF（text/scanned/mixed 不再分流）：fitz 采样（`_stage_1_1_sample_pdf`，见 `ingest-stages-mandatory.md` Stage 1.1）只做 **garbled 字体检测**，统一交给这条 pipeline，hybrid-engine/auto 内部按页判 txt vs VLM OCR。method 标签：
  - 正常 → `mineru-api`（auto：有文字层走 txt、无文字层走 VLM OCR）
  - garbled 字体 PDF（文字层是乱码）→ `mineru-api-ocr`（强制 `parse_method=ocr`，避免 auto 读乱码层）
  - 提取 <2000 字符 → 上述标签加 `-low-quality` 后缀
- 不适用：`.txt`/`.md`（直接读文件）、`.pptx`/`.docx`（zipfile/XML 解析，完全不碰 minerU）。
- **已废弃的旧说法**："纯 text-layer PDF 直接 PyMuPDF `get_text()`，毫秒级" ——2026-06-23 起这条捷径不存在了，所有 PDF 文本提取都要起一次本地 minerU。

## 完整流程

```
1. PyMuPDF 预检（仍然在用，5 秒级，仅做类型判断不做提取）
   └─ 采样判断 text/scanned/mixed（详见 ingest-stages-mandatory.md 信号①②③④）
2. 起一个持久化本地 minerU API 服务器（mineru.cli.fast_api，端口 MINERU_API_PORT 默认 19999）
   ├─ 系统级文件锁 _stage_1_1_acquire_mineru_lock()（fcntl.flock，超时 3600s）确保只有 1 本书在跑
   └─ 1 页 warmup，避免冷启动延迟计入第一个 chunk
3. PyMuPDF 渲染页面 + 切分 chunks（50 页/chunk，MINERU_CHUNK_SIZE）
4. 每个 chunk POST 本地 /file_parse（multipart，return_images + return_content_list=true）
   ├─ text 类型走 hybrid-engine 自动 txt method（无重 OCR）
   └─ scanned/mixed 走 hybrid-engine 自动 VLM OCR（`/file_parse` 默认 backend=hybrid-engine、parse_method=auto；可 per-request 改 backend，但 hybrid-engine 已验证最优）
5. 响应里直接带 base64 图片 + content_list（页码映射）→ _stage_1_2_harvest_images() 立即存图到 wiki/media/
6. md_content 写入每页 p<NNN>.txt
7. 全书跑完 → _stage_1_1_scanned_assemble_manifest() 写图片 manifest.json，并直接调 Stage 1.3 caption（见下方"输出物"）
8. 重试：每个 chunk 最多 3 次（含服务器重启），单次 HTTP 请求超时 1200s
9. 异常保护：>30% chunks 失败 → 全本 abort（RuntimeError）
```

## 关键技术决策

### 为什么用本地 minerU 而不是云端 API

- **零成本**：本地跑，无 API 调用费。
- **自动提取图片**：响应自带 base64 图 + content_list，不用额外一轮提取。
- **无需 API key**（caption 阶段才需要 MiniMax key）。
- **隐私**：数据本地处理。

**权衡**：模型常驻内存，系统级只允许 1 本书同时跑（见下）。

### 并发限制：文件锁，不是进程计数（2026-06-23 改）

旧版用 `MINERU_MAX_CONCURRENT` + `_wait_for_mineru_slot()` 轮询进程数（pgrep），在多会话/cron 并发下不可靠（注释原话："pgrep-based counting is unreliable under concurrent stress"）。现在用：

```python
# _stage_1_1_scanned.py
def _stage_1_1_acquire_mineru_lock(timeout: int = 3600) -> int:
    """fcntl.flock 独占锁，原子、跨进程安全。阻塞直到拿到锁或超时。"""
    ...

def _stage_1_1_release_mineru_lock(fd: int) -> None:
    ...
```

等待时打印 `[mineru] Waiting for lock... (Xs elapsed)`（按经过的分钟数打印一次，不是固定轮询间隔）。

> 仍然是严格串行——不要手动绕过这个锁。

### Chunk 大小：50 页

`MINERU_CHUNK_SIZE = 50` 不变。现在是 HTTP 调用，`urlopen(..., timeout=1200)`（20 分钟/次请求），超过则该次尝试失败进入重试，不是旧版"单 chunk 30 分钟硬超时杀进程"的模型。

### 为什么现在用持久 API 服务器，而不是每 chunk 起一次 CLI 进程（2026-06-23 改，502 workaround）

旧版每个 chunk `subprocess.run(mineru_bin, ...)` 起一次新进程。现在改成：

- 启动时起一次 `mineru.cli.fast_api` 服务器（整本书复用，模型只加载一次）
- 每个 chunk 用 HTTP POST `/file_parse` 提交，不再起新进程
- **原因**：minerU 3.4.0 的 `mineru -b pipeline` CLI 有已知 502 Bad Gateway bug——它自己启动的内部 API 服务器会立刻关闭。持久本地服务器 + 直接调 `/file_parse` 绕开了这个 bug（见 commit `a79cd7d`）。pipeline CLI 路径已于 2026-06-24 移除，API path 是唯一提取后端。
- 进程隔离仍然有：服务器是独立子进程，请求失败时 `_stage_1_1_scanned_restart_server()` 重启它，不影响 ingest.py 主进程。

## 输出物（已与 Stage 1.2/1.3 融合，不是独立两步）

| 产物 | 位置 | 验证标准 |
|------|------|---------|
| 每页文字 | `p<NNN>.txt`（1:1 对应） | chars > 100 |
| 提取图片 | `wiki/media/<slug>/p<NNN>-mineru_<id>.<ext>`（chunk 处理时直接落盘，不是事后单独提取） | 图片数 ≥ 原文引用数 |
| 图片 caption | `wiki/media/<slug>/*.caption.txt`（`_stage_1_1_scanned_assemble_manifest()` 跑完直接调 caption，不等 ingest.py 里独立的 Stage 1.3） | 每图都有，长度 ≥20 字符 |
| 全本合并文字 | 由 `_stage_1_1_assemble_ocr_text()` 拼接所有 `p<NNN>.txt` 返回（没有独立的 `_full_text.txt` 文件） | chars > 2000 |
| 统计文件 | `_mineru_stats.json` | 用于崩溃恢复 |

## Chunk 的 retry 策略

`_stage_1_1_scanned_submit_chunk_with_retries()`（`_stage_1_1_scanned.py`）实现：

1. **3 次重试** per chunk（HTTP 请求级，不是进程级 stderr 匹配）
2. 失败时先 sleep 2s 再重试；HTTP 错误/连接异常会触发 `_stage_1_1_scanned_restart_server()` 重启本地服务器
3. **全本 abort 阈值**：>30% chunks 失败 → `RuntimeError`（不变）

## 崩溃恢复

`_mineru_stats.json` 记录每个 chunk 的状态（completed_chunks/failed_chunks/images）。中断后重新运行 `ingest.py` 会自动跳过已完成的 chunk，只重跑未完成的——这部分行为不变。

### Image harvesting gap (2026-06-24, FIXED)

API path 的 `_stage_1_2_harvest_images()` 曾不读 `content_list` 的 `image_caption`（`content_list` 是 JSON 字符串，`isinstance(cl, list)` 永远 False）→ 全量图倾倒 + minerU caption 被浪费。**已修**（`json.loads(cl)` + 写 sidecar）：528→340 图，VLM 调用 ↓70%，caption 覆盖 62%→98%。详见 `known-issues.md`。

## 相关的其他 pipeline

- **Stage 1.3 Caption**：对默认路径而言，caption 已经在这条 pipeline 内部跑完了（见上方"输出物"），不再是 ingest.py 里独立调度的一步。详见 `references/image-caption-strategy.md`。
- **PPTX/DOCX**：完全不走这条 pipeline，用 stdlib zipfile/XML 解析文字 + 图片。
- **混合型 PDF 判定**：见 `references/ingest-stages-mandatory.md` Stage 1.1 的第四信号"隐藏 OCR 层检测"。

## 修订记录

- **2026-06-11**：初版，源于 HardwareWiki 无源器件篇 312 页 OCR 全本实战（minimax 云端版）
- **2026-06-17**：重写为 minerU 本地 VLM 版，反映 `ingest.py` 实际实现。移除 minimax OCR 内容（已过时），补充并发控制、chunk retry、崩溃恢复、mixed PDF 判定。
- **2026-06-23**：重写为持久本地 API 服务器版——`mineru -b pipeline` CLI 因 502 bug 被弃用为默认路径；文本版 PDF 不再走 PyMuPDF 直抽，统一并入这条 pipeline；并发控制从进程计数轮询改为 `fcntl.flock` 文件锁；图片提取+caption 内嵌进 chunk 处理，不再是 ingest.py 独立调度的 Stage 1.2/1.3。
