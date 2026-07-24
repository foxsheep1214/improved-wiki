"""Compatibility facade plus small shared ingest utilities.

Focused ownership lives in ``_config``, ``_progress``, ``_schema``, ``_parse``,
and ``_retry``.  Their established ``_core`` imports remain available here.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

# atomic_write moved to _paths.py (canonical, so light tools can import it
# without pulling in the full core); re-exported here for back compat.
from _paths import atomic_write  # noqa: E402,F401

# Driver-facing batch concurrency ceiling. The OS Phase-1 pipeline uses at
# most two detached workers (one minerU resource slot + one caption resource
# slot); the same value caps handoff answers coordinated outside this process.
# Shared by ingest.py and _watch.py.
BATCH_MAX_CONCURRENT = 4


# ── Progress / UI helpers ──

_current_file_local = threading.local()
_stage_start_times: dict[str, float] = {}


def set_current_file(name: str) -> None:
    _current_file_local.value = name


def get_current_file() -> str:
    return getattr(_current_file_local, "value", "")


def file_tag() -> str:
    f = get_current_file()
    if not f:
        return ""
    if len(f) > 50:
        return f"[{f[:40]}...{f[-6:]}] "
    return f"[{f}] "


def stage_begin(name: str) -> None:
    _stage_start_times[name] = time.time()
    tag = file_tag()
    print(f"\n{'─'*40}\n{tag}[{name}] Starting...\n{'─'*40}", flush=True)


def heartbeat(msg: str = "") -> None:
    ts = time.strftime("%H:%M:%S")
    tag = file_tag()
    suffix = f" — {msg}" if msg else ""
    print(f"  {ts}  {tag}… {suffix}", flush=True)


# Rate-limit tracking (shared across workers)
_RATE_LIMIT_HIT_AT = 0.0
_RLOCK = threading.Lock()


def record_rate_limit() -> None:
    global _RATE_LIMIT_HIT_AT
    with _RLOCK:
        _RATE_LIMIT_HIT_AT = time.time()


class ConversationPending(BaseException):
    """Raised when a prompt is written to disk and awaits the calling agent.

    Subclasses BaseException (not Exception) so the broad ``except Exception``
    retry/fallback blocks around LLM calls in the stage modules do NOT swallow
    it — ConversationPending is control flow (pause for the calling agent),
    not a transient HTTP error. It still propagates to the top-level
    ``except ConversationPending`` handler (ingest.py main) which exits 101.
    """


class PrepareStopAfter(BaseException):
    """Raised inside ``_do_prepare`` when ``--stop-after-stage`` matches a
    Stage-0..2 boundary that has just completed (0=extract, 1=global digest,
    2=generation). Subclasses BaseException so the broad ``except Exception``
    in ``_do_prepare`` (which prints FAILED + traceback and re-raises) does
    not noisy-up a clean, intentional stop. Caught in ``ingest_one`` and
    converted to ``{"status": "ok", "stopped_after": stage}``.

    Without this, ``--stop-after-stage 0`` could not actually halt after OCR:
    the stop check lived AFTER ``_do_prepare`` returned, but ``_do_prepare``
    runs all of Stage 0-2 (pausing at the 2.1/2.2/2.4 LLM handoffs) before
    that check — so the flag was effectively dead on a fresh run. Raising at
    the in-prepare boundary makes the documented "OCR-only then re-run" split
    work. Boundaries 1.5/2.3 (inside the chunk pipeline, no clean resume
    marker) remain best-effort and are not intercepted here.
    """

    def __init__(self, stage: str):
        super().__init__(stage)
        self.stage = stage


# Configuration ownership moved to ``_config``.  Compatibility exports keep
# the public ``_core`` surface stable while focused modules import directly.
from _config import (  # noqa: E402,F401
    Config,
    _CONTEXT_SIZE_DEFAULT,
    _TARGET_CHARS_HARD_CEIL,
    _TARGET_TOKENS_HARD_CEIL,
    _TARGET_TOKENS_MIN,
    _compute_chunk_targets,
    load_caption_provider,
    load_provider_config,
)


# ── File-type detection ──

FOLDER_TO_TEMPLATE = {
    "Book": "digest-book",
    "Paper": "digest-paper",
    "Datasheet": "digest-datasheet",
    "Applicationnote": "digest-applicationnote",
    "Designexample": "digest-designexample",
    "Presentation": "digest-presentation",
    "Standard": "digest-standard",
    "News": "digest-news",
}


def str_distance(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    if len(b) == 0:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(
                prev[j + 1] + 1, curr[j] + 1,
                prev[j] + (0 if ca == cb else 1),
            ))
        prev = curr
    return prev[-1]


def detect_template_type(raw_file: Path, raw_root: Path, override: str | None) -> str:
    if override:
        return override
    try:
        rel = raw_file.relative_to(raw_root)
    except ValueError:
        return "digest-book"
    parts = rel.parts
    if len(parts) == 1:
        return "digest-book"
    folder = parts[0]
    # Case-insensitive lookup: "book" → "Book", "BOOK" → "Book"
    folder_lower = folder.lower()
    FOLDER_LOWER_MAP = {k.lower(): k for k in FOLDER_TO_TEMPLATE}
    if folder_lower in FOLDER_LOWER_MAP:
        return FOLDER_TO_TEMPLATE[FOLDER_LOWER_MAP[folder_lower]]
    if folder == "sources":
        if len(parts) >= 3:
            type_part = parts[1]
            type_lower = type_part.lower()
            if type_lower in FOLDER_LOWER_MAP:
                return FOLDER_TO_TEMPLATE[FOLDER_LOWER_MAP[type_lower]]
        return "digest-book"
    available = sorted(FOLDER_TO_TEMPLATE.keys())
    match = min(available, key=lambda a: str_distance(folder, a))
    print(f"[detect] Unknown raw folder '{folder}' — treating as '{match}' "
          f"(pass --type to override)", flush=True)
    return FOLDER_TO_TEMPLATE[match]


def is_query_bridge_source(raw_file: Path, config: "Config") -> bool:
    """True iff raw_file is a deep-research research page — ingested directly
    from ``wiki/queries/<slug>.md`` (2026-07-16: the ``raw/queries/`` copy
    step was removed, NashSU ``autoIngest`` parity — query pages are no
    longer duplicated into raw/) or, for pre-2026-07-16 data, a legacy bridge
    copy still sitting under ``raw/queries/``.

    These are not real source documents — the ``wiki/queries/<slug>.md``
    page is the canonical human-readable artifact, so it should not get its
    own ``wiki/sources/queries/`` digest page (Stage 2.6).
    """
    for base in (config.wiki_dir, config.raw_root):
        try:
            rel = raw_file.relative_to(base)
        except ValueError:
            continue
        if len(rel.parts) >= 1 and rel.parts[0].lower() == "queries":
            return True
    return False


def canonical_source_path(raw_file: Path, config: "Config") -> str:
    """The authoritative ``sources:`` frontmatter value for ``raw_file``.

    ``raw/<rel>`` for a normal source under ``config.raw_root``; ``wiki/queries/<rel>``
    for a deep-research page ingested directly from ``wiki/queries/`` (2026-07-16:
    no more ``raw/queries/`` bridge copy — see ``is_query_bridge_source``). Falls
    back to the bare filename for any other path (should not normally happen —
    ``ingest.py``'s CLI gate only accepts these two roots).

    Single source of truth: every place that writes a ``sources:`` field
    (canonical write in ``_ingest_write.py``, the per-page prompt hints in
    Stage 2.4/2.6/2.9, the log.md line in Stage 3.5) must call this — not
    hand-roll an ``f"raw/{rel}"`` string — so they can never drift out of
    sync with each other. A drift would silently defeat
    ``_stage_3_1_canonicalize_sources_field``'s basename-based "already
    present" check (two differently-prefixed strings for the same file both
    have the same basename, so the stale one never gets overwritten).
    """
    try:
        return f"raw/{raw_file.relative_to(config.raw_root)}"
    except ValueError:
        pass
    try:
        return f"wiki/queries/{raw_file.relative_to(config.wiki_dir / 'queries')}"
    except ValueError:
        return raw_file.name


def source_cache_key(raw_file: Path, config: "Config") -> str:
    """The ``ingest-cache.json`` ``entries[]`` key for ``raw_file``.

    Path-based (unlike the content-hash-keyed ``stages.json`` that governs
    re-ingest skip logic — this key never affects whether a source gets
    re-ingested, only ``--delete``/``validate_ingest.py`` bookkeeping).

    A deep-research page ingested from ``wiki/queries/<rel>`` gets the SAME
    key (``queries/<rel>``) a pre-2026-07-16 ``raw/queries/<rel>`` bridge
    copy of the same file would have gotten — this is deliberate, so
    ``--delete`` on a query source ingested before/after the bridge removal
    resolves to one consistent key rather than forking into two formats.
    Must stay in sync with ``_source_lifecycle.py::delete_source``, which
    computes the same key for the delete path.
    """
    try:
        return str(raw_file.relative_to(config.raw_root))
    except ValueError:
        pass
    try:
        return str(Path("queries") / raw_file.relative_to(config.wiki_dir / "queries"))
    except ValueError:
        return raw_file.name


def load_template(template_name: str) -> str:
    skill_dir = Path(__file__).resolve().parent.parent
    tmpl_path = skill_dir / "templates" / f"{template_name}.md"
    if tmpl_path.exists():
        return tmpl_path.read_text(encoding="utf-8")
    return ""


# State ownership moved to ``_progress``.  Keep these re-exports so external
# callers and older stage modules importing from ``_core`` remain compatible.
from _progress import (  # noqa: E402,F401
    ProjectLock,
    clear_progress,
    delete_progress_keys,
    file_sha256,
    get_stage_payload,
    is_stage_done,
    load_cache,
    load_progress,
    load_stages,
    mark_stage_done,
    progress_path,
    save_cache,
    save_progress,
    stages_path,
    unmark_stage_done,
)


# Schema and path ownership moved to ``_schema``.  Re-export the old names to
# preserve imports while letting new stage code depend on the focused module.
from _schema import (  # noqa: E402,F401
    BASE_PAGE_DIRS,
    BASE_TYPE_TO_DIR,
    _ILLEGAL_CHARS_RE,
    is_safe_ingest_path,
    list_existing_slugs,
    load_purpose_md,
    load_schema_md,
    parse_wiki_schema_routing,
    schema_candidate_routes,
    schema_folders,
    schema_prompt_text,
    schema_route_dir,
    source_slug_from_raw_path,
)


# Compatibility exports. New stage modules import these from ``_parse``.
def parse_yaml_block(response: str) -> dict:
    from _parse import parse_yaml_block as _parse_yaml_block

    return _parse_yaml_block(response)


def parse_simple_yaml(text: str):
    from _parse import parse_simple_yaml as _parse_simple_yaml

    return _parse_simple_yaml(text)


def parse_file_blocks(response: str) -> list[tuple[str, str]]:
    from _parse import parse_file_blocks as _parse_file_blocks

    return _parse_file_blocks(response)

# ══════════════════════════════════════════════════════════════════════════════
# Shared utilities (used by multiple stage modules)
# ══════════════════════════════════════════════════════════════════════════════

def slugify(text: str) -> str:
    """Convert a concept/entity name to a kebab-case wiki slug.

    Standardized across all stage modules. Used in 15+ places. Strips the
    same Windows-illegal characters is_safe_ingest_path() rejects (e.g. a
    book-title entity like "...Volume III: Physics-Based Methods" would
    otherwise produce a colon-bearing slug that the FILE-block parser
    silently drops the page for).
    """
    # NFKC-normalize first so full-width CJK punctuation/digits fold to their
    # half-width equivalents before slugging (NashSU wiki-filename.ts parity).
    import unicodedata
    text = unicodedata.normalize("NFKC", text)
    slug = text.lower().replace(" ", "-").replace("/", "-")
    slug = _ILLEGAL_CHARS_RE.sub("", slug)
    # Strip interior punctuation that doesn't belong in slugs: commas, ampersands,
    # periods, semicolons, apostrophes (e.g. "Energy, Work, and Power" ->
    # "energy-work-and-power", "Taylor & Francis Ltd." -> "taylor-francis-ltd",
    # "The Fairmont Press, Inc." -> "the-fairmont-press-inc"). The illegal-char
    # strip above leaves these, producing comma/ampersand-bearing filenames.
    slug = re.sub(r"[,.&;!'`]+", "-", slug)
    # Collapse interior brackets/parentheses (ASCII + full-width) into hyphens.
    # The illegal-char strip above leaves them, and the trailing-edge strip
    # below only removes the LAST one, so "Total Module Power (TMP)" became the
    # malformed "total-module-power-(tmp" (interior "(" kept, trailing ")"
    # stripped). Converting them up front yields a clean "total-module-power-tmp".
    slug = re.sub(r"[()\[\]{}（）【】]+", "-", slug)
    # Keep Unicode letters/digits (CJK, Cyrillic, …) plus ASCII hyphen/underscore;
    # drop everything else (emoji, residual punctuation). NashSU wiki-filename.ts
    # parity: a non-Latin title must NOT collapse to an empty slug. The old
    # ASCII-only edge-strips (^[^a-z0-9]+ / [^a-z0-9]+$) deleted leading/trailing
    # CJK, turning "贴片电阻" into "" (colliding empty slugs) and "电感DCR" into "dcr".
    # The comma/bracket→hyphen substitutions above already ran, so dropping the
    # remaining non-slug chars here preserves intended word boundaries.
    slug = "".join(ch for ch in slug if ch in "-_" or ch.isalnum())
    # Collapse doubled hyphens (from bracket/space substitution) and trim edges.
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug


# (atomic_write lives in _paths.py; re-exported near the top of this module.)


def call_with_retry(fn, max_retries: int = 3, base_wait: float = 1.0, label: str = ""):
    """Compatibility export; new call sites import :mod:`_retry` directly."""
    from _retry import call_with_retry as _call_with_retry

    return _call_with_retry(
        fn,
        max_retries=max_retries,
        base_wait=base_wait,
        label=label,
    )
