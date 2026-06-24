"""Stage 1.2 image extraction (PPTX/DOCX zipfile + minerU figure harvesting).

Extracted from _stage_1_extract.py on 2026-06-24. Owns the media-slug derivation,
manifest writing, office-format image extraction, minerU figure harvesting, and
the minimum-image-dimension noise filter.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

# Shared infrastructure
_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))
from _core import Config, file_sha256  # noqa: E402
from _paths import media_slug  # noqa: E402

# ══════════════════════════════════════════════════════════════════════════════
# Minimum image dimensions for mineru-extracted figures.
# ══════════════════════════════════════════════════════════════════════════════

# Images below this are treated as noise (1x1/2x2 artifacts, stray pixels) and
# dropped. Threshold is deliberately very conservative: tiny formula strips
# (29-70px tall) are valuable because MiniMax-M3 transcribes them to
# LaTeX/Unicode ~81% of the time, so we must NOT filter them out. See
# image-caption-strategy.md.
MINERU_IMG_MIN_WIDTH = int(os.environ.get("MINERU_IMG_MIN_WIDTH", "20"))
MINERU_IMG_MIN_HEIGHT = int(os.environ.get("MINERU_IMG_MIN_HEIGHT", "20"))


def _is_image_too_small(width: int, height: int) -> bool:
    """Check if image is too small to keep.

    Filters only true noise (stray 1x1/2x2 pixel artifacts). Does NOT filter
    formula strips — tiny formula images (29-70px tall) are valuable because
    MiniMax-M3 transcribes them to LaTeX/Unicode ~81% of the time. The
    threshold is intentionally very low (default 20px) to avoid throwing away
    recoverable formula content.
    """
    return width < MINERU_IMG_MIN_WIDTH or height < MINERU_IMG_MIN_HEIGHT


# ══════════════════════════════════════════════════════════════════════════════
# Path helpers — media_slug now lives in _paths.py (shared utility).
# ══════════════════════════════════════════════════════════════════════════════


def _stage_1_2_write_manifest(manifest_path: Path, source: str, raw_file: Path, images: list[dict]) -> None:
    """改进4：manifest 版本控制和提取配置记录。"""
    manifest = {
        "manifest_version": 2,  # 版本控制
        "extraction_time": time.strftime("%Y-%m-%d %H:%M:%S"),  # 时间戳
        "extraction_config": {"min_size": 100},  # 配置记录
        "source": source,
        "source_sha256": file_sha256(raw_file),
        "total_images": len(images),
        "images": images,
    }
    tmp = manifest_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(manifest_path)


def _stage_1_2_find_uncaptioned_images(media_dir: Path) -> list[dict]:
    """Find minerU images in wiki/media/<subpath>/ that need captions."""
    if not media_dir.exists():
        return []
    imgs = []
    for f in sorted(media_dir.iterdir()):
        if f.suffix.lower() in (".jpg", ".jpeg", ".png"):
            cap_path = media_dir / (f.name + ".caption.txt")
            if not cap_path.exists() or cap_path.stat().st_size < 20:
                imgs.append({"filename": f.name, "path": str(f)})
    return imgs


# ══════════════════════════════════════════════════════════════════════════════
# minerU figure harvesting
# ══════════════════════════════════════════════════════════════════════════════

def _stage_1_2_harvest_images(results: dict, page_offset: int, raw_file: Path,
                             config: Config, chunk_out: Path) -> list[dict]:
    """Harvest minerU-extracted figures from API response and save to wiki/media.

    minerU VLM extracts individual figures/charts/tables from within scanned pages.
    These are the ONLY images produced for scanned PDFs (no full-page renders).
    This function:
    1. Reads {basename: base64-data-uri} from results[*]["images"]
    2. Reads content_list from results[*]["content_list"] to map images → pages
    3. Saves each figure to wiki/media/<slug>/ as p{page:04d}-mineru_{n}.{ext}
    4. Returns metadata list for _figures.json

    Called immediately after a successful minerU API response for each chunk.
    """
    import base64 as _b64
    slug = media_slug(raw_file, config)
    media_dir = config.wiki_dir / "media" / slug
    media_dir.mkdir(parents=True, exist_ok=True)

    # Collect all images and content_list items across result entries
    all_images: dict[str, str] = {}        # basename → data URI
    all_content: list[dict] = []

    for _rk, rv in (results or {}).items():
        if not isinstance(rv, dict):
            continue
        imgs = rv.get("images")
        if isinstance(imgs, dict):
            all_images.update(imgs)
        cl = rv.get("content_list")
        if isinstance(cl, str):
            # minerU API returns content_list as a JSON string (via
            # get_infer_result which reads the file with fp.read()),
            # not a parsed list. Parse it so we can map images → pages.
            try:
                cl = json.loads(cl)
            except (json.JSONDecodeError, ValueError):
                pass
        if isinstance(cl, list):
            all_content.extend(cl)

    if not all_images:
        return []

    # Build page→[image_basename] mapping from content_list.
    # Each IMAGE block has: type="image", img_path="images/hash.png", page_idx=N
    page_figs: dict[int, list[str]] = {}
    for block in all_content:
        block_type = block.get("type", "")
        if block_type not in ("image", "chart"):
            continue
        img_path = block.get("img_path", "")
        if not img_path:
            continue
        img_basename = os.path.basename(img_path)
        page_idx = block.get("page_idx", 0)
        abs_page = page_offset + int(page_idx)
        page_figs.setdefault(abs_page, []).append(img_basename)

    # Build a basename→caption map from content_list so we can write
    # minerU's own image_caption as a sidecar (.caption.txt). This lets
    # Stage 1.3 skip re-captioning figures minerU already described.
    mineru_captions: dict[str, str] = {}
    for block in all_content:
        if block.get("type") not in ("image", "chart"):
            continue
        ip = block.get("img_path", "")
        if not ip:
            continue
        bn = os.path.basename(ip)
        caps = block.get("image_caption", [])
        if caps and caps[0].strip():
            mineru_captions[bn] = caps[0].strip()

    # If content_list mapping produced nothing, fall back: assign all images
    # to the chunk-start page so they aren't lost.
    if not page_figs and all_images:
        page_figs[page_offset] = list(all_images.keys())

    # Save images and build metadata
    saved: list[dict] = []
    img_counter: dict[int, int] = {}

    for page_num in sorted(page_figs):
        for img_name in page_figs[page_num]:
            b64_uri = all_images.get(img_name)
            if not b64_uri:
                continue
            # Parse data URI: "data:image/png;base64,XXXX"
            if "," in b64_uri:
                _header, data = b64_uri.split(",", 1)
            else:
                data = b64_uri

            # 改进5：用 MD5 ID 替代位置索引
            raw_bytes = _b64.b64decode(data)
            img_id = hashlib.md5(raw_bytes).hexdigest()[:8]

            # Determine extension
            if img_name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                ext = img_name.rsplit(".", 1)[-1].lower()
            else:
                ext = "png"
            filename = f"p{page_num:04d}-mineru_{img_id}.{ext}"
            out_path = media_dir / filename

            if not out_path.exists():
                try:
                    out_path.write_bytes(raw_bytes)
                except Exception:
                    continue

            # Get dimensions if possible; drop true noise (1x1/2x2 artifacts).
            # PIL open is wrapped (can fail on corrupt bytes), but the size
            # check is outside the try so a NameError in _is_image_too_small
            # surfaces instead of silently keeping every image.
            w, h = 0, 0
            pil_ok = False
            try:
                from PIL import Image
                im = Image.open(out_path)
                w, h = im.size
                im.close()
                pil_ok = True
            except Exception:
                pass  # PIL couldn't read; keep image with unknown dims
            if pil_ok and _is_image_too_small(w, h):
                out_path.unlink(missing_ok=True)
                continue

            saved.append({
                "filename": filename,
                "page": page_num,
                "path": str(out_path.relative_to(config.wiki_root)),
                "width": w, "height": h,
                "source": "mineru-extracted",
            })

            # Write minerU's own image_caption as sidecar so Stage 1.3
            # skips re-captioning figures minerU already described.
            mc = mineru_captions.get(img_name)
            if mc:
                cap_path = media_dir / (filename + ".caption.txt")
                if not cap_path.exists():
                    cap_path.write_text(mc, encoding="utf-8")

    if saved:
        # Persist to chunk_out so the per-chunk stats can reference them
        harvest_path = chunk_out / "_mineru_figures.json"
        harvest_path.write_text(json.dumps(saved, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        print(f"[mineru-figures] {len(saved)} extracted figures saved to {media_dir.name}")

    return saved


# ══════════════════════════════════════════════════════════════════════════════
# Office-format image extraction
# ══════════════════════════════════════════════════════════════════════════════

def _stage_1_2_extract_images_office(raw_file: Path, media_dir: Path, manifest_path: Path,
                                 min_size: int = 100) -> dict:
    """Extract embedded images from PPTX/DOCX via zipfile.

    NashSU parity: extractAndSaveSourceImages handles PPTX/DOCX/PDF.
    Uses Python stdlib zipfile — no external deps needed.
    """
    import zipfile as _zf
    import io as _io

    fmt = raw_file.suffix.lower().lstrip(".")
    print(f"[stage 1.2] Extracting embedded images from {fmt.upper()}...")

    # Image dir inside the ZIP
    media_prefix = "ppt/media/" if fmt == "pptx" else "word/media/"
    img_exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp", ".svg"}

    all_images: list[dict] = []
    seen_hashes: set[str] = set()

    try:
        with _zf.ZipFile(raw_file, "r") as zf:
            for name in zf.namelist():
                if not name.startswith(media_prefix):
                    continue
                ext = Path(name).suffix.lower()
                if ext not in img_exts:
                    continue

                data = zf.read(name)
                if len(data) < min_size:
                    continue

                # Dedup by SHA-256
                fhash = hashlib.sha256(data).hexdigest()
                if fhash in seen_hashes:
                    continue
                seen_hashes.add(fhash)

                # Determine page context if available (from slide/word numbering)
                # PPTX: ppt/slides/slideN.xml → N; DOCX: no direct page mapping
                page = 0
                rel_parts = name.split("/")
                # For PPTX, try to extract slide number from parent dir structure
                if fmt == "pptx":
                    # Images are in ppt/media/, referenced from ppt/slides/slideN.xml
                    # We can't easily map back without parsing XML, so use 0
                    pass

                filename = Path(name).name
                out_path = media_dir / filename
                # Avoid overwriting: append hash prefix if collision
                if out_path.exists():
                    stem, ext2 = out_path.stem, out_path.suffix
                    out_path = media_dir / f"{stem}_{fhash[:6]}{ext2}"

                out_path.write_bytes(data)

                all_images.append({
                    "filename": out_path.name,
                    "page": page,
                    "size": len(data),
                    "sha256": fhash,
                    "format": ext.lstrip("."),
                })

    except Exception as e:
        print(f"[stage 1.2] {fmt.upper()} image extraction failed: {e}")
        return {"count": 0, "error": str(e)}

    # Write manifest
    manifest_data = {
        "source": str(raw_file),
        "format": fmt,
        "total_images": len(all_images),
        "images": all_images,
    }
    manifest_path.write_text(json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[stage 1.2] {fmt.upper()}: {len(all_images)} images → {media_dir}")
    return {"count": len(all_images), "media_dir": str(media_dir),
            "manifest": str(manifest_path), "images": all_images}


def _stage_1_2_extract_from_mineru(out_dir: Path, config: Config, raw_file: Path) -> dict:
    """Extract images from minerU output (pipeline txt / vlm / auto backends).

    minerU writes images to <out_dir>/<stem>/<method>/images/ where <method>
    is txt (pipeline -m txt), vlm (vlm-engine), or auto. Also reads
    content_list.json to harvest minerU's own image_caption (the PDF figure
    caption) and sub_type (flowchart/curve/text_image) so downstream Stage 1.3
    can skip re-captioning figures minerU already described.
    """
    media_dir = config.wiki_dir / "media" / media_slug(raw_file, config)
    media_dir.mkdir(parents=True, exist_ok=True)

    # Locate image source dir across backends: txt (pipeline), vlm, auto.
    # minerU nests output as <out_dir>/<stem>/<method>/images/.
    stem = raw_file.stem
    img_source_dir = None
    for method in ("txt", "vlm", "auto"):
        cand = out_dir / stem / method / "images"
        if cand.exists():
            img_source_dir = cand
            break
    # Fallback: older flat layout <out_dir>/vlm/images or auto/images
    if img_source_dir is None:
        for method in ("vlm", "auto"):
            cand = out_dir / method / "images"
            if cand.exists():
                img_source_dir = cand
                break

    # Harvest minerU image_caption + sub_type from content_list.json.
    # Keyed by image basename so we can attach during copy.
    caption_map: dict[str, dict] = {}
    cl_files = sorted(out_dir.rglob("*content_list.json"))
    for clf in cl_files:
        try:
            blocks = json.loads(clf.read_text(encoding="utf-8"))
        except Exception:
            continue
        for b in blocks:
            if b.get("type") in ("image", "chart"):
                ip = b.get("img_path", "")
                bn = os.path.basename(ip)
                if bn:
                    caps = b.get("image_caption", [])
                    caption_map[bn] = {
                        "caption": caps[0] if caps else "",
                        "sub_type": b.get("sub_type", ""),
                        "page": b.get("page_idx", 0),
                    }
        if caption_map:
            break  # use first content_list that yields images

    images = []
    mineru_captioned = 0
    if img_source_dir:
        for img_path in sorted(img_source_dir.glob("*")):
            if not img_path.is_file():
                continue
            dest = media_dir / img_path.name
            shutil.copy2(img_path, dest)
            meta = caption_map.get(img_path.name, {})
            # Write minerU caption as sidecar so Stage 1.3 skips re-captioning.
            # Combine image_caption (figure label) + content (Mermaid for flowcharts).
            cap_parts = []
            if meta.get("caption"):
                cap_parts.append(meta["caption"])
            if meta.get("content"):
                cap_parts.append(meta["content"])
            if cap_parts:
                sidecar = media_dir / (img_path.name + ".caption.txt")
                sidecar.write_text("\n".join(cap_parts), encoding="utf-8")
                mineru_captioned += 1
            images.append({
                "filename": img_path.name,
                "path": str(dest.relative_to(config.wiki_root)),
                "page": meta.get("page", 0),
                "caption": meta.get("caption", ""),
                "sub_type": meta.get("sub_type", ""),
                "width": 0,
                "height": 0,
            })
    else:
        # BUGFIX 2026-06-24: on OCR cache-resume the minerU API output dir is
        # not persisted (img_source_dir is None), so the original code wrote an
        # EMPTY manifest, wiping all figures. But OCR already saved figures to
        # media_dir as p*-mineru_*.* during chunk processing — recover them so
        # the manifest reflects reality and Stage 1.3 can caption the rest.
        for img_path in sorted(media_dir.glob("p*-mineru_*.*")):
            if not img_path.is_file() or img_path.name.endswith(".caption.txt"):
                continue
            bn = img_path.name
            try:
                page = int(bn[1:bn.index("-")])
            except (ValueError, IndexError):
                page = 0
            meta = caption_map.get(bn, {})
            if (media_dir / (bn + ".caption.txt")).exists():
                mineru_captioned += 1
            images.append({
                "filename": bn,
                "path": str(img_path.relative_to(config.wiki_root)),
                "page": page,
                "caption": meta.get("caption", ""),
                "sub_type": meta.get("sub_type", ""),
                "width": 0,
                "height": 0,
            })

    manifest_path = media_dir / "_manifest.json"
    _stage_1_2_write_manifest(manifest_path, "mineru-ocr", raw_file, images)
    print(f"[stage 1.2] minerU: {len(images)} images from {img_source_dir} "
          f"({mineru_captioned} pre-captioned by minerU"
          f"{', Stage 1.3 will skip pre-captioned' if mineru_captioned == len(images) and images else ''})")
    return {
        "count": len(images),
        "media_dir": str(media_dir),
        "manifest": str(manifest_path),
        "images": images,
        "mineru": True,
    }


def stage_1_2_extract_images(raw_file: Path, config: Config, min_size: int = 100) -> dict:
    """Extract embedded images from PPTX / DOCX via their internal zipfile media/ directory
    (NashSU parity: extractAndSaveSourceImages).

    PDF images are extracted separately by _stage_1_2_extract_from_mineru(), since all PDF
    text extraction routes through minerU (pipeline or VLM), which extracts images as part
    of the same pass.

    Returns: {"count": int, "media_dir": str, "manifest": str, "images": list}
    """
    slug = media_slug(raw_file, config)
    media_dir = config.wiki_dir / "media" / slug

    # Skip if already done
    manifest_path = media_dir / "_manifest.json"
    if manifest_path.exists():
        try:
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
            print(f"[stage 1.2] (cached) {m.get('total_images', 0)} images in {media_dir}")
            return {"count": m.get("total_images", 0), "cached": True, "media_dir": str(media_dir),
                    "manifest": str(manifest_path), "images": m.get("images", [])}
        except Exception:
            pass  # corrupt manifest, re-extract

    media_dir.mkdir(parents=True, exist_ok=True)
    return _stage_1_2_extract_images_office(raw_file, media_dir, manifest_path, min_size)
