"""Resolve source references without conflating same-named documents."""
from __future__ import annotations

from pathlib import Path, PurePosixPath


class AmbiguousSourceError(ValueError):
    pass


def normalize_source_ref(value: str, project: Path | None) -> str:
    value = str(value).strip().replace('\\', '/')
    path = Path(value)
    if path.is_absolute():
        if project is None:
            raise ValueError("Absolute source reference requires a project root")
        try:
            value = path.resolve().relative_to(project.resolve()).as_posix()
        except ValueError as exc:
            raise ValueError(f'Source is outside project: {value}') from exc
    parts = PurePosixPath(value).parts
    if not parts or '..' in parts:
        raise ValueError(f'Invalid source reference: {value}')
    value = PurePosixPath(*parts).as_posix()
    if '/' in value and not value.startswith(('raw/', 'wiki/queries/')):
        value = 'raw/' + value  # legacy raw-relative cache/frontmatter path
    return value


class SourceResolver:
    def __init__(self, project: Path | None, sources=()):
        self.project = project
        self.sources = {normalize_source_ref(s, project) for s in sources}

    def resolve(self, value: str) -> str | None:
        value = normalize_source_ref(value, self.project)
        if '/' in value:
            return value  # explicit paths NEVER fall back to a basename
        matches = {s for s in self.sources if Path(s).name == value}
        if not Path(value).suffix:
            matches |= {s for s in self.sources if Path(s).stem == value}
        if len(matches) > 1:
            raise AmbiguousSourceError(
                f'Ambiguous source {value!r}: ' + ', '.join(sorted(matches)))
        return next(iter(matches), None)


def raw_source_refs(project: Path) -> set[str]:
    return {p.relative_to(project).as_posix()
            for folder in ('raw', 'wiki/queries')
            for p in (project / folder).rglob('*')
            if p.is_file() and not any(part.startswith('.')
                for part in p.relative_to(project / folder).parts)}
