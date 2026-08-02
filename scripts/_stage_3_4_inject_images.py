"""Stage 3.2: Image injection into the source page.

Extracted from ingest.py on 2026-06-21 for stage-module locality (was inline
in the orchestrator). Appends an '## Embedded Images' section to the source
page, reading from the unified _manifest.json (Path A PyMuPDF + Path B minerU)
with legacy _figures.json / cloud-OCR caption fallbacks.
"""
import json
import os
import re
from pathlib import Path

from _config import Config
from _language import get_output_language
from _paths import media_slug, atomic_write
from _wikilinks import WIKILINK_RE


def _stage_3_2_language_sample(content: str) -> str:
    """Return page prose without metadata/link targets that can spoof script.

    A nested raw path such as ``Paper/01_反无人机探测与识别/...`` appears in
    source frontmatter. ``detect_language`` quite reasonably sees those Han
    characters, but they are an identifier, not the English page's prose.
    """
    sample = re.sub(
        r"\A---\s*\n.*?\n---\s*\n?",
        "",
        content,
        count=1,
        flags=re.DOTALL,
    )
    sample = sample.split("\n## Embedded Images", 1)[0]
    sample = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", sample)
    sample = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", sample)

    def _wikilink_label(match: re.Match) -> str:
        target = match.group(1)
        label = match.group(2)
        return label or target.rsplit("/", 1)[-1]

    sample = WIKILINK_RE.sub(_wikilink_label, sample)
    return sample[:4000]


def _stage_3_2_source_kind(raw_file: Path, config: Config) -> str:
    try:
        category = raw_file.relative_to(config.raw_root).parts[0].lower()
    except (ValueError, IndexError):
        return "document"
    return {
        "book": "book",
        "paper": "paper",
        "standard": "standard",
        "presentation": "presentation",
        "news": "article",
    }.get(category, "document")


def _stage_3_2_count_line(
    *, is_zh: bool, source_kind: str, count: int, is_mineru: bool,
) -> str:
    if is_zh:
        subject = {
            "book": "本书",
            "paper": "本文",
            "standard": "本标准",
            "presentation": "本演示文稿",
            "article": "本报道",
        }.get(source_kind, "本文档")
        noun = "图表" if is_mineru else "嵌入图"
        return f"{subject}共抽出 {count} 张{noun}。"

    subject = {
        "book": "This book",
        "paper": "This paper",
        "standard": "This standard",
        "presentation": "This presentation",
        "article": "This article",
    }.get(source_kind, "This document")
    singular, plural = (
        ("figure", "figures")
        if is_mineru else
        ("embedded image", "embedded images")
    )
    noun = singular if count == 1 else plural
    return f"{subject} contains {count} extracted {noun}."


def stage_3_4_inject_images(
    config: Config, raw_file: Path, source_path: Path,
) -> dict:
    """Append '## Embedded Images' section to the source page.

    Two paths:
    - Text-layer PDFs: reads _manifest.json from wiki/media/<raw-subpath>/<slug>/
    - Scanned PDFs:   reads .caption.txt files from OCR output dir
    """
    content = source_path.read_text(encoding="utf-8")
    content = re.sub(r"^## Embedded Images.*?(?=^## |\Z)", "", content, flags=re.MULTILINE | re.DOTALL)
    content = content.rstrip() + "\n\n"

    # Two-language KB policy (2026-07-15): the boilerplate prose in this
    # section (count line, attribution line) must follow the page's own
    # language, not be hardcoded Chinese — sampling the already-written body
    # is the correct signal since earlier stages already wrote it in the
    # page's target language (get_output_language collapses everything to
    # Chinese or English). Structural headings ("## Embedded Images",
    # "### Page N") stay English in both cases, matching the rest of the
    # pipeline's FILE-block convention: only prose
    # vocabulary is localized, not structural markup).
    is_zh = get_output_language(_stage_3_2_language_sample(content)) == "Chinese"
    source_kind = _stage_3_2_source_kind(raw_file, config)

    # Unified image injection: reads _manifest.json (the single source of truth
    # for both Path A PyMuPDF and Path B minerU).  Old ingests with full-page
    # renders are filtered via source != "page-render" for backward compat.
    slug = media_slug(raw_file, config)
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
        # Manifest schema guard: every entry must carry page + filename (the
        # grouping/caption code below indexes them directly). A malformed
        # manifest fails loud with its path instead of a bare KeyError.
        for img in images:
            if "page" not in img or "filename" not in img:
                raise RuntimeError(
                    f"[stage 3.4] malformed image entry in {source_path_to_read}: "
                    f"missing 'page'/'filename' — entry: {img}")
            page_index = img["page"]
            if (page_index is not None
                    and (not isinstance(page_index, int)
                         or isinstance(page_index, bool) or page_index < 0)):
                raise RuntimeError(
                    f"[stage 3.4] malformed image entry in {source_path_to_read}: "
                    f"'page' must be null or a non-negative integer — entry: {img}")
        if images:
            is_mineru = any("mineru_" in i.get("filename", "") for i in images[:10])
            section = "## Embedded Images\n\n"
            section += _stage_3_2_count_line(
                is_zh=is_zh,
                source_kind=source_kind,
                count=len(images),
                is_mineru=is_mineru,
            ) + "\n\n"
            # NashSU parity (extract-source-images.ts:buildImageMarkdownSection):
            # group numbered images under `### Page N` and unpaged DOCX/
            # Markdown images under `### Document`. Emit markdown image syntax
            # with 1-based source page numbers.  The manifest deliberately
            # keeps minerU's zero-based page index (also encoded in pNNNN), so
            # convert only at this presentation boundary. Emit ![caption](path)
            # with the FULL caption as alt text (sanitized — no newlines, no
            # `]`), not a truncated table cell. Path is resolved
            # relative to the source page so the image renders without a
            # markdown-image-resolver (which improved-wiki does not have).
            source_dir = source_path.parent
            by_page: dict[str, list] = {}
            for img in images:
                page_index = img["page"]
                key = "Document" if page_index is None else f"Page {page_index + 1}"
                by_page.setdefault(key, []).append(img)
            for page_images in by_page.values():
                page_images.sort(key=lambda x: x.get("img_idx_in_page", 0))

            def page_order(key: str) -> tuple[int, int]:
                if key == "Document":
                    return (1, 0)
                return (0, int(key.removeprefix("Page ")))

            for key in sorted(by_page, key=page_order):
                section += f"### {key}\n\n"
                for img in by_page[key]:
                    cap_path = media_dir / (img["filename"] + ".caption.txt")
                    cap = cap_path.read_text(encoding="utf-8").strip() if cap_path.exists() else ""
                    cap = re.sub(r"[\r\n]+", " ", cap).replace("]", ")").strip()
                    img_abs = media_dir / img["filename"]
                    try:
                        rel = os.path.relpath(img_abs, source_dir)
                    except ValueError:
                        rel = img.get("path", "")
                    section += f"![{cap}]({rel})\n\n"
            if is_zh:
                section += f"\n> 图片由 {'minerU VLM' if is_mineru else 'PyMuPDF'} 提取，caption 由 {config.caption_model} 生成。详细 manifest 见 `wiki/media/{slug}/`\n"
            else:
                section += f"\n> Images extracted by {'minerU VLM' if is_mineru else 'PyMuPDF'}; captions generated by {config.caption_model}. Full manifest: `wiki/media/{slug}/`\n"
            content += section
            atomic_write(source_path, content)
            print(f"[stage 3.4] Injected {len(images)} images into {source_path.name}")
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
        section = "## Embedded Images\n\n"
        section += _stage_3_2_count_line(
            is_zh=is_zh,
            source_kind=source_kind,
            count=len(images_in_media),
            is_mineru=True,
        ) + "\n\n"
        if is_zh:
            section += "| 文件/页码 | Caption |\n|------------|----------|\n"
        else:
            section += "| File/Page | Caption |\n|------------|----------|\n"
        for name, cap in images_in_media[:200]:  # cap at 200 rows
            cap_short = cap[:80] + "..." if len(cap) > 80 else cap
            section += f"| `{name}` | {cap_short} |\n"
        if len(images_in_media) > 200:
            section += f"| ... | ({len(images_in_media) - 200} more) |\n"
        if is_zh:
            section += f"\n> Caption 由 {config.caption_model} 生成。图片文件见 `wiki/media/{slug}/`\n"
        else:
            section += f"\n> Captions generated by {config.caption_model}. Image files: `wiki/media/{slug}/`\n"
        content += section
        atomic_write(source_path, content)
        print(f"[stage 3.4] Injected {len(images_in_media)} images into {source_path.name}")
        return {"injected": len(images_in_media)}

    print("[stage 3.4] No images or figures to inject — skipping")
    return {"injected": 0}
