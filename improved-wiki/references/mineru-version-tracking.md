# MinerU Version Tracking

## 当前状态（2026-06-24 起）

现行**唯一** OCR 路径：**hybrid-engine via local API server**（`mineru.cli.fast_api` 启动本地服务，管线调 `/file_parse` 接口）。`mineru -b pipeline` CLI 路径已废弃移除——3.4.0 的 pipeline CLI 存在 502 bug。下文"Backend selection"及推荐命令段落为历史记录，仅供追溯。

## Current (2026-06-23)

| Component | Version | Notes |
|-----------|---------|-------|
| mineru (pip) | **3.4.0** | Released 2026-06-18. Upgraded from 3.3.1. |
| VLM model | `MinerU2.5-Pro-2605-1.2B` | Unchanged — still current. Last HF commit 2026-06-16 (README-only). |
| Pipeline model repo | `opendatalab/PDF-Extract-Kit-1.0` | 3.4.0 changelog: OCR upgraded to **PP-OCRv6** (~11% accuracy improvement on Omni). Last repo update 2026-06-15. |
| Config version | 1.3.2 | Auto-migrates from older `mineru.json`. |

## Backend selection (revisited 2026-06-23) ⚠️ 历史记录，已废弃

> ⚠️ 本节及下方推荐命令为历史记录。`mineru -b pipeline` CLI 已于 2026-06-24 移除（3.4.0 502 bug），现行唯一路径见文首"当前状态"。

Investigated CLI help for a working `mineru` invocation and confirmed the
distinction between the two backends:

| Backend | Flag | What it does | When to use |
|---------|------|--------------|-------------|
| VLM | `-b vlm-engine` / `-vlm-auto-engine` | Layout analysis **+ VLM OCR** in one pass | Scanned PDFs, image-heavy docs |
| Pipeline | `-b pipeline` | Layout analysis **without** VLM OCR | Text-based PDFs (faster, no VLM cost) |

Method flag (`-m`) controls text extraction independently of backend:

- `-m txt` — read the embedded text layer directly, **no OCR at all**
- `-m auto` — let minerU decide (OCR only when text layer is sparse)

**Optimal parameters for text-based PDFs** (confirmed from prior successful run):

```bash
mineru -b pipeline -m txt -l ch <input.pdf>
```

## 3.4.0 changelog highlights

- **Pipeline backend OCR**: PP-OCRv6 (~11% accuracy gain)
- **VLM model**: unchanged at 2605 (fixed multiple issues from 2604)
- Removed Japanese/Traditional Chinese/Latin from explicit OCR language selection (now auto-detected)
- Model download logic: auto-selects model source (HuggingFace vs ModelScope) based on network
- Cache hit priority: checks local cache before downloading

## Upgrade command

```bash
uv pip install --upgrade mineru --python ~/.venv/bin/python
```

## What does NOT need upgrade

- VLM model (`MinerU2.5-Pro-2605-1.2B`): already at latest. The `vlm-auto-engine` backend uses this.
- Pipeline models: only needed if using `-b pipeline` backend. If you only use `vlm-engine`/`vlm-auto-engine`, pipeline models are never downloaded.

## How to check versions

```bash
# MinerU CLI version
~/.venv/bin/mineru --version

# VLM model (cached locally)
ls ~/.cache/huggingface/hub/models--opendatalab--MinerU2.5-Pro-2605-1.2B/snapshots/

# Pipeline model repo (if pipeline backend used)
~/.venv/bin/python -c "
from huggingface_hub import HfApi
api = HfApi()
info = api.repo_info('opendatalab/PDF-Extract-Kit-1.0')
print(f'Last modified: {info.lastModified}')
"

# Check for newer PyPI release
~/.venv/bin/python -c "
import urllib.request, json
data = json.loads(urllib.request.urlopen('https://pypi.org/pypi/mineru/json').read())
print(f'Latest: {data[\"info\"][\"version\"]}')
"
```
