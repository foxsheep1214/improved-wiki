# Scan evidence and local region review

Use this for scanned-PDF formula/table uncertainty, missing layout provenance,
or reviewing a second transcription. The normal extractor remains MinerU
`hybrid-engine` / medium; captions remain a separate configured VLM call.

## Durable originals

Every newly parsed chunk saves the exact MinerU result JSON (including image
bytes), content list, original table HTML, and a region manifest under:

`<runtime>/scan-evidence/<source-id>/<source-sha256>/<page-range>-<response-id>/`

The full source identity and SHA-256 distinguish same-named files and revisions.
The manifest records PDF page numbers (one based), page indices (zero based),
MinerU bounding boxes (normalized to 0–1000), original text and crop errors.
Table regions and detected anomalies receive 300-DPI source crops. Unknown page
locations remain unknown; they are not inferred from nearby text. Source bytes
are not duplicated wholesale: the exact response, table crops and anomaly crops
are retained; another crop can be made only while the source SHA-256 matches.

`extract-tmp/.../_scan_evidence.json` points to the durable manifest. Cleaning
extract-tmp does not remove the originals. Old cached chunks are not silently
reparsed or claimed to have this evidence. HTML tables containing rowspan or
colspan are retained as HTML in digest text; simple tables still convert to
Markdown. Original HTML remains available for either form.

## Structured captions

New batch captions use one JSON response per image:

- `summary`: a short description in the configured output language;
- `visible_text`, `axes`, `legend`, `relationships`, `formulas`, `uncertainties`:
  arrays of strings, with original labels and LaTeX transcriptions preserved.

The `.caption.txt` contains both summary and details, so existing digest and
embedding consumers receive the evidence, not just a short caption. The sibling
`.caption.json` keeps structured observations, image/text hashes, model,
language and source context. It labels model observations as unverified; it is
not an accuracy certificate. New pairs are checked against the actual image and
caption bytes on resume. Existing text-only captions remain usable; this change
does not force a corpus-wide recaption.

Malformed JSON and output-token truncation use the existing retry/failover
policy. Uncertain individual labels do not count as provider refusal. The
summary is short, but evidence arrays are not restricted to 2–4 sentences.

## Targeted second opinions

After MinerU releases its global lock, freshly extracted sources check equation
and table regions for concrete anomalies: empty transcription, unreadable
markers, unbalanced LaTeX braces, missing/unclosed HTML tables, or unequal cell
counts in tables without spans. These checks cannot detect every semantic error
(for example, a plausible but wrong bar versus arrow).

Only flagged crops are sent for independent transcription. The configured
caption fallback is preferred (often local Ollama); without one, the configured
primary is used. No provider is installed or switched. Calls use the global
caption lock and at most eight regions per chunk/run; other regions remain
explicitly deferred. The second opinion never sees the first transcription.

`region-review.json` stores original and secondary transcriptions side by side.
Successful calls remain `needs-review`, even if both models agree. Failed calls
are `unavailable`; quota-limited regions are `deferred`. A warning with page and
evidence locations accompanies text passed to the digest. Original Markdown,
formula text and HTML are never overwritten by these opinions. This review is
an evidence aid, not an automatic correction or ingest-acceptance gate.

For an error noticed visually, inspect a manifest and explicitly request a
region by ID. The apply command acquires the project's maintenance lock; it
does not modify wiki pages or original OCR:

```bash
python3 "$SKILL_DIR/scripts/scan_review.py" --root /path/to/wiki \
  --manifest /path/to/wiki/.llm-wiki/scan-evidence/.../manifest.json
python3 "$SKILL_DIR/scripts/scan_review.py" --root /path/to/wiki \
  --manifest /path/to/wiki/.llm-wiki/scan-evidence/.../manifest.json \
  --region r0004 --apply
```

Use the actual runtime path from the pointer; `.llm-wiki` is only an example.
Re-running skips completed second opinions and retries unavailable/deferred
regions (increase `--limit` if appropriate). Exit 2 means some requested regions
are still unavailable/deferred. Human validation is still required to resolve a
disagreement; never apply an opinion solely because a second model produced it.
