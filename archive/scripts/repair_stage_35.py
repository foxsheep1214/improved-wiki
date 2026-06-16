#!/usr/bin/env python3
"""repair_stage_35.py — 补做漏掉的 Stage 3.5（Embedded Images 注入源页）

扫描 wiki/media/*/ 所有子目录，对缺少 ## Embedded Images 段的源页补注入。
Path A（有 _manifest.json）用 manifest；Path B 扫描 loose image files。
"""
import json
import os
import re
import sys
from pathlib import Path

PROJECT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd()))
WIKI = PROJECT / "wiki"
SOURCES = WIKI / "sources"
MEDIA = WIKI / "media"

injected = 0
skipped = 0
errors = 0

for media_dir in sorted(MEDIA.iterdir()):
    if not media_dir.is_dir() or media_dir.name.startswith("."):
        continue

    stem = media_dir.name
    source_path = SOURCES / f"{stem}.md"

    if not source_path.exists():
        print(f"[skip] No source page: {stem}")
        skipped += 1
        continue

    content = source_path.read_text(encoding="utf-8")
    if "## Embedded Images" in content:
        print(f"[skip] Already has Embedded Images: {stem}")
        skipped += 1
        continue

    # Remove any stale partial section
    content = re.sub(r"## Embedded Images.*?(?=^## |\Z)", "", content, flags=re.MULTILINE | re.DOTALL)
    content = content.rstrip() + "\n\n"

    manifest_path = media_dir / "_manifest.json"
    images_added = 0

    if manifest_path.exists():
        # Path A: manifest-based (text-layer PDFs)
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
        images = m.get("images", [])
        if images:
            section = f"## Embedded Images\n\n"
            section += f"本书共抽出 {len(images)} 张嵌入图。\n\n"
            section += "| 页号 | Caption | 文件 |\n|------|---------|------|\n"
            for img in sorted(images, key=lambda x: (x["page"], x.get("img_idx_in_page", 0))):
                cap_path = media_dir / (img["filename"] + ".caption.txt")
                cap = cap_path.read_text(encoding="utf-8").strip() if cap_path.exists() else "（无 caption）"
                if len(cap) > 80:
                    cap = cap[:80] + "..."
                section += f"| p{img['page']} | {cap} | `{img['path']}` |\n"
            section += f"\n> 详细 manifest: `wiki/media/{stem}/_manifest.json`\n"
            content += section
            images_added = len(images)
    else:
        # Path B: loose files (minerU / scanned PDFs)
        image_files = []
        for f in sorted(media_dir.iterdir()):
            if f.suffix.lower() in (".jpg", ".jpeg", ".png"):
                cap_path = media_dir / (f.name + ".caption.txt")
                cap = cap_path.read_text(encoding="utf-8").strip() if cap_path.exists() else "（无 caption）"
                image_files.append((f.name, cap))

        if image_files:
            section = f"## Embedded Images\n\n"
            section += f"本书共提取 {len(image_files)} 张图表。\n\n"
            section += "| 文件 | Caption |\n|------|---------|\n"
            for name, cap in image_files[:200]:
                cap_short = cap[:80] + "..." if len(cap) > 80 else cap
                section += f"| `{name}` | {cap_short} |\n"
            if len(image_files) > 200:
                section += f"| ... | ({len(image_files) - 200} more) |\n"
            section += f"\n> Caption 由 MiniMax M3 生成。图片文件见 `wiki/media/{stem}/`\n"
            content += section
            images_added = len(image_files)

    if images_added > 0:
        tmp = source_path.with_suffix(source_path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.rename(source_path)
        print(f"[OK] {stem}: injected {images_added} images")
        injected += 1
    else:
        print(f"[warn] {stem}: no images found in media dir")
        errors += 1

print(f"\nDone. Injected: {injected}, Skipped: {skipped}, No-images: {errors}")
