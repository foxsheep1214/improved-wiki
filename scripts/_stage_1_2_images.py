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
# (29-70px tall) are valuable because the VLM transcribes them to
# LaTeX/Unicode ~81% of the time, so we must NOT filter them out. See
# image-caption-strategy.md.
MINERU_IMG_MIN_WIDTH = int(os.environ.get("MINERU_IMG_MIN_WIDTH", "20"))
MINERU_IMG_MIN_HEIGHT = int(os.environ.get("MINERU_IMG_MIN_HEIGHT", "20"))


def _stage_1_2_image_size(path: Path) -> tuple[int, int]:
    """Read (width, height) via PIL; (0, 0) if the file can't be opened
    (matches _stage_1_2_harvest_images()'s defensive "unknown dims" fallback
    rather than hardcoding 0,0 unconditionally — see bug 2026-07-06)."""
    try:
        from PIL import Image
        im = Image.open(path)
        w, h = im.size
        im.close()
        return w, h
    except Exception:
        return 0, 0


def _is_image_too_small(width: int, height: int) -> bool:
    """Check if image is too small to keep.

    Filters only true noise (stray 1x1/2x2 pixel artifacts). Does NOT filter
    formula strips — tiny formula images (29-70px tall) are valuable because
    the VLM transcribes them to LaTeX/Unicode ~81% of the time. The
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
                except Exception as e:
                    print(f"[mineru-figures] failed to save {filename}: {e} — skipped")
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

            # NOTE (2026-06-24): minerU's image_caption is NO LONGER written as
            # a .caption.txt sidecar. It is usually just the book's printed
            # figure label (e.g. "Figure 2.14 A backward-tilted antenna
            # geometry.") — using it as the final caption and skipping the VLM
            # produced lazy label-only captions (bug 2026-06-24). Stage 1.3
            # now VLM-captions EVERY image, with minerU's caption + surrounding
            # text passed as anchoring context (see _stage_1_3_caption.py).

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
                # For PPTX, try to extract slide number from parent dir structure
                if fmt == "pptx":
                    # Images are in ppt/media/, referenced from ppt/slides/slideN.xml
                    # We can't easily map back without parsing XML, so use 0
                    pass

                filename = Path(name).name
                out_path = media_dir / filename
                # Name collision (e.g. prior run): reuse the existing file when
                # its content is identical; only fork to a hash-suffixed name
                # when the bytes actually differ.
                if out_path.exists():
                    existing_hash = hashlib.sha256(out_path.read_bytes()).hexdigest()
                    if existing_hash != fhash:
                        stem, ext2 = out_path.stem, out_path.suffix
                        out_path = media_dir / f"{stem}_{fhash[:6]}{ext2}"

                if not out_path.exists():
                    out_path.write_bytes(data)

                all_images.append({
                    "filename": out_path.name,
                    "page": page,
                    "size": len(data),
                    "sha256": fhash,
                    "format": ext.lstrip("."),
                })

    except Exception as e:
        # No-silent-fallback: callers don't check an "error" key, so returning
        # an error dict was a silent degrade (0 images, pipeline continues).
        # Raise loudly instead — mirrors the office TEXT path in
        # _stage_1_extract.py.
        raise RuntimeError(f"Failed to extract images from {raw_file.name}: {e}")

    # Write manifest (atomic tmp+rename via the shared v2 writer)
    _stage_1_2_write_manifest(manifest_path, fmt, raw_file, all_images)
    print(f"[stage 1.2] {fmt.upper()}: {len(all_images)} images → {media_dir}")
    return {"count": len(all_images), "media_dir": str(media_dir),
            "manifest": str(manifest_path), "images": all_images}


def _stage_1_2_recover_from_api_out(
    out_dir: Path, media_dir: Path, config: Config, caption_map: dict[str, dict],
) -> list[dict]:
    """Recover minerU figures from the shared mineru-api-out tree after --delete.

    Called only when img_source_dir is None AND media_dir has no p*-mineru_*.*
    images (a cached re-ingest whose media dir was wiped by ``--delete``).
    Stage 1.1 chunk cache skips minerU re-run, so harvest never re-fires; the
    only surviving copy of each figure is under
    ``runtime_dir/mineru-api-out/<uuid>/.../images/``. This reads the per-chunk
    ``_mineru_figures.json`` manifests (written during the original harvest) to
    get each target's MD5[:8] id + filename + page + dimensions, builds a
    MD5[:8] -> source-path index over every image minerU saved, and copies
    matches back into media_dir with their original filenames.
    """
    import hashlib

    # 1. Collect targets from per-chunk manifests.
    #    img_id is the 8-char MD5 prefix between "-mineru_" and the extension.
    targets: dict[str, list[dict]] = {}
    for fj in out_dir.rglob("_mineru_figures.json"):
        try:
            entries = json.loads(fj.read_text(encoding="utf-8"))
        except Exception:
            continue
        for e in entries:
            fn = e.get("filename", "")
            if "-mineru_" not in fn:
                continue
            img_id = fn.split("-mineru_", 1)[1].rsplit(".", 1)[0]
            targets.setdefault(img_id, []).append({
                "filename": fn,
                "page": e.get("page", 0),
                "width": e.get("width", 0),
                "height": e.get("height", 0),
            })
    if not targets:
        return []

    # 2. Build MD5[:8] -> source path index over mineru-api-out images.
    api_out = config.runtime_dir / "mineru-api-out"
    if not api_out.exists():
        return []
    exts = (".jpg", ".jpeg", ".png", ".webp")
    index: dict[str, Path] = {}
    for img in api_out.rglob("*"):
        if not img.is_file() or img.suffix.lower() not in exts:
            continue
        try:
            h = hashlib.md5(img.read_bytes()).hexdigest()[:8]
        except Exception:
            continue
        index.setdefault(h, img)  # first match wins; duplicates are byte-identical

    # 3. Copy matched images into media_dir with their original filenames.
    images: list[dict] = []
    for img_id, entries in targets.items():
        src = index.get(img_id)
        if src is None:
            continue
        for e in entries:
            dest = media_dir / e["filename"]
            if not dest.exists():
                try:
                    shutil.copy2(src, dest)
                except Exception:
                    continue
            meta = caption_map.get(e["filename"], {})
            images.append({
                "filename": e["filename"],
                "path": str(dest.relative_to(config.wiki_root)),
                "page": e["page"],
                "caption": meta.get("caption", ""),
                "width": e["width"],
                "height": e["height"],
            })
    if images:
        print(f"[stage 1.2] recovered {len(images)} figures from mineru-api-out "
              f"via MD5 match (media dir had been wiped)")
    return images


def _stage_1_2_extract_from_mineru(out_dir: Path, config: Config, raw_file: Path) -> dict:
    """Extract images from minerU output (pipeline txt / vlm / auto backends).

    minerU writes images to <out_dir>/<stem>/<method>/images/ where <method>
    is txt (pipeline -m txt), vlm (vlm-engine), or auto. Also reads
    content_list.json to harvest minerU's own image_caption (the PDF figure
    caption) so downstream Stage 1.3 can use it as anchoring context.
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

    # Harvest minerU image_caption from content_list.json.
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
                        "page": b.get("page_idx", 0),
                    }
        if caption_map:
            break  # use first content_list that yields images

    images = []
    if img_source_dir:
        for img_path in sorted(img_source_dir.glob("*")):
            if not img_path.is_file():
                continue
            dest = media_dir / img_path.name
            shutil.copy2(img_path, dest)
            meta = caption_map.get(img_path.name, {})
            # NOTE (2026-06-24): no .caption.txt sidecar is written here.
            # minerU's image_caption is a figure label, not a description —
            # using it as the final caption caused lazy label-only captions
            # (bug 2026-06-24). Stage 1.3 VLM-captions every image, using
            # minerU's caption as context (see _stage_1_3_caption.py).
            # BUGFIX 2026-07-06: this branch hardcoded width/height to 0
            # instead of reading the actual copied image (the sibling
            # _stage_1_2_harvest_images() computes real dims) — every book
            # extracted via this path got a manifest of all-zero dims, which
            # then showed up as "尺寸 0×0" in every retry-placeholder caption
            # regardless of the image's real size.
            w, h = _stage_1_2_image_size(dest)
            images.append({
                "filename": img_path.name,
                "path": str(dest.relative_to(config.wiki_root)),
                "page": meta.get("page", 0),
                "caption": meta.get("caption", ""),
                "width": w,
                "height": h,
            })
    else:
        # BUGFIX 2026-06-24: on OCR cache-resume the minerU API output dir is
        # not persisted (img_source_dir is None), so the original code wrote an
        # EMPTY manifest, wiping all figures. But OCR already saved figures to
        # media_dir as p*-mineru_*.* during chunk processing — recover them so
        # the manifest reflects reality and Stage 1.3 can caption them.
        for img_path in sorted(media_dir.glob("p*-mineru_*.*")):
            if not img_path.is_file() or img_path.name.endswith(".caption.txt"):
                continue
            bn = img_path.name
            try:
                page = int(bn[1:bn.index("-")])
            except (ValueError, IndexError):
                page = 0
            meta = caption_map.get(bn, {})
            w, h = _stage_1_2_image_size(img_path)
            images.append({
                "filename": bn,
                "path": str(img_path.relative_to(config.wiki_root)),
                "page": page,
                "caption": meta.get("caption", ""),
                "width": w,
                "height": h,
            })

    # BUGFIX 2026-06-25: --delete wipes media_dir, and Stage 1.1 chunk cache
    # skips minerU re-run so harvest never re-fires. img_source_dir is None
    # (the per-chunk API output UUID isn't persisted) and media_dir is empty.
    # Recover figures from the shared mineru-api-out tree by matching the
    # MD5[:8] id embedded in each _mineru_figures.json filename against the
    # MD5[:8] of every image minerU saved. Without this, --delete + re-ingest
    # of a cached source silently produces 0 figures.
    if not images:
        images = _stage_1_2_recover_from_api_out(out_dir, media_dir, config, caption_map)

    manifest_path = media_dir / "_manifest.json"
    _stage_1_2_write_manifest(manifest_path, "mineru-ocr", raw_file, images)
    print(f"[stage 1.2] minerU: {len(images)} images from {media_dir.name} "
          f"(Stage 1.3 will VLM-caption all)")
    return {
        "count": len(images),
        "media_dir": str(media_dir),
        "manifest": str(manifest_path),
        "images": images,
        "mineru": True,
    }


def _stage_1_2_extract_markdown_images(raw_file: Path, media_dir: Path, manifest_path: Path,
                                        config: Config, min_size: int = 100) -> dict:
    """Extract local images referenced by a Markdown source into wiki/media/<slug>/.

    NashSU parity: extractAndSaveMarkdownImages + findLocalMarkdownImageRefs
    (extract-source-images.ts). A .md source may embed images via ![[ref]]
    (Obsidian/wikilink) or ![alt](ref) (standard markdown) pointing at local
    files; each referenced image is copied into the media dir and recorded in
    the manifest so Stage 1.3 captions it and Stage 3.2 injects it — same
    pipeline as minerU-harvested figures.

    Remote (http/https/ftp/data:) URIs are left in place (not copied). Only
    local refs, resolved against the source file's directory, are copied.
    Returns: {"count": int, "media_dir": str, "manifest": str, "images": list}
    """
    import re as _re
    import shutil

    _MARKDOWN_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
    text = raw_file.read_text(encoding="utf-8")
    source_dir = raw_file.parent

    refs: list[str] = []
    seen: set[str] = set()

    def _add(raw_ref: str) -> None:
        ref = raw_ref.split("#")[0].split("|")[0].strip()
        if not ref:
            return
        if ref.lower().startswith(("http://", "https://", "ftp://", "data:")):
            return
        if os.path.splitext(ref)[1].lower() not in _MARKDOWN_IMAGE_EXTS:
            return
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)

    for m in _re.finditer(r'!\[\[([^\]]+)\]\]', text):
        _add(m.group(1))
    for m in _re.finditer(r'!\[[^\]]*\]\(([^)\s]+)(?:\s+["\'][^"\']*["\'])?\)', text):
        _add(m.group(1))

    saved: list[dict] = []
    for ref in refs:
        src = Path(ref) if Path(ref).is_absolute() else source_dir / ref
        if not src.exists():
            print(f"[stage 1.2] markdown image not found, skipped: {ref}")
            continue
        idx = len(saved) + 1
        ext = os.path.splitext(src.name)[1].lower() or ".png"
        dest_name = f"md_{idx:03d}_{src.stem}{ext}"
        dest = media_dir / dest_name
        try:
            shutil.copyfile(str(src), str(dest))
        except Exception as e:
            print(f"[stage 1.2] markdown image copy failed ({ref}): {e}")
            continue
        w, h = 0, 0
        try:
            from PIL import Image
            im = Image.open(dest)
            w, h = im.size
            im.close()
        except Exception:
            pass
        if w and h and _is_image_too_small(w, h):
            dest.unlink(missing_ok=True)
            continue
        saved.append({
            "filename": dest_name,
            "page": None,
            "path": str(dest.relative_to(config.wiki_root)),
            "width": w, "height": h,
            "source": "markdown-embedded",
        })

    _stage_1_2_write_manifest(manifest_path, "markdown", raw_file, saved)
    print(f"[stage 1.2] {len(saved)} markdown-embedded images copied to {media_dir.name}")
    return {"count": len(saved), "media_dir": str(media_dir),
            "manifest": str(manifest_path), "images": saved}


def stage_1_2_extract_images(raw_file: Path, config: Config, min_size: int = 100) -> dict:
    """Extract embedded images from PPTX / DOCX / Markdown sources.

    - PPTX/DOCX: internal zipfile media/ directory (NashSU parity: extractAndSaveSourceImages).
    - Markdown: local images referenced via ![[ref]] / ![alt](ref), copied into the media
      dir (NashSU parity: extractAndSaveMarkdownImages, added 2026-07-08).

    PDF images are extracted separately by _stage_1_2_extract_from_mineru(), since all PDF
    text extraction routes through minerU (hybrid-engine/auto), which extracts images as
    part of the same pass.

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
    if raw_file.suffix.lower() in (".md", ".markdown"):
        return _stage_1_2_extract_markdown_images(raw_file, media_dir, manifest_path, config, min_size)
    return _stage_1_2_extract_images_office(raw_file, media_dir, manifest_path, min_size)
