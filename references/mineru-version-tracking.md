# MinerU Version Tracking

## 当前状态（2026-06-24 起）

现行**唯一** OCR 路径：**hybrid-engine via local API server**（`mineru.cli.fast_api` 启动本地服务，管线调 `/file_parse` 接口）。`mineru -b pipeline` CLI 路径已于 2026-06-24 移除（3.4.0 的 pipeline CLI 存在 502 bug）。

## Current (2026-06-23)

| Component | Version | Notes |
|-----------|---------|-------|
| mineru (pip) | **3.4.0** | Released 2026-06-18. Upgraded from 3.3.1. |
| VLM model | `MinerU2.5-Pro-2605-1.2B` | Unchanged — still current. Last HF commit 2026-06-16 (README-only). |
| Pipeline model repo | `opendatalab/PDF-Extract-Kit-1.0` | 3.4.0 changelog: OCR upgraded to **PP-OCRv6** (~11% accuracy improvement on Omni). Last repo update 2026-06-15. |
| Config version | 1.3.2 | Auto-migrates from older `mineru.json`. |

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
