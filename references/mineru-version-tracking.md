# MinerU runtime checks

The supported extraction route is a local `mineru.cli.fast_api` server and
`/file_parse` with `backend=hybrid-engine`, `parse_method=auto`. Every PDF uses
this path. PyMuPDF's dry-run text-density sample never selects an OCR backend.

An environment observation is not a version pin or a claim about the newest
release: the audited host reported MinerU **3.4.5 on 2026-09-25**, replacing the
old 3.4.0 snapshot. Check the interpreter actually launching the service:

```bash
# Substitute the service's actual environment, if different.
"$HOME/.venv/bin/python" -c 'import importlib.metadata as m; print(m.version("mineru"))'
```

Before an upgrade, record that interpreter/package version, active backend,
configured model paths/revisions and one representative PDF result. Afterward,
verify `/file_parse` text, page mapping, tables/formulas and image output before
resuming production ingestion. Package version alone does not prove the active
model revision, cache completeness or service compatibility.

Older MinerU 3.0–3.2 deployments used `hybrid-auto-engine` / `vlm-auto-engine`.
This skill does not guess aliases or silently switch backends: configure a
compatible local service and investigate an unknown-backend response. Historical
CLI 3.4.0 failures do not establish a limitation of every later version.

See `scanned-pdf-ocr-pipeline.md` for service calls/checkpoints and
`image-caption-strategy.md` for the separate caption-provider configuration.
