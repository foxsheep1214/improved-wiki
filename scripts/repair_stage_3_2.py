#!/usr/bin/env python3
"""
repair_stage_3_2.py — Rebuild ## Embedded Images sections in source pages.

Reads current .caption.txt files from wiki/media/ and updates the
caption column in source page `## Embedded Images` tables. This is
needed after batch caption repair — the .caption.txt files are fixed
but the source pages still show stale captions.

Works by:
  1. Walking wiki/sources/**/*.md
  2. Extracting media/<slug>/<filename> references from the table
  3. Locating the corresponding .caption.txt under wiki/media/
  4. Replacing stale captions with current ones
  5. Atomic write (tmp → rename)

Usage:
  python3 scripts/repair_stage_3_2.py                          # all pages
  python3 scripts/repair_stage_3_2.py --dry-run                # preview only
  python3 scripts/repair_stage_3_2.py --slug "Some Book"       # single source
"""

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Optional


def find_media_dir(wiki_root: Path, slug: str) -> Optional[Path]:
    """Find media/<type>/<slug>/ directory given just the slug name."""
    media_root = wiki_root / "media"
    if not media_root.exists():
        return None
    for type_dir in media_root.iterdir():
        if not type_dir.is_dir():
            continue
        candidate = type_dir / slug
        if candidate.is_dir():
            return candidate
    return None


def _find_media_for_source(source_path: Path, wiki_root: Path) -> Optional[tuple[Path, str]]:
    """Find the wiki/media/ directory matching this source page.

    Strategy:
      1. Parse frontmatter `sources:` to get raw file path
      2. Derive slug from the raw file stem
      3. Search wiki/media/<type>/<slug>/

    Returns (media_dir, slug) or None.
    """
    try:
        text = source_path.read_text(encoding="utf-8")
    except Exception:
        return None

    # Extract sources from frontmatter
    fm_match = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
    if fm_match:
        fm_text = fm_match.group(1)
        src_match = re.search(r'sources:\s*\[(.*?)\]', fm_text)
        if src_match:
            raw_files = re.findall(r'"([^"]+)"', src_match.group(1))
            for rf in raw_files:
                # raw/Book/xxx.pdf → slug = book/xxx
                parts = rf.split("/", 1)
                if len(parts) >= 2:
                    type_dir = parts[0]  # book / datasheet / paper
                    stem = Path(parts[1]).stem  # filename without .pdf
                    slug = f"{type_dir}/{stem}"
                    media_dir = wiki_root / "media" / slug
                    if media_dir.is_dir():
                        return media_dir, slug

    # Fallback: try source page name as slug
    slug = source_path.stem
    for type_dir in (wiki_root / "media").iterdir() if (wiki_root / "media").exists() else []:
        if not type_dir.is_dir():
            continue
        candidate = type_dir / slug
        if candidate.is_dir():
            return candidate, slug

    return None


def build_embedded_images_section(media_dir: Path, slug: str) -> Optional[str]:
    """Build a fresh ## Embedded Images section from media/*.caption.txt files.

    Returns the full markdown section string, or None if no captioned images found."""
    lines = ["## Embedded Images", ""]
    total_imgs = 0
    captioned = 0

    for img_file in sorted(media_dir.iterdir()):
        if img_file.suffix.lower() not in ('.png', '.jpg', '.jpeg'):
            continue
        cap_path = media_dir / (img_file.name + ".caption.txt")
        if not cap_path.exists():
            continue
        cap_text = cap_path.read_text(encoding="utf-8").strip()
        if not cap_text or "[待重试]" in cap_text or "解析失败" in cap_text:
            continue
        total_imgs += 1
        captioned += 1
        # Extract page number from filename like p123-fig4.png
        page = img_file.stem.split("-")[0] if "-" in img_file.stem else img_file.stem
        caption_escaped = cap_text.replace("|", "\\|")
        media_rel = f"media/{slug}/{img_file.name}"
        lines.append(f"| {page} | {caption_escaped} | `{media_rel}` |")

    if not lines[2:]:
        return None

    lines.insert(1, f"本书共抽出 {captioned} 张已配 caption 的图。")
    lines.insert(1, "")
    return "\n".join(lines)


def rebuild_embedded_images(source_text: str, wiki_root: Path,
                            source_path: Optional[Path] = None) -> tuple[str, int]:
    """Rebuild or add the ## Embedded Images section with current captions.

    Returns (updated_text, changes_count)."""
    # Find the ## Embedded Images section
    h2_match = re.search(r'^## Embedded Images\s*\n', source_text, re.MULTILINE)
    if h2_match:
        # Existing section — update captions in table
        table_start = h2_match.end()
        remaining = source_text[table_start:]

        row_pattern = re.compile(
            r'^\|\s*(p?\d+)\s*\|\s*(.*?)\s*\|\s*`(media/[^`]+)`\s*\|',
            re.MULTILINE
        )
        rows = list(row_pattern.finditer(remaining))
        if not rows:
            return source_text, 0

        last_row_end = rows[-1].end()
        after_table = remaining[last_row_end:]

        new_rows = []
        changes = 0
        for m in rows:
            page = m.group(1)
            old_caption = m.group(2).strip()
            media_rel = m.group(3).strip()
            parts = media_rel.split("/", 1)
            if len(parts) < 2:
                new_rows.append(m.group(0))
                continue
            slug_and_file = parts[1]
            slug_parts = slug_and_file.rsplit("/", 1)
            if len(slug_parts) < 2:
                new_rows.append(m.group(0))
                continue
            slug, filename = slug_parts[0], slug_parts[1]

            media_dir = find_media_dir(wiki_root, slug)
            if not media_dir:
                new_rows.append(m.group(0))
                continue

            cap_path = media_dir / (filename + ".caption.txt")
            img_exists = (media_dir / filename).exists()
            if not img_exists:
                # Image was deleted — drop this row entirely
                changes += 1
                continue
            if cap_path.exists():
                new_caption = cap_path.read_text(encoding="utf-8").strip()
                if "[待重试]" in new_caption or "解析失败" in new_caption:
                    new_rows.append(m.group(0))
                    continue
                if new_caption != old_caption:
                    changes += 1
            else:
                new_rows.append(m.group(0))
                continue

            new_caption_escaped = new_caption.replace("|", "\\|")
            new_row = f"| {page} | {new_caption_escaped} | `{media_rel}` |"
            new_rows.append(new_row)

        if changes == 0:
            return source_text, 0

        new_table = "\n".join(new_rows)
        result = source_text[:table_start] + new_table + after_table
        return result, changes

    # No existing section — build fresh from media dir
    if source_path is None:
        return source_text, 0

    media_result = _find_media_for_source(source_path, wiki_root)
    if not media_result:
        return source_text, 0
    media_dir, slug = media_result

    new_section = build_embedded_images_section(media_dir, slug)
    if not new_section:
        return source_text, 0

    # Count images added
    img_count = new_section.count("| p") + new_section.count("| `")
    # Append before the page ends (before any trailing newlines)
    result = source_text.rstrip() + "\n\n" + new_section + "\n"
    return result, img_count


def repair_source_page(page_path: Path, wiki_root: Path, dry_run: bool = False) -> int:
    """Repair a single source page. Returns number of caption changes."""
    try:
        text = page_path.read_text(encoding="utf-8")
    except Exception:
        return 0

    new_text, changes = rebuild_embedded_images(text, wiki_root, source_path=page_path)
    if changes == 0:
        return 0

    if not dry_run:
        tmp = page_path.with_suffix(page_path.suffix + ".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.rename(page_path)

    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without writing")
    parser.add_argument("--slug", type=str, default=None,
                        help="Repair only source pages matching this slug substring")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show per-page detail")
    args = parser.parse_args()

    root = Path(os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd()))
    wiki_root = root / "wiki"
    sources_dir = wiki_root / "sources"
    if not sources_dir.is_dir():
        print(f"ERROR: {sources_dir} not found", file=sys.stderr)
        return 2

    total_pages = 0
    total_changes = 0
    mode = "DRY-RUN" if args.dry_run else "repair"

    for page_path in sorted(sources_dir.rglob("*.md")):
        rel = page_path.relative_to(sources_dir)
        if args.slug and args.slug not in str(rel) and args.slug not in page_path.stem:
            continue

        changes = repair_source_page(page_path, wiki_root, dry_run=args.dry_run)
        if changes > 0:
            total_pages += 1
            total_changes += changes
            if args.verbose:
                print(f"  [{mode}] {rel}: {changes} captions updated")

    print(f"[{mode}] {total_pages} source pages, {total_changes} captions updated")
    if args.dry_run:
        print("[dry-run] No files modified. Remove --dry-run to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
