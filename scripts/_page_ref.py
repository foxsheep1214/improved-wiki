"""Canonical references to Markdown pages inside a wiki project.

Persisted ingest/cache/checkpoint values use exactly one representation:
project-root-relative POSIX paths with a leading ``wiki/`` segment, for
example ``wiki/concepts/cache-coherence.md``.  Consumers that need a FILE
block path, an on-disk path, or a wikilink slug derive it from ``PageRef``
instead of guessing which path convention a string currently uses.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class PageRefError(ValueError):
    """Raised when a value cannot identify a safe page inside ``wiki/``."""


@dataclass(frozen=True)
class PageRef:
    """One canonical wiki page with explicit path projections."""

    project_relative: str
    wiki_relative: str
    absolute_path: Path

    @classmethod
    def parse(
        cls,
        value: str | Path | "PageRef",
        wiki_root: str | Path,
        wiki_dir: str | Path | None = None,
    ) -> "PageRef":
        """Normalize a legacy/canonical/absolute page path.

        Accepted inputs:
        - canonical project-relative: ``wiki/concepts/x.md``
        - legacy wiki-relative: ``concepts/x.md``
        - absolute path contained by the project's wiki directory

        The returned persisted representation is always ``wiki/...``.
        Traversal, paths outside the wiki, doubled ``wiki/wiki/`` prefixes,
        and non-Markdown paths are rejected.
        """
        if isinstance(value, cls):
            expected_root = Path(wiki_root).expanduser().resolve()
            expected_wiki = (
                Path(wiki_dir).expanduser().resolve()
                if wiki_dir is not None
                else expected_root / "wiki"
            )
            if value.absolute_path.parent == expected_wiki or expected_wiki in value.absolute_path.parents:
                return value
            value = value.absolute_path

        root = Path(wiki_root).expanduser().resolve()
        wiki = (
            Path(wiki_dir).expanduser().resolve()
            if wiki_dir is not None
            else root / "wiki"
        )
        raw = str(value).strip()
        if not raw:
            raise PageRefError("page reference is empty")

        # Treat Windows separators consistently even when running on POSIX.
        normalized = raw.replace("\\", "/")
        windows_absolute = bool(re.match(r"^[A-Za-z]:/", normalized))
        path_obj = Path(raw).expanduser()
        if path_obj.is_absolute() or windows_absolute:
            if windows_absolute and not path_obj.is_absolute():
                raise PageRefError(
                    f"Windows absolute page path is outside this project: {raw}")
            absolute = path_obj.resolve()
            try:
                wiki_rel_path = absolute.relative_to(wiki)
            except ValueError as exc:
                raise PageRefError(
                    f"absolute page path is outside wiki directory: {raw}") from exc
            wiki_relative = wiki_rel_path.as_posix()
        else:
            while normalized.startswith("./"):
                normalized = normalized[2:]
            parts = normalized.split("/")
            if any(part == ".." for part in parts):
                raise PageRefError(
                    f"page reference contains traversal: {raw}")
            parts = [part for part in parts if part not in ("", ".")]
            if not parts:
                raise PageRefError("page reference is empty")
            if parts[0] == "wiki":
                parts = parts[1:]
            if not parts:
                raise PageRefError(
                    f"page reference points to the wiki directory, not a page: {raw}")
            if parts[0] == "wiki":
                raise PageRefError(
                    f"page reference has a doubled wiki prefix: {raw}")
            wiki_relative = PurePosixPath(*parts).as_posix()
            absolute = wiki.joinpath(*PurePosixPath(wiki_relative).parts)

        wiki_parts = PurePosixPath(wiki_relative).parts
        if not wiki_parts or any(part in ("", ".", "..") for part in wiki_parts):
            raise PageRefError(f"unsafe page reference: {raw}")
        if wiki_parts[0] == "wiki":
            raise PageRefError(
                f"page reference has a doubled wiki prefix: {raw}")
        if PurePosixPath(wiki_relative).suffix.lower() != ".md":
            raise PageRefError(
                f"wiki page reference must end in .md: {raw}")
        if PurePosixPath(wiki_relative).name == ".md":
            raise PageRefError(f"wiki page reference has no filename: {raw}")

        project_relative = f"wiki/{wiki_relative}"
        return cls(
            project_relative=project_relative,
            wiki_relative=wiki_relative,
            absolute_path=absolute,
        )

    @property
    def slug(self) -> str:
        """Wiki-relative path without the Markdown suffix."""
        return PurePosixPath(self.wiki_relative).with_suffix("").as_posix()

    @property
    def name(self) -> str:
        return PurePosixPath(self.wiki_relative).name

    def __str__(self) -> str:
        return self.project_relative


def canonical_page_refs(
    values: list[str | Path | PageRef],
    wiki_root: str | Path,
    wiki_dir: str | Path | None = None,
) -> list[str]:
    """Normalize and de-duplicate page refs while preserving first-seen order."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        ref = PageRef.parse(value, wiki_root, wiki_dir)
        if ref.project_relative in seen:
            continue
        seen.add(ref.project_relative)
        out.append(ref.project_relative)
    return out
