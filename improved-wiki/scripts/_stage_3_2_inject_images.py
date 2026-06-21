"""Stage 3.2: Image injection into the source page.

Extracted from ingest.py on 2026-06-21 for stage-module locality (was inline
in the orchestrator). Appends an '## Embedded Images' section to the source
page, reading from the unified _manifest.json (Path A PyMuPDF + Path B minerU)
with legacy _figures.json / cloud-OCR caption fallbacks.
"""
import json
import re
from pathlib import Path

from _core import Config
from _stage_1_extract import _stage_1_2_media_slug


def stage_3_2_inject_images(config: Config, raw_file: Path, source_path: Path,
                            method: str = "") -> dict:
    """Append '## Embedded Images' section to the source page.

    Two paths:
    - Text-layer PDFs: reads _manifest.json from wiki/media/<raw-subpath>/<slug>/
    - Scanned PDFs:   reads .caption.txt files from OCR output dir
    """
    content = source_path.read_text(encoding="utf-8")
    content = re.sub(r"## Embedded Images.*?(?=^## |\Z)", "", content, flags=re.MULTILINE | re.DOTALL)
    content = content.rstrip() + "\n\n"

    # Unified image injection: reads _manifest.json (the single source of truth
    # for both Path A PyMuPDF and Path B minerU).  Old ingests with full-page
    # renders are filtered via source != "page-render" for backward compat.
    slug = _stage_1_2_media_slug(raw_file, config)
    media_dir = config.wiki_dir / "media" / slug
    manifest_path = media_dir / "_manifest.json"

    # Also check legacy _figures.json (older minerU ingests before unification)
    figures_path = media_dir / "_figures.json"
    source_path_to_read = figures_path if figures_path.exists() else manifest_path

    if source_path_to_read.exists():
        m = json.loads(source_path_to_read.read_text(encoding="utf-8"))
        images = m.get("images", [])
        # Filter out legacy page-render entries (pre-2026-06-19 ingests)
        images = [i for i in images if i.get("source") != "page-render"]
        if images:
            is_mineru = any("mineru_" in i.get("filename", "") for i in images[:10])
            section = f"## Embedded Images\n\n"
            section += f"本书共抽出 {len(images)} 张{'图表' if is_mineru else '嵌入图'}。\n\n"
            section += "| 页号 | Caption | 文件 |\n|------|---------|------|\n"
            for img in sorted(images, key=lambda x: (x["page"], x.get("img_idx_in_page", 0))):
                cap_path = media_dir / (img["filename"] + ".caption.txt")
                cap = cap_path.read_text(encoding="utf-8").strip() if cap_path.exists() else "（无 caption）"
                if len(cap) > 80:
                    cap = cap[:80] + "..."
                section += f"| p{img['page']} | {cap} | `{img['path']}` |\n"
            section += f"\n> 图片由 {'minerU VLM' if is_mineru else 'PyMuPDF'} 提取，caption 由 {config.caption_model} 生成。详细 manifest 见 `wiki/media/{slug}/`\n"
            content += section
            tmp = source_path.with_suffix(source_path.suffix + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.rename(source_path)
            print(f"[stage 3.2] Injected {len(images)} images into {source_path.name}")
            return {"injected": len(images)}

    # Last resort: old cloud OCR caption files (pre-manifest era)
    images_in_media: list[tuple[str, str]] = []  # (filename, caption)
    if media_dir.exists():
        for f in sorted(media_dir.iterdir()):
            if f.suffix.lower() in (".jpg", ".jpeg", ".png"):
                cap_path = media_dir / (f.name + ".caption.txt")
                if cap_path.exists() and cap_path.stat().st_size >= 20:
                    images_in_media.append((f.name, cap_path.read_text(encoding="utf-8").strip()[:80]))

    # Also check old cloud OCR path
    ocr_dir = config.extract_tmp_dir / raw_file.stem
    if ocr_dir.exists():
        for cf in sorted(ocr_dir.glob("p*.caption.txt")):
            cap = cf.read_text(encoding="utf-8").strip()
            for line in cap.split("\n"):
                if line.strip():
                    pn_match = re.match(r'p(\d+)', cf.name)
                    pn = int(pn_match.group(1)) if pn_match else 0
                    images_in_media.append((f"p{pn} (cloud OCR)", line.strip()[:80]))

    if images_in_media:
        section = f"## Embedded Images\n\n"
        section += f"本书共提取 {len(images_in_media)} 张图表。\n\n"
        section += "| 文件/页码 | Caption |\n|------------|----------|\n"
        for name, cap in images_in_media[:200]:  # cap at 200 rows
            cap_short = cap[:80] + "..." if len(cap) > 80 else cap
            section += f"| `{name}` | {cap_short} |\n"
        if len(images_in_media) > 200:
            section += f"| ... | ({len(images_in_media) - 200} more) |\n"
        section += f"\n> Caption 由 {config.caption_model} 生成。图片文件见 `wiki/media/{slug}/`\n"
        content += section
        tmp = source_path.with_suffix(source_path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.rename(source_path)
        print(f"[stage 3.2] Injected {len(images_in_media)} images into {source_path.name}")
        return {"injected": len(images_in_media)}

    print("[stage 3.2] No images or figures to inject — skipping")
    return {"injected": 0}
