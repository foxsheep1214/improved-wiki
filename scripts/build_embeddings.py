#!/usr/bin/env python3
"""Chunk-level embedding index for improved-wiki.

The implementation follows NashSU 0.6.6's embedding pipeline:

* the same Markdown chunker defaults and title/breadcrumb enrichment;
* strict OpenAI-compatible batch response validation with per-input fallback;
* character-safe auto-halving for endpoint "input too long" responses;
* page-scoped replacement during ingest (only pages written by this source);
* a separate full re-index command that prepares everything before overwrite;
* page-level aggregation for vector search; and
* best-effort LanceDB compaction/pruning after successful writes.

Two improved-wiki policies remain intentionally different: the CLI defaults to
local Ollama/bge-m3, and ingest treats Stage 3.7 as mandatory rather than
silently accepting a missing vector backend.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _embedding_chunker import (  # noqa: E402
    DEFAULT_MAX_CHARS,
    DEFAULT_MIN_CHARS,
    DEFAULT_OVERLAP_CHARS,
    DEFAULT_TARGET_CHARS,
    chunk_markdown,
)
from _embedding_store import remove_page_embeddings  # noqa: E402
from _frontmatter import extract_frontmatter_title  # noqa: E402

import lancedb  # noqa: E402


TABLE_NAME = "wiki_chunks"
SKIP_STEMS = {"index", "log", "overview", "purpose", "schema"}
RESERVED_HEADERS = {
    "authorization",
    "content-type",
    "host",
    "content-length",
    "origin",
    "x-goog-api-key",
}


@dataclass(frozen=True)
class EmbeddingConfig:
    endpoint: str
    model: str
    api_key: str = ""
    output_dimensionality: int | None = None
    target_chars: int = DEFAULT_TARGET_CHARS
    overlap_chars: int = DEFAULT_OVERLAP_CHARS
    concurrency: int = 1
    batch_size: int = 1
    extra_headers: dict[str, str] | None = None
    timeout: float = 8.0

    @property
    def max_chars(self) -> int:
        # NashSU passes maxChunkChars as targetChars and retains the chunker's
        # 1500-char hard default unless the target itself is larger.
        return max(DEFAULT_MAX_CHARS, self.target_chars)


class EmbeddingHttpError(RuntimeError):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(
            f"Embedding API HTTP {status}: {body[:200]}"
        )


def _env_positive_int(name: str, default: int, *, maximum: int | None = None) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < 1:
        raise ValueError(f"{name} must be >= 1, got {value}")
    return min(value, maximum) if maximum is not None else value


def _env_optional_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(float(raw))
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric, got {raw!r}") from exc
    return value if value >= 1 else None


def _env_nonnegative_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value}")
    return value


def _env_extra_headers() -> dict[str, str]:
    raw = os.environ.get("EMBEDDING_EXTRA_HEADERS", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("EMBEDDING_EXTRA_HEADERS must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("EMBEDDING_EXTRA_HEADERS must be a JSON object")
    return {
        str(key).strip(): str(item).strip()
        for key, item in value.items()
        if str(key).strip() and str(item).strip()
    }


def get_embed_config() -> tuple[str, str, str]:
    """Return the exact request endpoint, model, and key.

    ``EMBEDDING_ENDPOINT`` follows NashSU and is used verbatim for generic
    OpenAI-compatible gateways.  The legacy improved-wiki
    ``EMBEDDING_BASE_URL`` remains supported by appending ``/embeddings``.
    """
    endpoint = os.environ.get("EMBEDDING_ENDPOINT", "").strip()
    if not endpoint:
        base_url = (
            os.environ.get("EMBEDDING_BASE_URL", "").strip()
            or "http://127.0.0.1:11434/v1"
        )
        endpoint = _append_endpoint_path(base_url, "embeddings")
    return endpoint, os.environ.get("EMBEDDING_MODEL", "bge-m3"), os.environ.get(
        "EMBEDDING_API_KEY", ""
    )


def embedding_config_from_env() -> EmbeddingConfig:
    endpoint, model, api_key = get_embed_config()
    timeout_raw = os.environ.get("EMBEDDING_TIMEOUT_SECONDS", "8").strip()
    try:
        timeout = float(timeout_raw)
    except ValueError as exc:
        raise ValueError(
            f"EMBEDDING_TIMEOUT_SECONDS must be numeric, got {timeout_raw!r}"
        ) from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("EMBEDDING_TIMEOUT_SECONDS must be finite and > 0")
    return EmbeddingConfig(
        endpoint=endpoint.strip(),
        model=model.strip(),
        api_key=api_key.strip(),
        output_dimensionality=_env_optional_int(
            "EMBEDDING_OUTPUT_DIMENSIONALITY"
        ),
        target_chars=_env_positive_int(
            "EMBEDDING_MAX_CHUNK_CHARS", DEFAULT_TARGET_CHARS
        ),
        overlap_chars=_env_nonnegative_int(
            "EMBEDDING_OVERLAP_CHUNK_CHARS", DEFAULT_OVERLAP_CHARS
        ),
        concurrency=_env_positive_int("EMBEDDING_CONCURRENCY", 1, maximum=32),
        batch_size=_env_positive_int("EMBEDDING_BATCH_SIZE", 1, maximum=64),
        extra_headers=_env_extra_headers(),
        timeout=timeout,
    )


def _is_google(config: EmbeddingConfig) -> bool:
    endpoint = config.endpoint.lower()
    return (
        "generativelanguage.googleapis.com" in endpoint
        or ":embedcontent" in endpoint
    )


def _is_doubao_multimodal(config: EmbeddingConfig) -> bool:
    return "doubao-embedding-vision" in config.model.lower()


def _is_volcengine(endpoint: str) -> bool:
    try:
        host = (urllib.parse.urlparse(endpoint).hostname or "").lower()
    except ValueError:
        host = ""
    return (
        host == "volces.com"
        or host.endswith(".volces.com")
        or "volcengine" in host
    )


def _append_endpoint_path(endpoint: str, suffix: str) -> str:
    endpoint = endpoint.strip()
    parsed = urllib.parse.urlparse(endpoint)
    path = parsed.path.rstrip("/")
    lower_path = path.lower()
    target = "/" + suffix.strip("/").lower()
    if lower_path.endswith(target):
        return endpoint
    if lower_path.endswith("/embeddings/multimodal") and target == "/embeddings":
        path = path[: -len("/multimodal")]
    elif lower_path.endswith("/embeddings") and target == "/embeddings/multimodal":
        path += "/multimodal"
    else:
        path += "/" + suffix.strip("/")
    return urllib.parse.urlunparse(parsed._replace(path=path))


def _resolved_endpoint(config: EmbeddingConfig) -> str:
    endpoint = config.endpoint.strip()
    if _is_google(config):
        parsed = urllib.parse.urlparse(endpoint)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = [(key, value) for key, value in query if key.lower() != "key"]
        endpoint = urllib.parse.urlunparse(
            parsed._replace(query=urllib.parse.urlencode(query))
        ).rstrip("?/")
        lower = endpoint.lower()
        if ":batchembedcontents" in lower:
            endpoint = re.sub(
                r":batchembedcontents",
                ":embedContent",
                endpoint,
                flags=re.IGNORECASE,
            )
        elif ":embedcontent" not in lower:
            model = config.model.removeprefix("models/")
            if "/models/" in lower:
                endpoint += ":embedContent"
            else:
                endpoint += f"/models/{model}:embedContent"
        return endpoint
    if _is_volcengine(endpoint):
        suffix = (
            "embeddings/multimodal"
            if _is_doubao_multimodal(config)
            else "embeddings"
        )
        return _append_endpoint_path(endpoint, suffix)
    # NashSU treats a generic OpenAI-compatible endpoint as the exact request
    # URL. Only Volcengine base URLs get provider-specific path completion.
    return endpoint


def _is_local_or_private_endpoint(endpoint: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(endpoint)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    parts = host.split(".")
    if len(parts) != 4:
        return False
    try:
        octets = [int(item) for item in parts]
    except ValueError:
        return False
    return (
        octets[0] == 10
        or (octets[0] == 172 and 16 <= octets[1] <= 31)
        or (octets[0] == 192 and octets[1] == 168)
        or octets[0] == 127
    )


def _headers(config: EmbeddingConfig, endpoint: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if _is_local_or_private_endpoint(endpoint):
        headers["Origin"] = "http://localhost"
    if config.api_key:
        if _is_google(config):
            headers["x-goog-api-key"] = config.api_key
        else:
            headers["Authorization"] = f"Bearer {config.api_key}"
    for name, value in (config.extra_headers or {}).items():
        if name.strip().lower() in RESERVED_HEADERS:
            continue
        if re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", name.strip()):
            headers[name.strip()] = value.strip()
    return headers


def _request_json(
    endpoint: str,
    payload: dict,
    headers: dict[str, str],
    timeout: float,
) -> dict:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = str(exc)
        raise EmbeddingHttpError(exc.code, body) from exc
    except Exception as exc:
        raise RuntimeError(f"Embedding request failed: {exc}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Embedding response parse failed: {exc}: {raw[:200]}"
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeError("Embedding response must be a JSON object")
    return value


def _parse_vector(values, *, context: str) -> list[float]:
    if not isinstance(values, list) or not values:
        raise RuntimeError(f"{context} vector is empty or missing")
    out: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError(f"{context} contains non-number values")
        number = float(value)
        if not math.isfinite(number):
            raise RuntimeError(f"{context} contains non-finite values")
        out.append(number)
    return out


def _parse_single_response(data: dict, config: EmbeddingConfig) -> list[float]:
    if _is_google(config):
        embedding = data.get("embedding")
        values = embedding.get("values") if isinstance(embedding, dict) else None
    elif _is_doubao_multimodal(config):
        payload = data.get("data")
        values = payload.get("embedding") if isinstance(payload, dict) else None
    else:
        payload = data.get("data")
        first = payload[0] if isinstance(payload, list) and payload else None
        values = first.get("embedding") if isinstance(first, dict) else None
    return _parse_vector(values, context="Embedding response")


def _parse_batch_response(data: dict, expected: int) -> list[list[float]]:
    entries = data.get("data")
    if not isinstance(entries, list):
        raise RuntimeError("Embedding batch response missing data array")
    if len(entries) != expected:
        raise RuntimeError(
            f"Embedding batch returned {len(entries)} vectors for {expected} inputs"
        )
    indexed: list[tuple[int, list[float]]] = []
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RuntimeError("Embedding batch response contains a non-object item")
        raw_index = entry.get("index", position)
        if isinstance(raw_index, bool) or not isinstance(raw_index, int):
            raise RuntimeError("Embedding batch response contains an invalid index")
        if raw_index < 0 or raw_index >= expected:
            raise RuntimeError(
                "Embedding batch response contains an out-of-range index"
            )
        vector = _parse_vector(
            entry.get("embedding"), context="Embedding batch response"
        )
        indexed.append((raw_index, vector))
    indexed.sort(key=lambda item: item[0])
    if any(
        indexed[index - 1][0] == indexed[index][0]
        for index in range(1, len(indexed))
    ):
        raise RuntimeError("Embedding batch response contains duplicate indexes")
    dimension = len(indexed[0][1])
    if any(len(vector) != dimension for _, vector in indexed):
        raise RuntimeError(
            "Embedding batch response contains inconsistent vector dimensions"
        )
    return [vector for _, vector in indexed]


def _looks_like_oversize(error: EmbeddingHttpError) -> bool:
    if error.status == 413:
        return True
    body = error.body.lower()
    return any(
        phrase in body
        for phrase in (
            "too long",
            "maximum context",
            "max_tokens",
            "max tokens",
            "context length",
            "token limit",
            "exceeds",
            "input length",
        )
    )


def _single_payload(text: str, config: EmbeddingConfig) -> dict:
    if _is_google(config):
        model = config.model
        if not model.startswith("models/"):
            model = f"models/{model}"
        payload: dict = {
            "model": model,
            "content": {"parts": [{"text": text}]},
        }
        if config.output_dimensionality is not None:
            payload["output_dimensionality"] = config.output_dimensionality
        return payload
    if _is_doubao_multimodal(config):
        return {
            "model": config.model,
            "encoding_format": "float",
            "input": [{"type": "text", "text": text}],
        }
    return {"model": config.model, "input": text}


def _fetch_one(text: str, config: EmbeddingConfig, max_retries: int = 3) -> list[float]:
    endpoint = _resolved_endpoint(config)
    current = text
    attempts = 0
    while True:
        attempts += 1
        try:
            response = _request_json(
                endpoint,
                _single_payload(current, config),
                _headers(config, endpoint),
                config.timeout,
            )
            return _parse_single_response(response, config)
        except EmbeddingHttpError as exc:
            if (
                _looks_like_oversize(exc)
                and attempts <= max_retries
                and len(current) > 1
            ):
                keep = max(1, len(current) // 2)
                current = current[:keep]
                print(
                    "[Embedding] auto-halving after oversize error; "
                    f"retrying at {len(current)} chars "
                    f"({attempts}/{max_retries + 1})"
                )
                continue
            if _looks_like_oversize(exc):
                raise RuntimeError(
                    "Endpoint rejected input even after auto-halving. "
                    "Lower EMBEDDING_MAX_CHUNK_CHARS. "
                    f"{exc}"
                ) from exc
            raise


def _supports_batch(config: EmbeddingConfig) -> bool:
    return not _is_google(config) and not _is_doubao_multimodal(config)


def _fetch_batch(texts: list[str], config: EmbeddingConfig) -> list[list[float]]:
    if not 1 <= len(texts) <= 64:
        raise ValueError("Embedding batch must contain between 1 and 64 inputs")
    endpoint = _resolved_endpoint(config)
    response = _request_json(
        endpoint,
        {"model": config.model, "input": texts},
        _headers(config, endpoint),
        config.timeout,
    )
    return _parse_batch_response(response, len(texts))


def _fetch_group(texts: list[str], config: EmbeddingConfig) -> list[list[float]]:
    if len(texts) > 1 and _supports_batch(config):
        try:
            return _fetch_batch(texts, config)
        except Exception as exc:
            print(
                "[Embedding] Batch request failed; retrying inputs "
                f"individually: {type(exc).__name__}: {exc}"
            )
    return [_fetch_one(text, config) for text in texts]


def embed_with_config(
    texts: list[str],
    config: EmbeddingConfig,
) -> list[list[float]]:
    """Embed all inputs, preserving order and requiring complete coverage."""
    if not texts:
        return []
    if not config.endpoint or not config.model:
        raise RuntimeError("Embedding endpoint and model are required")
    batch_size = max(1, min(64, int(config.batch_size)))
    groups = [
        texts[index:index + batch_size]
        for index in range(0, len(texts), batch_size)
    ]
    results: list[list[list[float]] | None] = [None] * len(groups)
    workers = max(1, min(32, int(config.concurrency), len(groups)))
    if workers == 1:
        for index, group in enumerate(groups):
            results[index] = _fetch_group(group, config)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_fetch_group, group, config): index
                for index, group in enumerate(groups)
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()
    flattened = [
        vector
        for group in results
        if group is not None
        for vector in group
    ]
    if len(flattened) != len(texts):
        raise RuntimeError(
            f"Embedding returned {len(flattened)} vectors for {len(texts)} inputs"
        )
    dimension = len(flattened[0])
    if dimension == 0 or any(len(vector) != dimension for vector in flattened):
        raise RuntimeError("Embedding vectors have inconsistent dimensions")
    return flattened


def embed_texts(texts, base_url, model, api_key, timeout=8):
    """Backward-compatible strict embedding helper used by dedup."""
    config = replace(
        embedding_config_from_env(),
        endpoint=base_url,
        model=model,
        api_key=api_key,
        batch_size=min(64, max(1, len(texts))),
        timeout=timeout,
    )
    return embed_with_config(list(texts), config)


def enrich_for_embedding(title: str, heading_path: str, chunk_text: str) -> str:
    parts = [
        value.strip()
        for value in (title, heading_path, chunk_text)
        if value and value.strip()
    ]
    return "\n\n".join(parts)


def _safe_page_path(raw_path: str) -> Path:
    raw = Path(raw_path).expanduser()
    if raw.is_absolute():
        candidate = raw.resolve()
    elif raw.parts and raw.parts[0] == "wiki":
        candidate = (Path(ROOT) / raw).resolve()
    else:
        candidate = (Path(WIKI) / raw).resolve()
    wiki = Path(WIKI).resolve()
    try:
        candidate.relative_to(wiki)
    except ValueError as exc:
        raise ValueError(f"Embedding page escapes wiki/: {raw_path}") from exc
    return candidate


def collect_pages(page_paths: list[str] | None = None) -> list[dict]:
    from _paths import WIKI_ARTIFACT_DIRS, iter_wiki_pages

    pages: list[dict] = []
    if page_paths is None:
        candidates = [
            (Path(WIKI) / rel_path, rel_path.replace(os.sep, "/"), content)
            for rel_path, content in iter_wiki_pages(
                Path(WIKI), skip_dirs=WIKI_ARTIFACT_DIRS
            )
        ]
    else:
        candidates = []
        seen: set[Path] = set()
        for raw_path in page_paths:
            candidate = _safe_page_path(raw_path)
            if candidate in seen or not candidate.is_file():
                continue
            seen.add(candidate)
            rel_path = candidate.relative_to(Path(WIKI).resolve()).as_posix()
            if any(part in WIKI_ARTIFACT_DIRS for part in Path(rel_path).parts[:-1]):
                continue
            candidates.append(
                (candidate, rel_path, candidate.read_text(encoding="utf-8"))
            )

    for _absolute, rel_path, content in candidates:
        stem = Path(rel_path).stem
        if stem in SKIP_STEMS:
            continue
        pages.append(
            {
                # Keep improved-wiki's path-derived id to prevent collisions
                # between identical stems in different schema directories.
                "page_id": rel_path[:-3],
                "path": rel_path,
                "title": extract_frontmatter_title(content) or stem,
                "content": content,
            }
        )
    pages.sort(key=lambda page: page["path"])
    return pages


def build_chunks(
    pages: list[dict],
    *,
    target_chars: int | None = None,
    overlap_chars: int | None = None,
) -> list[dict]:
    target = target_chars if target_chars is not None else globals().get(
        "TARGET_CHARS", DEFAULT_TARGET_CHARS
    )
    overlap = overlap_chars if overlap_chars is not None else globals().get(
        "OVERLAP_CHARS", DEFAULT_OVERLAP_CHARS
    )
    chunks: list[dict] = []
    for page in pages:
        content = page.get("content", page.get("body", ""))
        for chunk in chunk_markdown(
            content,
            target_chars=target,
            max_chars=max(DEFAULT_MAX_CHARS, target),
            min_chars=DEFAULT_MIN_CHARS,
            overlap_chars=overlap,
        ):
            embed_text = enrich_for_embedding(
                page["title"], chunk.heading_path, chunk.text
            )
            sha = hashlib.sha256(embed_text.encode("utf-8")).hexdigest()[:16]
            chunks.append(
                {
                    "chunk_id": f"{page['page_id']}#{chunk.index}",
                    "page_id": page["page_id"],
                    "chunk_index": chunk.index,
                    "chunk_text": chunk.text,
                    "embed_text": embed_text,
                    "heading_path": chunk.heading_path,
                    "title": page["title"],
                    "path": page["path"],
                    "text_sha16": sha,
                    "oversized": chunk.oversized,
                }
            )
    return chunks


def _rows_for_chunks(chunks: list[dict], vectors: list[list[float]]) -> list[dict]:
    if len(chunks) != len(vectors):
        raise RuntimeError(
            f"Cannot index {len(chunks)} chunks with {len(vectors)} vectors"
        )
    rows = []
    for chunk, vector in zip(chunks, vectors):
        rows.append(
            {
                "chunk_id": chunk["chunk_id"],
                "page_id": chunk["page_id"],
                "chunk_index": chunk["chunk_index"],
                "chunk_text": chunk["chunk_text"],
                "heading_path": chunk["heading_path"] or "",
                "title": chunk["title"] or "",
                "path": chunk["path"],
                "vector": vector,
            }
        )
    return rows


def _compact_and_prune_table(table):
    table.optimize(
        cleanup_older_than=timedelta(seconds=0),
        delete_unverified=False,
    )


def _best_effort_compact_and_prune(table):
    try:
        _compact_and_prune_table(table)
    except Exception as exc:
        print(
            "⚠ LanceDB compact/prune failed after a successful index write "
            f"({type(exc).__name__}: {exc}). The current index remains usable; "
            "run the `compact` command later."
        )
        return False
    print("✓ LanceDB compacted; verified historical versions pruned")
    return True


def _open_table(db):
    try:
        return db.open_table(TABLE_NAME)
    except Exception:
        return None


def _table_vector_dimension(table) -> int | None:
    if table is None:
        return None
    try:
        vector_type = table.schema.field("vector").type
        return int(vector_type.list_size)
    except Exception:
        return None


def _page_filter(page_id: str) -> str:
    return "page_id = '" + page_id.replace("'", "''") + "'"


def _replace_page_rows(db, page_id: str, rows: list[dict]):
    if not rows:
        return None
    table = _open_table(db)
    incoming_dim = len(rows[0]["vector"])
    if table is not None:
        existing_dim = _table_vector_dimension(table)
        if existing_dim is not None and existing_dim != incoming_dim:
            raise RuntimeError(
                f"Embedding dimension changed ({existing_dim} → {incoming_dim}). "
                "Run the full `embed` command to rebuild the index."
            )
        table.delete(_page_filter(page_id))
        table.add(rows)
    else:
        table = db.create_table(TABLE_NAME, rows)
    actual = table.count_rows(_page_filter(page_id))
    if actual != len(rows):
        raise RuntimeError(
            f"LanceDB page coverage mismatch for {page_id}: "
            f"expected {len(rows)}, found {actual}"
        )
    return table


def _embed_chunks(chunks: list[dict], config: EmbeddingConfig) -> list[dict]:
    if not chunks:
        return []
    started = time.time()
    vectors = embed_with_config(
        [chunk["embed_text"] for chunk in chunks],
        config,
    )
    rows = _rows_for_chunks(chunks, vectors)
    print(
        f"  Embedded {len(rows)}/{len(chunks)} chunks "
        f"in {time.time() - started:.1f}s"
    )
    return rows


def cmd_upsert():
    pages = collect_pages(ARGS.page)
    print(f"Pages to replace: {len(pages)}")
    if not pages:
        print("✓ No embeddable pages in this write set")
        return
    db = lancedb.connect(LANCE_DIR)
    updated = 0
    last_table = None
    for page in pages:
        chunks = build_chunks(
            [page],
            target_chars=CONFIG.target_chars,
            overlap_chars=CONFIG.overlap_chars,
        )
        if not chunks:
            print(f"  Empty page left unchanged: {page['path']}")
            continue
        rows = _embed_chunks(chunks, CONFIG)
        last_table = _replace_page_rows(db, page["page_id"], rows)
        updated += 1
        print(
            f"✓ Replaced {page['path']}: {len(rows)}/{len(chunks)} chunks"
        )
    if last_table is not None:
        _best_effort_compact_and_prune(last_table)
    print(f"✓ Incremental embedding complete: {updated}/{len(pages)} pages")


def cmd_embed():
    """Full re-index: prepare every vector before replacing the live table."""
    pages = collect_pages()
    print(f"Pages: {len(pages)}")
    chunks = build_chunks(
        pages,
        target_chars=CONFIG.target_chars,
        overlap_chars=CONFIG.overlap_chars,
    )
    print(f"Chunks: {len(chunks)}")
    db = lancedb.connect(LANCE_DIR)
    existing = _open_table(db)
    existing_rows = existing.count_rows() if existing is not None else 0
    if not chunks:
        if existing_rows > 0:
            raise RuntimeError(
                "Wiki has no embeddable chunks but the current index contains "
                f"{existing_rows} rows; existing index was left unchanged."
            )
        print("✓ No embeddable chunks and no existing index")
        return
    rows = _embed_chunks(chunks, CONFIG)
    if len(rows) != len(chunks):
        raise RuntimeError(
            f"Full rebuild incomplete: {len(rows)}/{len(chunks)} chunks prepared"
        )
    table = db.create_table(TABLE_NAME, rows, mode="overwrite")
    actual = table.count_rows()
    if actual != len(chunks):
        raise RuntimeError(
            f"LanceDB full-index coverage mismatch: expected {len(chunks)}, "
            f"found {actual}"
        )
    print(f"✓ LanceDB: {actual}/{len(chunks)} chunks → {LANCE_DIR}/{TABLE_NAME}")
    _best_effort_compact_and_prune(table)


def cmd_compact():
    db = lancedb.connect(LANCE_DIR)
    table = _open_table(db)
    if table is None:
        raise RuntimeError("LanceDB table not found. Run `embed` first.")
    rows_before = table.count_rows()
    _compact_and_prune_table(table)
    rows_after = db.open_table(TABLE_NAME).count_rows()
    if rows_after != rows_before:
        raise RuntimeError(
            "LanceDB row count changed during compact/prune: "
            f"{rows_before} → {rows_after}"
        )
    print(
        "✓ LanceDB compacted; verified historical versions pruned "
        f"({rows_after} rows preserved)"
    )


def cmd_delete():
    result = remove_page_embeddings(Path(ROOT), ARGS.page, strict=True)
    if not result["index_present"]:
        print("✓ No LanceDB wiki_chunks table; nothing to delete")
        return
    print(
        "✓ Deleted page embeddings: "
        f"{result['matched_pages']}/{result['requested_pages']} page(s), "
        f"{result['rows_removed']} chunk row(s)"
    )


def _aggregate_page_results(frame, top_k: int) -> list[dict]:
    by_page: dict[str, list[dict]] = {}
    for _, row in frame.iterrows():
        distance = float(row.get("_distance", 0))
        score = 1.0 / (1.0 + distance)
        item = {
            "page_id": str(row.get("page_id", "")),
            "path": str(row.get("path", "")),
            "title": str(row.get("title", "") or ""),
            "chunk_text": str(row.get("chunk_text", "") or ""),
            "heading_path": str(row.get("heading_path", "") or ""),
            "score": score,
        }
        by_page.setdefault(item["page_id"], []).append(item)
    ranked = []
    for page_id, chunks in by_page.items():
        chunks.sort(key=lambda item: item["score"], reverse=True)
        top = chunks[0]["score"]
        tail = sum(item["score"] for item in chunks[1:])
        blended = top + min(tail * 0.3, max(0.0, 1.0 - top))
        ranked.append(
            {
                "page_id": page_id,
                "path": chunks[0]["path"],
                "title": chunks[0]["title"],
                "score": blended,
                "matched_chunks": chunks[:3],
            }
        )
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[:top_k]


def cmd_search():
    db = lancedb.connect(LANCE_DIR)
    table = _open_table(db)
    if table is None:
        raise RuntimeError("LanceDB table not found. Run `embed` first.")
    query_vector = embed_with_config([ARGS.query], replace(CONFIG, batch_size=1))[0]
    raw_limit = max(ARGS.top_k * 3, 30)
    frame = table.search(query_vector).limit(raw_limit).to_pandas()
    ranked = _aggregate_page_results(frame, ARGS.top_k)
    print(f"\n=== Top {len(ranked)} semantic page matches ===\n")
    for index, page in enumerate(ranked, 1):
        print(
            f"#{index}  [{page['path']}]  {page['title']}\n"
            f"     score={page['score']:.3f}"
        )
        for chunk in page["matched_chunks"]:
            snippet = chunk["chunk_text"][:200].replace("\n", " ")
            print(
                f"     chunk={chunk['score']:.3f} "
                f"{chunk['heading_path']}: {snippet}..."
            )
        print()


def cmd_stats():
    db = lancedb.connect(LANCE_DIR)
    table = _open_table(db)
    if table is None:
        print("✗ LanceDB table not found")
        return
    count = table.count_rows()
    print(f"LanceDB table: {len(table.schema.names)} columns, {count} rows")
    for name in table.schema.names[:10]:
        print(f"  - {name}: {table.schema.field(name).type}")
    legacy = Path(RUNTIME_DIR) / "embed-cache.json"
    if legacy.exists():
        print(
            f"Legacy cache (unused by NashSU-aligned flow): "
            f"{legacy} ({legacy.stat().st_size} bytes)"
        )


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project", default=os.environ.get("IMPROVED_WIKI_PROJECT")
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=None,
        help=(
            "NashSU maxChunkChars setting (used as targetChars; default 1000, "
            "hard floor remains 1500)"
        ),
    )
    parser.add_argument("--overlap-chars", type=int, default=None)
    parser.add_argument("--model", default=None, help="Override EMBEDDING_MODEL")
    parser.add_argument(
        "--base-url",
        default=None,
        help="Legacy base URL override; /embeddings is appended",
    )
    parser.add_argument(
        "--endpoint",
        default=None,
        help="Exact NashSU-style embedding request endpoint override",
    )
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "embed",
        help="Safely prepare and rebuild the complete current wiki index",
    )
    upsert = sub.add_parser(
        "upsert",
        help="Replace embeddings only for the named wiki pages",
    )
    upsert.add_argument(
        "--page",
        action="append",
        required=True,
        help="Absolute, project-relative, or wiki-relative Markdown path",
    )
    delete = sub.add_parser(
        "delete",
        help="Delete embeddings for the named wiki pages without re-embedding",
    )
    delete.add_argument(
        "--page",
        action="append",
        required=True,
        help="Absolute, project-relative, or wiki-relative Markdown path",
    )
    sub.add_parser("compact", help="Compact LanceDB and prune verified versions")
    search = sub.add_parser("search", help="Vector search with page aggregation")
    search.add_argument("--query", required=True)
    search.add_argument("--top-k", type=int, default=5)
    sub.add_parser("stats", help="Show LanceDB table info")
    args = parser.parse_args()
    if not args.project:
        parser.error("--project required (or set IMPROVED_WIKI_PROJECT)")
    return args


def _init_cli():
    global ARGS, ROOT, WIKI, RUNTIME_DIR, LANCE_DIR
    global CONFIG, TARGET_CHARS, OVERLAP_CHARS
    ARGS = parse_args()
    ROOT = str(Path(ARGS.project).expanduser().resolve())
    WIKI = str(Path(ROOT) / "wiki")
    from _paths import detect_runtime_dir

    RUNTIME_DIR = str(detect_runtime_dir(Path(ROOT)))
    LANCE_DIR = str(Path(RUNTIME_DIR) / "lancedb")
    CONFIG = embedding_config_from_env()
    if ARGS.model:
        CONFIG = replace(CONFIG, model=ARGS.model.strip())
    if ARGS.endpoint:
        CONFIG = replace(CONFIG, endpoint=ARGS.endpoint.strip())
    elif ARGS.base_url:
        CONFIG = replace(
            CONFIG,
            endpoint=_append_endpoint_path(
                ARGS.base_url.strip(), "embeddings"
            ),
        )
    if ARGS.max_chars is not None:
        CONFIG = replace(CONFIG, target_chars=max(1, ARGS.max_chars))
    if ARGS.overlap_chars is not None:
        CONFIG = replace(CONFIG, overlap_chars=max(0, ARGS.overlap_chars))
    if ARGS.concurrency is not None:
        CONFIG = replace(
            CONFIG, concurrency=max(1, min(32, ARGS.concurrency))
        )
    if ARGS.batch_size is not None:
        CONFIG = replace(CONFIG, batch_size=max(1, min(64, ARGS.batch_size)))
    TARGET_CHARS = CONFIG.target_chars
    OVERLAP_CHARS = CONFIG.overlap_chars
    Path(LANCE_DIR).mkdir(parents=True, exist_ok=True)
    Path(RUNTIME_DIR).mkdir(parents=True, exist_ok=True)
    print(
        f"[embedding] endpoint: {_resolved_endpoint(CONFIG)}  "
        f"model: {CONFIG.model}  target/max/overlap: "
        f"{CONFIG.target_chars}/{CONFIG.max_chars}/{CONFIG.overlap_chars}  "
        f"concurrency/batch: {CONFIG.concurrency}/{CONFIG.batch_size}"
    )


if __name__ == "__main__":
    _init_cli()
    if ARGS.command == "embed":
        cmd_embed()
    elif ARGS.command == "upsert":
        cmd_upsert()
    elif ARGS.command == "delete":
        cmd_delete()
    elif ARGS.command == "compact":
        cmd_compact()
    elif ARGS.command == "search":
        cmd_search()
    elif ARGS.command == "stats":
        cmd_stats()
