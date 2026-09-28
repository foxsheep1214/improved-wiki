# MinerU runtime checks

MinerU 4.x uses a local `mineru.parser.api_server` service, `/v1/health`,
uploads, parse jobs and a self-contained ZIP result. The wiki explicitly requests
`tier=standard`, `ocr_mode=auto`, and every page in each PDF chunk. Image analysis
is disabled in this service because Stage 1.3 performs the separate caption call.

The V1 adapter retains native middle JSON, model output, structured content,
images and API job metadata. It derives the existing content-list contract using
MinerU's public renderer, preserving zero-based page indices and normalized
0–1000 bounding boxes. Partial/failed/canceled jobs are not accepted as complete.
Timeout/error paths request cancellation before the existing chunk retry policy.

The 3.x fallback remains available for rollback, with `mineru.cli.fast_api` and
`/file_parse`. Service selection checks the actual interpreter's package version.
Each OCR run starts its own local service on a free port. It never reuses an
unidentified service on the configured/default port: a healthy endpoint does
not prove its upload directory, output directory, model readiness, or content
quality. An explicitly configured occupied `MINERU_API_PORT` fails visibly.

MinerU's upload store (`blobs/`) and native API output are placed in a unique
`/tmp/codex-work/improved-wiki-mineru/run-*` directory, then removed after the
owned service stops. The wiki's resumable OCR page/chunk state remains in its
runtime directory; old `.llm-wiki/mineru-api-out` data is read only as a legacy
media recovery fallback. Do not set the API upload directory to the wiki root
or remove an active run's scratch directory.

Check the interpreter actually launching the service:

```bash
"$HOME/.venv/bin/python" -c 'import importlib.metadata as m; print(m.version("mineru"))'
```

An optional `IMPROVED_WIKI_MINERU_PYTHON` selects both the server and native
renderer interpreter. Otherwise `~/.venv/bin/python3` is used when present,
then the driver interpreter. V1 rendering runs in that environment over a
JSON stdin/stdout subprocess; the wiki driver need not import MinerU. An
explicit invalid interpreter fails visibly instead of silently falling back.
4.x configuration uses `$MINERU_HOME/config.yaml` or `MINERU_CONFIG`; it does not
read old `mineru.json`. Use the model verification command for the selected small
backend and VLM engine. Do not equate a cached 3.x model directory with 4.x readiness.

On Apple Silicon, explicitly selecting `model.vlm.engine: mlx` can reuse the
unquantized MinerU VLM snapshot. Install the MLX extra required by
`mineru-vl-utils`, not just the base MinerU package: 2.0.5 requires
`mlx-vlm>=0.7.0,<0.8.0`. Older MLX packages can pass a base dependency check yet
fail only when a real scan reaches the VLM. Resolve Gradio and Starlette together
with MLX, then validate inference; do not bypass their dependency constraints.

Before upgrading, record versions, configuration, active workers, effective model
revisions and a representative result. Validate dependencies and a real scanned
page's formulas, table, page mapping and image output before switching production.
Retain the previous environment for rollback and stop temporary validation servers.
Existing source caches are not automatically reparsed by a software upgrade.

See `scanned-pdf-ocr-pipeline.md` and `scan-evidence-review.md` for data flow and
artifacts; caption and embedding providers remain separate dependencies.
