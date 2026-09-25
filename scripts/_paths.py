"""
_paths.py — Shared runtime directory detection + path-derivation utilities for
improved-wiki scripts.

Shared by every CLI; detection never migrates data:
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
from typing import TYPE_CHECKING, Iterable, Iterator

if TYPE_CHECKING:
    # Avoid a runtime cycle: _core imports _paths (detect_runtime_dir), so _paths
    # must not import Config at runtime. The annotation is evaluated lazily under
    # `from __future__ import annotations`.
    from _config import Config


RUNTIME_STATE_NAMES = (
    "ingest-cache.json", "ingest-events.jsonl", "ingest-progress", "extract-tmp",
    "lancedb", "embed-cache.json", "ingest-queue.json", "conversation",
    "lint-cache.json", "lint-semantic.json", "lint-run-state.json", "lint",
    "review.json", "review-suggestions.json", "spine-reservation.json",
)
LEGACY_STATE_ALIASES = {
    ".ingest-cache.json": "ingest-cache.json",
    ".ingest-progress": "ingest-progress",
    ".extract-tmp": "extract-tmp",
}


def detect_runtime_dir(wiki_root: Path) -> Path:
    """Select the runtime without creating, moving or deleting anything.

    A populated .iwiki-runtime remains readable until explicitly migrated.
    Two populated dedicated runtimes require operator reconciliation; they must
    never silently choose different ingest.lock files.
    """
    root = wiki_root.expanduser().resolve()
    current, old, wiki = root / ".llm-wiki", root / ".iwiki-runtime", root / "wiki"
    current_exists = any((current / name).exists() for name in RUNTIME_STATE_NAMES)
    old_exists = any(p.is_file() and not p.name.endswith((".lock", ".lease"))
                     for p in old.rglob("*")) if old.exists() else False
    if old_exists and current_exists:
        raise RuntimeError("Both .iwiki-runtime and .llm-wiki contain state; "
                           "preview migrate_runtime.py before choosing a writer")
    if old_exists:
        if any((old / name).exists() for name in LEGACY_STATE_ALIASES):
            raise RuntimeError("Legacy hidden state requires migrate_runtime.py --from .iwiki-runtime")
        return old
    if current_exists:
        return current
    if any((wiki / name).exists() for name in LEGACY_STATE_ALIASES):
        raise RuntimeError("Legacy hidden state requires migrate_runtime.py --from wiki")
    if any((wiki / name).exists() for name in
           ("ingest-cache.json", "ingest-progress", "extract-tmp")):
        return wiki
    return current


# ══════════════════════════════════════════════════════════════════════════════
# Wiki page traversal (shared by lint / semantic lint / dedup / validate / graph).
#
# WIKI_ARTIFACT_DIRS: top-level wiki/ subdirs holding DERIVED artifacts, never
# knowledge pages. This port WRITES ingest review items to wiki/REVIEW/ and
# graph cluster-hub pages to wiki/clusters/ (NashSU has neither); without this
# guard those diagnostics leak back into lint/dedup/graph input as if they were
# wiki content, risking self-referential findings and an ingest feedback loop.
# Single source of truth — do not redeclare per tool (the per-tool copies
# drifted: cross_source_dedup and validate_ingest were missing `clusters`).
# ══════════════════════════════════════════════════════════════════════════════

WIKI_ARTIFACT_DIRS = frozenset({"lint", "REVIEW", "clusters", "media"})


def atomic_write(path, content: str, encoding: str = "utf-8") -> None:
    """Write file atomically via tmp + rename. Prevents partial writes.

    Canonical implementation (moved from _core so light tools don't need to
    import the full core module; _core re-exports it for back compat).
    """
    import os
    p = str(path)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding=encoding) as f:
        f.write(content)
    os.replace(tmp, p)


def iter_wiki_pages(
    wiki_dir: Path,
    *,
    anchor_files: Iterable[str] = frozenset(),
    state_files: Iterable[str] = frozenset(),
    skip_dirs: Iterable[str] = WIKI_ARTIFACT_DIRS,
) -> Iterator[tuple[str, str]]:
    """Yield (rel_path_str, content) for knowledge pages under wiki_dir.

    anchor_files / state_files stay per-tool (each tool's scan universe is a
    deliberate semantic choice); the walk itself and the artifact-dir guard are
    shared. Unreadable files are skipped. Sorted for determinism.
    """
    if not wiki_dir.is_dir():
        return
    for path in sorted(wiki_dir.rglob("*.md")):
        rel = path.relative_to(wiki_dir)
        if rel.name in anchor_files or rel.name in state_files:
            continue
        if rel.parts and rel.parts[0] in skip_dirs:
            continue
        try:
            yield str(rel), path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue


# ══════════════════════════════════════════════════════════════════════════════
# Raw-source path derivation (pure functions, no side effects).
#
# These mirror the raw/ directory structure to derive media-directory slugs and
# raw-type subdirectories. They live here (not in the Stage 1 image module) so
# that Stage 3.4, validators, and Stage 2 can use them without a fake dependency
# on Stage 1.
# ══════════════════════════════════════════════════════════════════════════════


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
