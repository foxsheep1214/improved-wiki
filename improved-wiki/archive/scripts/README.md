# archive/scripts/ — retired one-shot scripts

Archived 2026-07-02 (redundancy audit). Not on any active call path; kept for
reference only.

| Script | Why archived |
|--------|--------------|
| `normalize_page_types.py` (+test) | One-shot migration (role-as-type → `type: entity`, strip `role:`). `--check` confirmed 0 remaining pages on HardwareWiki + RadarWiki. |
| `reingest_batch.py` | One-shot re-ingest driver with a hardcoded 26-book list for the 2026-06-25 batch. |
| `repair_wiki.py` / `repair_stage_3_2.py` | Rebuilt the OLD caption-table `## Embedded Images` format. Current Stage 3.2 (`_stage_3_2_inject_images.py`) is the live repair path — re-running it strips and re-injects. The last 2 old-format source pages were migrated 2026-07-02. |
