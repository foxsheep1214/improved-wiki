"""
_paths.py — Shared runtime directory detection + path-derivation utilities for
improved-wiki scripts.

Matches ingest.py Config.from_env() logic exactly:
  - Default:     <root>/.llm-wiki/          (NashSU-aligned)
  - Back compat: <root>/.iwiki-runtime/     (existing improved-wiki projects)
  - Legacy:      <root>/wiki/               (when old state files exist inside wiki/)

Usage:
    from _paths import detect_runtime_dir, media_slug

    runtime = detect_runtime_dir(Path(project_root))
    extract  = runtime / "extract-tmp" / slug
    cache    = runtime / "ingest-cache.json"
    review   = runtime / "review.json"
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Avoid a runtime cycle: _core imports _paths (detect_runtime_dir), so _paths
    # must not import Config at runtime. The annotation is evaluated lazily under
    # `from __future__ import annotations`.
    from _core import Config


def detect_runtime_dir(wiki_root: Path) -> Path:
    """Return the runtime directory for this wiki project.

    Priority:
      1. .iwiki-runtime/   auto-migrate to .llm-wiki/ if it still exists
      2. .llm-wiki/        if it exists and has valid content (ingest-cache.json,
                           ingest-progress/, or embed-cache.json) — preferred over
                           legacy wiki/ even if old state files exist there
      3. wiki/             if old state files exist there (legacy), and .llm-wiki/
                           is empty or doesn't exist
      4. .llm-wiki/        clean default (NashSU-aligned)
    """
    llm_wiki = wiki_root / ".llm-wiki"
    iwiki = wiki_root / ".iwiki-runtime"

    # Auto-migrate from .iwiki-runtime → .llm-wiki
    if iwiki.exists():
        _migrate_iwiki_runtime(iwiki, llm_wiki)
        # After migration, use .llm-wiki
        return llm_wiki

    # If .llm-wiki/ exists and has valid content, use it regardless of legacy wiki/
    llm_wiki_indicators = [
        llm_wiki / "ingest-cache.json",
        llm_wiki / "ingest-progress",
        llm_wiki / "embed-cache.json",
    ]
    if any(p.exists() for p in llm_wiki_indicators):
        return llm_wiki

    # Legacy: old projects that put state inside wiki/
    old_indicators = [
        wiki_root / "wiki" / ".ingest-cache.json",
        wiki_root / "wiki" / "ingest-cache.json",
        wiki_root / "wiki" / ".ingest-progress",
        wiki_root / "wiki" / "ingest-progress",
        wiki_root / "wiki" / ".extract-tmp",
        wiki_root / "wiki" / "extract-tmp",
    ]
    if any(p.exists() for p in old_indicators):
        return wiki_root / "wiki"

    # Auto-migrate: lint-cache.json / lint-lock in wiki/
    _migrate_lint_cache_out_of_wiki(wiki_root)

    # Default: NashSU-aligned
    return llm_wiki


def _migrate_iwiki_runtime(iwiki: Path, llm_wiki: Path) -> None:
    """Migrate .iwiki-runtime/ contents → .llm-wiki/, then remove old dir."""
    import shutil, sys
    llm_wiki.mkdir(parents=True, exist_ok=True)
    count = 0
    for src in sorted(iwiki.iterdir()):
        dst = llm_wiki / src.name
        try:
            if src.is_dir():
                if dst.exists():
                    # Merge: move individual files
                    for f in src.iterdir():
                        f.rename(dst / f.name)
                        count += 1
                    src.rmdir()
                else:
                    src.rename(dst)
            else:
                if dst.exists():
                    src.unlink()  # already migrated elsewhere
                else:
                    src.rename(dst)
            count += 1
        except OSError:
            pass
    # Remove old dir if empty (or force remove after migration attempt)
    try:
        iwiki.rmdir()
    except OSError:
        pass
    if count:
        print(f"[_paths] Migrated {count} items from .iwiki-runtime/ → .llm-wiki/", file=sys.stderr)


def _migrate_lint_cache_out_of_wiki(wiki_root: Path) -> None:
    """If lint-cache.json or lint-lock exists under wiki/, move to .llm-wiki/."""
    wiki = wiki_root / "wiki"
    runtime = wiki_root / ".llm-wiki"
    migrated = 0
    for name in ("lint-cache.json", "lint-lock"):
        wiki_path = wiki / name
        if wiki_path.exists():
            runtime.mkdir(parents=True, exist_ok=True)
            dest = runtime / name
            wiki_path.rename(dest)
            migrated += 1
    # Also clean up stale numbered copies (concurrent-run artifacts)
    for stale in sorted(wiki.glob("lint-cache [0-9]*.json")):
        stale.unlink(missing_ok=True)
        migrated += 1
    for stale in sorted(wiki.glob("lint-lock [0-9]*")):
        stale.unlink(missing_ok=True)
        migrated += 1
    if migrated:
        import sys
        print(f"[_paths] Migrated {migrated} lint state file(s) from wiki/ → .llm-wiki/", file=sys.stderr)


# ══════════════════════════════════════════════════════════════════════════════
# Raw-source path derivation (pure functions, no side effects).
#
# These mirror the raw/ directory structure to derive media-directory slugs and
# raw-type subdirectories. They live here (not in the Stage 1 image module) so
# that Stage 3.2 / validators / Stage 2 can use them without a fake dependency
# on Stage 1. Moved from _stage_1_2_images.py on 2026-06-24; the old
# `_stage_1_2_*` names are kept as back-compat aliases by the facade.
# ══════════════════════════════════════════════════════════════════════════════


def raw_type_subdir(raw_file: Path, config: "Config") -> str:
    """Return the raw/-relative parent directory for this file.

    raw/Book/Foo.pdf           → book
    raw/Datasheet/05_AMP/Bar.pdf → datasheet/05_AMP
    """
    try:
        rel = raw_file.relative_to(config.raw_root)
    except ValueError:
        return ""
    parent = str(rel.parent)
    return parent if parent != "." else ""


def media_slug(raw_file: Path, config: "Config") -> str:
    """Derive media directory path from raw file path, mirroring raw/ structure.

    raw/Book/Foo.pdf           → book/Foo
    raw/Datasheet/05_AMP/Bar.pdf → datasheet/05_AMP/Bar
    """
    try:
        rel = raw_file.relative_to(config.raw_root)
    except ValueError:
        return raw_file.stem
    parent = rel.parent
    stem = rel.stem
    return str(parent / stem) if str(parent) != "." else stem
