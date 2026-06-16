#!/usr/bin/env python3
"""repair_stage_05.py — 对缺 media 目录的书补做 Stage 0.5（图片提取）

用 PyMuPDF get_images() 抽取嵌入图，sha256 去重，尺寸过滤，
写 _manifest.json 到 wiki/media/<stem>/。
"""
import hashlib
import json
import os
import sys
from pathlib import Path

import fitz

PROJECT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd()))
RAW = PROJECT / "raw"
SOURCES = PROJECT / "wiki" / "sources"
MEDIA_ROOT = PROJECT / "wiki" / "media"

MIN_SIZE = 100  # px, both width and height must exceed this


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_book(pdf_path, media_dir, stem):
    """Extract all embedded images from a PDF into media_dir."""
    media_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(str(pdf_path))
    total_pages = len(doc)
    seen_hashes = {}  # sha256 -> first filename
    images = []
    fig_counter = {}  # page -> fig index within page

    for page_idx in range(total_pages):
        page = doc[page_idx]
        page_num = page_idx + 1  # 1-indexed for humans
        img_list = page.get_images(full=True)

        for img_idx, img_info in enumerate(img_list):
            xref = img_info[0]
            try:
                base = doc.extract_image(xref)
            except Exception:
                continue

            w, h = base["width"], base["height"]
            if w < MIN_SIZE and h < MIN_SIZE:
                continue

            img_bytes = base["image"]
            img_hash = sha256_hex(img_bytes)
            ext = base["ext"]

            if img_hash in seen_hashes:
                # Already extracted from another page
                existing = seen_hashes[img_hash]
                images.append(dict(
                    page=page_num, img_idx_in_page=img_idx,
                    filename=existing["filename"],
                    path=f"media/{stem}/{existing['filename']}",
                    width=w, height=h, sha256=img_hash,
                    dedup_of=existing["page"],
                ))
                continue

            # Determine figure number within this page
            fig_counter.setdefault(page_num, 0)
            fig_counter[page_num] += 1
            fig_num = fig_counter[page_num]

            fname = f"p{page_num}-fig{fig_num}.{ext}"
            fpath = media_dir / fname
            fpath.write_bytes(img_bytes)

            seen_hashes[img_hash] = dict(filename=fname, page=page_num)
            images.append(dict(
                page=page_num, img_idx_in_page=img_idx,
                filename=fname,
                path=f"media/{stem}/{fname}",
                width=w, height=h, sha256=img_hash,
            ))

    doc.close()

    # Write manifest
    manifest = dict(
        source=str(pdf_path.relative_to(PROJECT)),
        source_sha256=sha256_hex(pdf_path.read_bytes()),
        total_images=len(images),
        total_unique=len(seen_hashes),
        pages=total_pages,
        images=images,
    )
    (media_dir / "_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return len(images), len(seen_hashes)


def main():
    # Find source pages without Embedded Images
    targets = []
    for src in sorted(SOURCES.rglob("*.md")):
        content = src.read_text(encoding="utf-8")
        if "## Embedded Images" not in content:
            stem = src.stem
            if not (MEDIA_ROOT / stem).exists():
                # Find matching PDF
                for pdf in RAW.rglob("*.pdf"):
                    if pdf.stem == stem:
                        targets.append((stem, pdf, src))
                        break

    if not targets:
        print("All books have media directories already.")
        return

    print(f"Found {len(targets)} books needing Stage 0.5\n")

    total_imgs = 0
    for stem, pdf_path, source_path in targets:
        media_dir = MEDIA_ROOT / stem
        img_count, unique = extract_book(pdf_path, media_dir, stem)
        total_imgs += img_count
        print(f"  [{img_count:4d} imgs, {unique:4d} unique] {stem[:60]}")

    print(f"\nTotal extracted: {total_imgs} images across {len(targets)} books")


if __name__ == "__main__":
    main()
