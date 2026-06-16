#!/usr/bin/env python3
"""repair_stage_37.py — 为缺源页的书生成 stub 源页

对 media 有内容但 wiki/sources/ 不存在的书，用 PyMuPDF 提取基本元数据生成 stub，
然后注入 Embedded Images。
"""
import os
import re
import sys
from pathlib import Path

import json

import fitz  # PyMuPDF

PROJECT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd()))
WIKI = PROJECT / "wiki"
SOURCES = WIKI / "sources"
MEDIA = WIKI / "media"
RAW = PROJECT / "raw"


def find_pdf(stem):
    """Search raw/ recursively for PDF matching the given stem."""
    patterns = [
        f"{stem}.pdf",
        f"{stem.replace(' - ', '_-_')}.pdf",
    ]
    # Also try partial match
    for pdf in RAW.rglob("*.pdf"):
        if pdf.stem == stem:
            return pdf
    # Fuzzy: match by first 40 chars + rest
    for pdf in RAW.rglob("*.pdf"):
        if stem[:40] in pdf.stem and pdf.suffix.lower() == ".pdf":
            return pdf
    return None


def generate_stub(media_dir, pdf_path, source_path):
    """Generate a minimal stub source page from PDF metadata."""
    stem = media_dir.name

    doc = fitz.open(pdf_path)
    page_count = len(doc)
    meta = doc.metadata or {}

    title = meta.get("title") or stem
    author = meta.get("author") or "未知"
    # Clean title
    if title.startswith("Microsoft Word") or title == "":
        title = stem

    # Try to get TOC for chapter outline
    toc = doc.get_toc() or []
    doc.close()

    # Count images in media dir
    img_count = 0
    cap_count = 0
    for f in media_dir.iterdir():
        if f.suffix.lower() in (".jpg", ".jpeg", ".png"):
            img_count += 1
            if (media_dir / (f.name + ".caption.txt")).exists():
                cap_count += 1

    # Build stub
    stub = f"# {title}\n\n"
    stub += f"> **作者**: {author}\n"
    stub += f"> **页数**: {page_count}\n"
    stub += f"> **状态**: 源页存根（Stage 3.7 fallback）\n"
    stub += f"> **图片**: {img_count} 张已提取, {cap_count} 张有 caption\n\n"

    if toc:
        stub += "## 目录\n\n"
        prev_level = 0
        for level, heading, page in toc[:80]:  # cap at 80 entries
            if level == 1:
                stub += f"\n### {heading} (第{page}页)\n\n"
            elif level == 2:
                stub += f"- {heading}\n"
            elif level == 3:
                stub += f"  - {heading}\n"
            prev_level = level
        if len(toc) > 80:
            stub += f"\n> （共 {len(toc)} 条目，仅显示前 80）\n"

    stub += "\n---\n"
    stub += "*此页面由 repair_stage_37.py 自动生成。*\n"
    stub += "*完整内容需通过 ingest pipeline 重新消化。*\n"

    # Write
    tmp = source_path.with_suffix(source_path.suffix + ".tmp")
    tmp.write_text(stub, encoding="utf-8")
    tmp.rename(source_path)
    print(f"[stub] {source_path.name}: {page_count}页, {img_count}图, {cap_count}caption")
    return {"pages": page_count, "images": img_count, "captions": cap_count}


# Now inject images using same logic as repair_stage_35.py
def inject_images(media_dir, source_path):
    """Append ## Embedded Images section. Same logic as repair_stage_35.py."""
    content = source_path.read_text(encoding="utf-8")

    manifest_path = media_dir / "_manifest.json"
    images_added = 0

    if manifest_path.exists():
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
            section += f"\n> 详细 manifest: `wiki/media/{media_dir.name}/_manifest.json`\n"
            content += section
            images_added = len(images)
    else:
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
            section += f"> Caption 由 MiniMax M3 生成。图片文件见 `wiki/media/{media_dir.name}/`\n"
            content += section
            images_added = len(image_files)

    if images_added > 0:
        tmp = source_path.with_suffix(source_path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.rename(source_path)
        print(f"  [inject] Added {images_added} images")
    return images_added


def main():
    created = 0
    for media_dir in sorted(MEDIA.rglob("*")):
        if not media_dir.is_dir() or media_dir.name.startswith("."):
            continue
        # Skip type-only dirs like book/, datasheet/ — only process leaf dirs with images
        if not any(f.suffix.lower() in (".jpeg",".png",".jpg") for f in media_dir.iterdir()):
            continue

        stem = media_dir.name
        source_path = SOURCES / f"{stem}.md"

        if source_path.exists():
            continue

        pdf_path = find_pdf(stem)
        if not pdf_path:
            print(f"[skip] No PDF found for '{stem}'")
            continue

        info = generate_stub(media_dir, pdf_path, source_path)
        inject_images(media_dir, source_path)
        created += 1

    print(f"\nDone. Created {created} stub source pages.")


if __name__ == "__main__":
    main()
