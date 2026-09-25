"""Per-source evidence ledger kept after ingest.

Stage 2.2 extracts, per chunk, claims with an evidence anchor ("§2.7.3;
Figure 2.31") and confidence, anchored source quotes, formulas, and verbatim
tables. Stage 2.4 folds them into pages whose only provenance is the
page-level ``sources:`` list, and Stage 3.6 deletes the progress file that
held them. This module writes those fields to
``<runtime>/evidence/<source-id>-<hash16>-<run-id>.json`` just before that deletion, so
retrieval can cite the section/figure/table a statement came from.

No LLM call: the ledger is a projection of analysis the ingest already paid
for. It is derived state — each source version/run keeps its own record; sources ingested before the
ledger existed have none.
"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

from _paths import atomic_write

LEDGER_SCHEMA_VERSION = 2
EVIDENCE_FIELDS = ("claims", "source_quotes", "formulas", "structured_data")
_NON_KNOWLEDGE_REFS = ("wiki/REVIEW/",)
_NON_KNOWLEDGE_FILES = {"wiki/index.md", "wiki/log.md", "wiki/overview.md"}


def evidence_dir(config) -> Path:
    return Path(config.runtime_dir) / "evidence"


def write_evidence_ledger(
    config,
    source: str,
    source_hash: str,
    chunk_analyses: list[dict],
    written_refs: list[str],
    *,
    run_id: str | None = None,
) -> Path | None:
    """Write the ledger for one source; return its path.

    ``source`` is the wiki-root-relative raw path (the ingest cache key).
    Returns None and leaves any existing ledger alone when no analysis is
    available (e.g. a resume after the progress file was already cleared).
    """
    chunks = []
    for position, analysis in enumerate(chunk_analyses or []):
        if not isinstance(analysis, dict):
            continue
        kept = {field: analysis[field] for field in EVIDENCE_FIELDS
                if analysis.get(field)}
        if kept:
            chunks.append({"chunk_index": analysis.get("_chunk_index", position),
                           **kept})
    if not chunks:
        return None
    pages = sorted(
        ref for ref in set(written_refs)
        if ref.startswith("wiki/") and ref not in _NON_KNOWLEDGE_FILES
        and not ref.startswith(_NON_KNOWLEDGE_REFS)
    )
    if run_id is None:
        manifest_path = (Path(getattr(config, 'progress_dir',
                                     Path(config.runtime_dir) / 'ingest-progress'))
                         / f'{source_hash[:16]}.task.json')
        manifest = (json.loads(manifest_path.read_text(encoding='utf-8'))
                    if manifest_path.exists() else {})
        run_id = str((manifest.get('run') or {}).get('run_id') or '')
    record = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "source": source,
        "source_hash": source_hash,
        "run_id": run_id,
        "source_page": next((p for p in pages if p.startswith("wiki/sources/")), ""),
        "pages": pages,
        "chunks": chunks,
    }
    identity = hashlib.sha256(source.encode('utf-8')).hexdigest()[:16]
    run_key = hashlib.sha256(run_id.encode('utf-8')).hexdigest()[:16]
    out = evidence_dir(config) / f"{identity}-{source_hash[:16]}-{run_key}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(out, json.dumps(record, ensure_ascii=False, indent=1))
    return out


def load_ledgers(runtime_dir: Path) -> list[dict]:
    """Every readable ledger under ``<runtime>/evidence``."""
    records = []
    for path in sorted((Path(runtime_dir) / "evidence").glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict) and record.get("source"):
            records.append(record)
    return records


def evidence_items(record: dict):
    """Flatten a ledger into (kind, text, detail) items, chunk order kept."""
    for chunk in record.get("chunks", []):
        index = chunk.get("chunk_index")
        for claim in chunk.get("claims") or []:
            if isinstance(claim, dict):
                yield {"kind": "claim", "chunk_index": index,
                       "text": str(claim.get("claim", "")),
                       "evidence": str(claim.get("evidence", "")),
                       "confidence": str(claim.get("confidence", ""))}
        quotes = chunk.get("source_quotes")
        if quotes:
            yield {"kind": "source_quotes", "chunk_index": index,
                   "text": quotes if isinstance(quotes, str)
                   else json.dumps(quotes, ensure_ascii=False)}
        for formula in chunk.get("formulas") or []:
            if isinstance(formula, dict):
                yield {"kind": "formula", "chunk_index": index,
                       "text": str(formula.get("formula", "")),
                       "meaning": str(formula.get("meaning", ""))}
        for table in chunk.get("structured_data") or []:
            if isinstance(table, dict):
                yield {"kind": "structured_data", "chunk_index": index,
                       "text": str(table.get("content", "")),
                       "label": str(table.get("label", ""))}
