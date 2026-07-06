---
name: llm-wiki
description: "Query the user's LLM Wiki knowledge base (the LLM Wiki desktop app at 127.0.0.1:19828 — NOT Obsidian, Notion, Apple Notes, Logseq, or any other PKM tool). Trigger ONLY when the user explicitly names LLM Wiki, says 'my wiki', 'my 知识库 / 知识库 / knowledge base', or asks things like 'what does my wiki say about X', 'read wiki page Y', 'show my wiki graph / 知识图谱', 'search in my LLM Wiki project', 'rescan my wiki sources / 重新索引', or names a wiki project by ID. DO NOT trigger on generic 'search my notes', 'find in my notebook', 'check my Obsidian', etc. — those belong to other tools the user may have installed. Covers wiki page search, file listing, content read, knowledge graph navigation, source rescan against the running LLM Wiki desktop app, AND triaging the wiki/queries/ backlog and the .llm-wiki/review.json confirm items. Read-only except for source rescan and query/review triage."
tags: []
related_skills: [improved-wiki, karpathy-llm-wiki, mineru-document-parsing]
---
# LLM Wiki Local API Skill

Talk to the user's locally-running LLM Wiki app over its built-in HTTP API. This is a **standard JSON API** — call it directly with whatever HTTP tool is already in your environment (`curl`, `fetch`, `requests`, `http` middleware, etc.). No client library to install, no SDK to learn.

Treat the wiki as a **private, structured knowledge base** the user has been curating: pages live as `wiki/**.md`, raw documents under `raw/sources/`, wikilinks form a graph.

## When to invoke

Invoke **only** when the user is clearly referring to **LLM Wiki** specifically — by app name, by `wiki` framing, or by `知识库` framing. Concretely:
Invoke **only** when the user is clearly referring to **LLM Wiki** specifically — by app name, by `wiki` framing, or by `知识库` framing. Concretely:
- asks a question framed as "what does my **wiki** / my **knowledge base** / 我的**知识库** / **LLM Wiki** say about X"
- asks to "search **my wiki** / **LLM Wiki** project / 我的**知识库** for X"
- references a **wiki page** by stem / title and wants to read or cross-link
- asks to "show my **wiki graph / 知识图谱**"
- has just added or edited files under the LLM Wiki **source folder** and wants ingest re-run / **重新索引**
- says "use **my wiki** for context" / "ground your answer in **my wiki**" / "check **my LLM Wiki**"
- names a wiki project (by ID, by absolute path, or by `current`)
- asks about wiki state/cleanup: "wiki 有多少问题" / "review 里 X 怎么办" / "queries/ 怎么处理" / "fix the broken links" / "lint.json 怎么清"
- asks **how the ingest pipeline works internally** / "消化过程分几个阶段" / "what does Step N do" / "why does the aggregate-repair stage..." / "what's the long-source chunk loop" — load `references/ingest-pipeline-stages.md` for the actual v0.4.23 stage taxonomy sourced from upstream `src/lib/ingest.ts`
- asks to **identify / reverse-engineer an unfamiliar packaged LLM Wiki binary** back to its open-source upstream — same reference covers the fingerprint recipe (Bundle ID → Info.plist → app-state.json → strings)

**Do NOT invoke when the user says:**

- "search **my notes**" without further qualification — likely Obsidian / Apple Notes / Notion / Logseq / Bear / etc.
- "find in **my notebook**" — likely Jupyter / OneNote / Notability
- "check **my Obsidian / Notion / Roam / Logseq vault**" — explicitly a different tool
- "look up **my Anki / Readwise / Pocket**" — different tool
- "search **my files / my Documents folder**" — generic filesystem, not the wiki
- general world knowledge, current events, or anything the user clearly wants from the open web

When in doubt about which knowledge tool the user means, ask: *"Do you mean your LLM Wiki specifically, or another tool?"* — don't silently call the LLM Wiki API on what might be an Obsidian vault.

## Quick start

The whole API is plain HTTP + JSON. The fastest path:

```bash
BASE=http://127.0.0.1:19828
TOKEN="${LLM_WIKI_API_TOKEN:-<paste-from-Settings>}"

# 1. probe state — no auth needed
curl -s $BASE/api/v1/health

# 2. list projects
curl -s -H "Authorization: Bearer $TOKEN" $BASE/api/v1/projects

# 3. search
curl -s -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"query":"rope embedding","topK":5}' \
  $BASE/api/v1/projects/current/search

# 4. read a page
curl -s -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/projects/current/files/content?path=wiki/concepts/rope.md"
```

If you're writing TypeScript / JavaScript:

```ts
const res = await fetch("http://127.0.0.1:19828/api/v1/projects/current/search", {
  method: "POST",
  headers: { "Authorization": `Bearer ${process.env.LLM_WIKI_API_TOKEN}`, "Content-Type": "application/json" },
  body: JSON.stringify({ query: "rope embedding", topK: 5 }),
})
const { results } = await res.json()
```

Python is the same shape — `urllib.request`, `requests`, `httpx`, whatever you already have. **Don't install anything new.**

## Auth model

The API is **localhost-only**. The token is one of:

1. `LLM_WIKI_API_TOKEN` environment variable (if set, overrides UI)
2. The user's `apiConfig.token` saved via Settings → API Server
3. `allowUnauthenticated: true` mode (no token needed; rare, user opt-in only)

Always check `/api/v1/health` first — it returns `{ enabled, authConfigured, allowUnauthenticated, tokenSource }`. **If `authConfigured: false && allowUnauthenticated: false`, ask the user to open `Settings → API Server → Generate new token`**. Do not proceed without auth being set up.

Three equivalent ways to send the token:

```
Authorization: Bearer <token>          # preferred
X-LLM-Wiki-Token: <token>              # alternative header
?token=<urlencoded-token>              # query param — last resort, leaks into logs
```

**Never log or echo the token. Never put it in any URL the user can see in your output** (Referer / shell history / logs all leak it).

## Standard workflow

When the user asks "look it up in my wiki":

1. **Resolve project** (see [Project resolution](#project-resolution) below).
2. **Search**: `POST /api/v1/projects/{id}/search` with `{ query, topK: 5..10 }` → ranked hits (`path`, `title`, `snippet`, `score`, `titleMatch`, optional `vectorScore`, `images`). Inspect `response.mode` to know whether hybrid retrieval kicked in.
3. **Read top hits**: for each promising hit, `GET /api/v1/projects/{id}/files/content?path=...` for the full markdown. Or pass `includeContent: true` to the search to avoid the round-trip.
4. **Cite + answer**: synthesize an answer grounded in the read pages. **Quote the `path` of each page you used** so the user can verify and jump in-app.

### Reading the score

The `score` field's scale depends on `mode`:

- **`mode: "keyword"`** — additive keyword score. Filename-exact hits are ~200; phrase-in-title ~50+; bag-of-tokens lands in single digits. Treat anything below ~5% of the top result as low-confidence.
- **`mode: "hybrid"` or `"vector"`** — RRF (Reciprocal Rank Fusion) score, typically in the **0.015–0.035** range. The absolute number is small; relative ordering is what matters. Use the per-result `vectorScore` (raw cosine 0–1) for "how strongly did the embedding match" if you need it.

Don't apply a fixed score threshold across modes. Sort by `score` descending and rely on relative gaps.

### Project resolution

`{id}` in every project-scoped endpoint accepts **four forms**:

| Form | When to use | Example |
|---|---|---|
| `current` (literal) | Default for "my wiki / 我的知识库 / this project / this wiki". The user is referring to whatever is open in the desktop UI. | `/api/v1/projects/current/search` |
| UUID | The user pasted a project ID, OR you previously resolved a name to an ID and want to re-use it. | `/api/v1/projects/a0e90b29-fcf3-4364-9502-8bd1272de820/files` |
| Absolute filesystem path (URL-encoded) | The user named the path (e.g. `~/notes/research`). Useful when the user has multiple projects with similar names. | `/api/v1/projects/%2FUsers%2Fme%2Fwiki%2Fresearch/files` |
| Project name | **Not supported directly.** You must `GET /api/v1/projects` first, find a match by `name`, then use that project's `id`. |

**Decision tree** for what the user said:

```
"my wiki" / "my 知识库" / "this wiki" / "this project" / unspecified
    → use `current`

"my Research project" / "in Reading"
    → GET /api/v1/projects
    → name-match (case-insensitive substring on `name`)
    → use the resulting `id`
    → if 0 matches: tell the user, list available names, fall back to `current` only if they confirm
    → if 2+ matches: ask the user to disambiguate, quoting both names + paths

"the project at /Users/me/foo"
    → URL-encode the path, use directly
    → if the API returns 404, the project isn't registered — list and let user pick

"project a0e90b29-…"
    → use the UUID literally
```

Cache the resolved `id}` for the rest of the conversation — there's no need to re-`GET /projects` for every call. But if the user switches contexts mid-conversation ("now look in my Reading project"), re-resolve.

When the user is silent about which project, **default to `current`** and mention it once: *"Looking in your active project (Research Notes)…"*. This avoids cross-project surprises.

For graph / cross-reference questions:

- `GET /api/v1/projects/{id}/graph?limit=200` → `{ nodes: [{id, label, nodeType, path, linkCount}], edges: [{source, target, weight}] }`
- Filter via `?q=term` (substring of id/label, case-insensitive) and `?nodeType=entity|concept|...`

For "I added new docs" requests:

- `POST /api/v1/projects/{id}/sources/rescan` → returns `{ queue: { tasks }, changedTasks: [...] }`. Tell the user how many files changed. Actual ingest runs asynchronously via the desktop queue.

## Endpoint contract (v1)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/health` | No auth. Returns `{ ok, status, version, enabled, authRequired, authConfigured, allowUnauthenticated, tokenSource }`. |
| GET | `/api/v1/projects` | List projects. Each: `{ id, name, path, current }`. |
| GET | `/api/v1/projects/{id}/files?root=wiki\|sources\|all&recursive=true&maxFiles=2000` | Tree of `{ name, path, isDir, size, children }`. Capped at 10000 nodes (413). |
| GET | `/api/v1/projects/{id}/files/content?path=wiki/foo.md` | Text files only (md/mdx/txt/json/yaml/yml/csv/html/htm/xml/rtf/log). 2 MB max. 415 on binary, 413 on oversize, 403 on out-of-scope path. |
| POST | `/api/v1/projects/{id}/search` | Body: `{ "query": "...", "topK": 10, "includeContent": false }`. **Hybrid (keyword + vector)** when the user has embeddings configured in Settings; falls back to keyword-only otherwise. Response carries `mode: "keyword" \| "vector" \| "hybrid"`, plus `tokenHits` / `vectorHits` and per-result `vectorScore`. Empty query → 400. |
| GET | `/api/v1/projects/{id}/graph?q=&nodeType=&limit=200` | Wikilinks graph from `wiki/*.md`. Limit clamped to 1000. |
| POST | `/api/v1/projects/{id}/sources/rescan` | Triggers a backend rescan using the user's Source Watch config. Returns post-rescan queue + actually-changed tasks. |
| POST | `/api/v1/projects/{id}/chat` | **501** — not implemented in v1. Don't call. |

`{id}` accepts a UUID, an absolute filesystem path (URL-encoded), or the literal string `current`.

## Error handling

Always treat the status code as the contract:

| Status | Meaning | What to do |
|---|---|---|
| 200 | OK | Use `body.ok === true` belt-and-suspenders; payload is in the same object. |
| 400 | Bad request | Show `body.error`. Typical: empty `query`, invalid `?root=`, oversized body. |
| 401 | Unauthorized | Token missing/wrong. Tell user to set/regenerate in Settings → API Server. |
| 403 | Forbidden | Path traversal or out-of-scope (e.g. `../app-state.json`). Don't retry the same path. |
| 404 | Not found | Unknown project id or unknown route. On unknown project, list projects first to recover. |
| 405 | Method not allowed | Wrong HTTP verb. |
| 413 | Payload too large | File > 2 MB, file tree > maxFiles, or request body > 1 MB. Suggest narrower scope. |
| 415 | Unsupported media | Binary or non-UTF-8 file content. API is text-only. |
| 429 | Too many requests | Rate limit (120 req/sec global). Back off ≥1 second. |
| 500 | Internal error | Log + report; don't loop. |
| 501 | Not implemented | `/chat` stub. Don't retry. |
| 503 | Service unavailable | Two flavors: API toggled off (`error` contains "disabled"); in-flight cap (64) reached ("busy"). Back off ≥2s. |

If the HTTP call itself fails (connection refused / ENOTFOUND): the desktop app is **not running**. Tell the user: "Launch LLM Wiki, then re-try."

## Ingest queue file is itself corrupted (JSON parse fails)

When `ingest-queue.json` raises `JSONDecodeError: Extra data: line N column M`, the file was written mid-flush — typically an OS-level write truncation, crash, or the app being killed during a state save. Empirically observed on RadarWiki 2026-06-11: the file's last ~10 chars were `[<valid obj>...]\nount": 1\n  }\n]` — the end of the array got mangled into something that looks like a truncated property assignment.

**Why this matters:** `sources/rescan` will NOT recover the queue from corruption — it only adds new/changed sources. If the queue file is unparseable, the app's file-watcher is in a stuck state and you need to either (a) let the app rewrite it (usually happens on next state save if the in-memory queue is intact), or (b) recover manually.

**Recovery recipe (no data loss):**

```python
import json, shutil
from pathlib import Path
from datetime import datetime
from collections import Counter

llm = Path("/Users/skyfend/Documents/知识库/<project>/.llm-wiki")
ts = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = Path(f"/Users/skyfend/.hermes/backups/llm-wiki-queue/{ts}")
backup.mkdir(parents=True, exist_ok=True)
shutil.copy2(llm/"ingest-queue.json", backup/"ingest-queue.json.broken")
print(f"backup: {backup/'ingest-queue.json.broken'}")

raw = (llm/"ingest-queue.json").read_text()

# Brace-depth scan: walk char-by-char, harvest every {...} that parses as JSON.
# This works even when the surrounding [ ] wrapper is truncated or duplicated.
recovered = []
depth = 0
start = None
for i, ch in enumerate(raw):
    if ch == '{':
        if depth == 0: start = i
        depth += 1
    elif ch == '}':
        depth -= 1
        if depth == 0 and start is not None:
            try:
                recovered.append(json.loads(raw[start:i+1]))
            except json.JSONDecodeError:
                pass  # skip malformed objects
            start = None

print(f"recovered {len(recovered)} entries")
print(f"by status: {Counter(o.get('status') for o in recovered)}")

# Cross-check vs cache to confirm none are re-digests
cache = json.loads((llm/"ingest-cache.json").read_text())
cached_names = {Path(k).name for k in cache["entries"]}
overlap = [Path(o["sourcePath"]).name for o in recovered
           if Path(o["sourcePath"]).name in cached_names]
print(f"overlap with cache (would re-digest if written back): {len(overlap)}")
```

**When to actually rewrite `ingest-queue.json`:**
- **Do not rewrite preemptively.** The app's in-memory queue may still be intact; the next state save will overwrite the file with valid JSON. Just monitor for ~10 minutes.
- **Rewrite only if** (a) the file stays corrupt after one app restart, AND (b) you have a backup, AND (c) `ingest-progress/` shows the worker is idle (no live JSON, no recent `updatedAt`).
- **Always preserve `processing` entries** — never write back a queue that drops an in-flight task. Filter to only `pending` and `failed` statuses if you must write.
- After writing back, restart LLM Wiki so it re-reads the queue from disk instead of its stale in-memory copy.

**Don't lose information when rewriting:** always include `id`, `projectId`, `sourcePath`, `folderContext`, `status`, `addedAt`, `error`, `retryCount` fields from each recovered object. The app uses `id` for de-duplication.

## Ingest diagnostics (stuck/failed queue)

When the user asks whether LLM Wiki is "stuck" or "processing" or "digesting books":

1. **Check `ingest-queue.json`** first (`{project}/.llm-wiki/ingest-queue.json`). This gives the queue overview: how many files are `pending`, `processing`, or `failed`. A file in `processing` with `error` and `retryCount` may look stuck — but **this file alone is insufficient** to determine liveness.

2. **Check `ingest-progress/`** (`{project}/.llm-wiki/ingest-progress/<slug>-<hash>.json`). This is the **real per-file progress**. If a file exists in `ingest-progress/`, LLM Wiki is actively working on it. The JSON schema:
   ```json
   {
     "version": 1,
     "sourceIdentity": "book/Advanced Metric Wave Radar - 2020 - Wu.pdf",
     "sourceHash": "70ac38b8baddaca8",
     "sourceLength": 1064310,
     "sourceBudget": 300000,
     "targetChars": 60000,
     "overlapChars": 3000,
     "chunkTotal": 19,
     "completedThrough": 1,
     "globalDigest": "...",
     "analyses": ["## Chunk 1/19 — Page 1\n..."],
     "updatedAt": 1780935113952
   }
   ```
   Key fields: `chunkTotal` vs `completedThrough` (progress), `updatedAt` (liveness — a recent timestamp means it's still working).

3. **Interpreting "Chunk analysis stream failed"**: This error in `ingest-queue.json` means a single chunk's LLM API streaming call was interrupted. LLM Wiki auto-retries (`retryCount` increments). This is **not a deadlock** as long as `ingest-progress/` shows activity. Common causes: API provider rate limits, streaming connection drops, or long thinking-token outputs exceeding the model's context budget.

4. **`sources/rescan` is NOT a retry button**: It only picks up **new or changed** source files (by hash). A file already in the queue with `processing`/`pending`/`failed` status won't be re-added. Returns `{ changedTasks: [], queue: { tasks: [] } }` on no-op.

5. **Retrying a failed ingest task by editing the queue**: When the user wants to force-retry a specific book that's stuck in `failed` state, you can edit `ingest-queue.json` directly. **Always back up first** (`cp` to `~/.hermes/backups/llm-wiki-retry/<ts>/`). Find the target entry, change `status` from `"failed"` to `"pending"`, reset `retryCount` to `0`, and set `error` to `null`. Move it to the end of the JSON array so it doesn't interrupt the currently-processing task. LLM Wiki's file watcher picks up the change.

6. **Diagnosing streaming timeout ("Chunk analysis stream failed")**: The root cause is LLM Wiki's reasoning-mode prompt causing long `<think>...</think>` phases where the stream produces **zero bytes** for 30–120 seconds while the model reasons server-side. LLM Wiki's HTTP stream reader has a ~60s timeout, so complex chunks trigger it. Image captions use non-streaming mode and are unaffected. Fix: switch to a non-reasoning model or disable reasoning in `app-state.json` (`llmConfig.reasoning.mode` → `"off"`). **Important:** v0.4.23 already auto-forces reasoning off on the ingest path, so if you're still hitting this on ingest, maxContextSize mismatch is the more likely culprit (see "Common failure" section below). The reasoning fix here is mainly useful for chat/Q&A paths where the user has set a high mode and is hitting timeouts on long contexts.

7. **Switching LLM providers**: Edit `~/Library/Application Support/com.llmwiki.app/app-state.json` → `llmConfig`. Change `model`, `customEndpoint`, and `apiKey`. LLM Wiki reads this on startup, so a restart is required for the change to take effect. **Do not restart while a task is actively processing** (check `ingest-progress/` for recent `updatedAt`) — let it finish first to avoid losing progress. The file also contains `providerConfigs` with pre-configured alternate providers (e.g. `deepseek`, `zai`).

8. **`image-caption-cache.json` as a secondary liveness signal**: When chunk analysis is stuck, check the count of entries in `{project}/.llm-wiki/image-caption-cache.json`. If this is growing, LLM Wiki is still actively processing images even if chunk analysis has stalled. This confirms the app is alive and the API is reachable.

5. **Finding the API token from disk**: Read `~/Library/Application Support/com.llmwiki.app/app-state.json` → `apiConfig.token`. This avoids asking the user to copy-paste from Settings. Also exposes `llmConfig` (model, provider, endpoint) for debugging API issues.

6. **`ingest-queue.json` write-truncation corruption — recovery by depth-counted brace scan**

Verified 2026-06-11 on RadarWiki. Symptom: `json.loads(ingest-queue.json)` raises `JSONDecodeError: Extra data: line 92 column 2 (char 2921)`. Inspecting the file with `read_file` reveals a truncated last object — tail looks like `]\nount":1\n}\n]"` instead of a clean `]`. Cause: app got killed mid-write (or the file was being written while the watcher rewrote it, leaving a partial object appended).

The file contains mostly valid JSON objects separated by `,\n` plus the wrapping `[ ... ]`. Recovery is straightforward:

```python
import json
from pathlib import Path

raw = Path("/path/to/.llm-wiki/ingest-queue.json").read_text()
objects, depth, start = [], 0, None
for i, ch in enumerate(raw):
    if ch == '{':
        if depth == 0: start = i
        depth += 1
    elif ch == '}':
        depth -= 1
        if depth == 0 and start is not None:
            obj_text = raw[start:i+1]
            try:
                objects.append(json.loads(obj_text))
            except json.JSONDecodeError:
                pass  # skip the truncated tail
            start = None

print(f"recovered {len(objects)} valid queue entries")
```

After recovery, **always back up** the broken file first (`~/.hermes/backups/llm-wiki-queue/<ts>/ingest-queue.json.broken`) before deciding to overwrite. The app's in-memory queue is unaffected — only the on-disk file is corrupt — so a restart of LLM Wiki will regenerate the file from memory. Manual overwrite is only needed if the file IS the source of truth (e.g. when restarting with the app killed).

**Do NOT** try to repair the truncated last object — the inner fields are corrupt. Drop it. The app will re-add it on the next watcher scan if the source still needs ingesting.

7. **Worker vs queue desync — progress is alive but queue says `failed`**

Verified 2026-06-11 on RadarWiki. Symptom: `ingest-progress/<book>.json` shows `completedThrough=14/36` with a recent `updatedAt`, AND `ingest-queue.json` shows the same book as `status: failed, retryCount: 3`. This is a state drift where the worker IS still pushing the book forward (probably a delayed retry that happened after the queue file was last written with `failed`), but the on-disk queue is stale.

**Diagnosis**:
- Trust `ingest-progress/<book>.json` as the liveness signal — it's updated on every successful chunk completion.
- Treat `ingest-queue.json` status as **advisory only** when `ingest-progress/` shows recent activity.
- Do NOT modify the queue entry to force a retry while the worker is actively writing to `ingest-progress/` — you'll race the worker and potentially corrupt both files.

**Recovery**: just wait. The worker will either:
- Complete the book → progress moves to `_backup/`, queue entry gets cleared.
- Hit the failure again → queue updates to reflect new `retryCount`. (Retry count is per-attempt, not cumulative; after a successful retry the count resets.)

If after ~30 min of no `updatedAt` advance in `ingest-progress/`, then the worker actually died — that's a real stuck case, and the recipe in section 5 applies (back up, set `pending`, reset `retryCount`).

6. **"Queue full of pending, but nothing is happening" (worker idle after completion)**:
   The four-file state can drift: a digest finishes, its progress JSON moves to `ingest-progress/_backup/`, but the `ingest-cache.json` entry never gets written AND the user (or some watcher re-scan) re-adds the book to `ingest-queue.json` as `pending`. The app then sits idle — `ingest-progress/` is empty, no `processing` queue entries, CPU near zero, no ingest-related log lines.

   **Quick diagnosis** (run all three):
   ```python
   import json
   from pathlib import Path
   from collections import Counter

   proj = Path("~/Documents/知识库/<project>").expanduser()
   llm = proj / ".llm-wiki"

   queue = json.loads((llm/"ingest-queue.json").read_text())
   cache  = json.loads((llm/"ingest-cache.json").read_text())
   backup_dir = llm/"ingest-progress/_backup"

   # (a) Is the worker actually running?
   print("queue by status:", Counter(q["status"] for q in queue))
   print("in-flight progress files:", list((llm/"ingest-progress").glob("*.json")))
   # If statuses are all 'pending' and in-flight list is empty → worker is idle.

   # (b) Find digest results that are in _backup but NOT in cache (= orphan completed work)
   cached_hashes = {e["hash"][:16] for e in cache.get("entries", {}).values()}
   orphan = []
   for f in backup_dir.glob("*.json"):
       p = json.loads(f.read_text())
       h = p.get("sourceHash", "")[:16]
       if h and h not in cached_hashes and p.get("completedThrough", 0) >= p.get("chunkTotal", 0):
           orphan.append((p["sourceIdentity"], h, p["completedThrough"], p["chunkTotal"]))
   print("orphan completed (in _backup, NOT in cache):")
   for x in orphan: print(" ", x)
   # If you see entries here, the digest finished but app didn't register the cache entry.

   # (c) Verify the resulting wiki files actually exist (so the work isn't lost)
   for src_id, h, done, total in orphan:
       name = src_id.split("/")[-1].replace(".pdf", ".md")
       md = proj/"wiki/sources"/name
       print(f"  {name}: {'EXISTS' if md.exists() else 'MISSING'}")
   ```

   **Recovery** (without losing the digest the app already produced):
   - The digest output (entities, concepts, source.md) is already on disk — do **not** force-re-queue these via `sources/rescan`.
   - Restart LLM Wiki app. On launch it re-reads `ingest-progress/_backup/` and may re-register entries into `ingest-cache.json`. Empirically this heals the desync in most cases.
   - If a queue entry sits as `pending` after restart and is already complete (the .md file exists in `wiki/sources/`), the cleanest fix is to manually delete that one entry from `ingest-queue.json` (after backup). Do not edit `ingest-cache.json` directly — it gets rewritten on shutdown.

   **Liveness signals** (process is alive vs actually working):
   - `lsof -p $(pgrep -f "LLM Wiki.app/Contents/MacOS/llm-wiki") | grep -E "\.llm-wiki"` — if the process has no `.llm-wiki` files open, no digest is running. If it has one `ingest-progress/<book>-<hash>.json` open for writing, that book is actively being digested.
   - `ps -o pcpu,pmem -p <pid>` — sustained CPU >5% during ingest (LLM API streaming); <1% means idle.
   - `log show --predicate 'process == "llm-wiki"' --last 30m` — ingest paths produce structured JSON to stdout/log. Idle worker = no recent ingest lines, just WebKit/AppKit noise.

7. **Finding the active project's `.llm-wiki/` data dir** (multi-project setups):
   ```python
   import json
   from pathlib import Path
   app = json.loads(Path("~/Library/Application Support/com.llmwiki.app/app-state.json").read_text())
   proj_path = Path(app["lastProject"]["path"])
   llm_dir = proj_path / ".llm-wiki"
   ```
   Use this when `lsof` shows multiple LLM Wiki processes or the user has multiple projects registered. `lastProject.path` is the one currently open in the UI; other registered projects are in `projectRegistry`.

## Etiquette

- **Cite paths.** When you answer using wiki content, name the page: `(from wiki/concepts/rope.md)`. The user uses these to verify and to jump in-app.
- **Stay read-only by default.** Only `sources/rescan` mutates state; everything else is reads. Don't invent write endpoints — they don't exist in v1.
- **Don't dump full pages unless asked.** Snippet + path is usually enough. Pull full content only when reasoning genuinely needs it.
- **Respect the project boundary.** The current project is the user's active context. Do not silently switch projects.
- **Honor the rate limit.** 120 req/sec is plenty for sequential work, but parallel page reads can burst close to it. Batch where the API allows (`includeContent: true` on search avoids N+1 reads).
- **Never leak the token.** Headers are safe; query params and your own output text are not.

## See also
## Troubleshooting

### "Chunk analysis stream failed" (ingest 卡住)

**诊断优先级**（按顺序）：
1. **maxContextSize 超限（最常见）**：LLM Wiki 的 `maxContextSize` 可能超出模型实际 context。
   模型真实 context window 误设过大时 API 拒收 → stream 断。
   LLM Wiki 会在失败后**自动修正**此值（实测 1M → 524,288）。
   验证：读 `app-state.json` → `llmConfig.maxContextSize`，与模型实际值比对。
2. **reasoning/thinking 超时**：如果 maxContextSize 已修正但仍失败，检查 `reasoning: {mode: 'auto'}`。
   thinking 阶段 stream 无数据 30-120s，reader ~60s 超时。
3. **其他**：网络波动、API 临时不可用（较少见，captions 正常则排除）

**诊断**：
1. 检查 `~/.llm-wiki/ingest-progress/` 下对应书籍的 JSON，看 `completedThrough` 是否停滞
2. 检查 `ingest-queue.json` 中 `retryCount` 是否已达 3（failed）
3. 对比 image-caption-cache 是否正常增长（captions 用 non-streaming，不会超时）
   — 如果 captions 正常但 chunks 失败，基本确认是 streaming timeout

**修复**（选一）：
- **切换模型**（推荐）：改 app-state.json 的 llmConfig 到 DeepSeek 或其他非 reasoning 模型，然后重启 LLM Wiki
- **关掉 reasoning**：删除 llmConfig 中的 `reasoning` 字段
- **减小 chunk**：改 sourceBudget 从 300000 降到 50000-100000

**手动重试单本**：改 `ingest-queue.json` 把对应 entry 的 status 从 `failed` 改为 `pending`，
`retryCount` 归 0，移到队列末尾。操作前先备份。

## Ingest queue internals (troubleshooting stuck processing)

When ingest is stuck or failing, the primary data structures live under the project's `.llm-wiki/` directory (NOT via the HTTP API):

| File | Content |
|---|---|
| `ingest-queue.json` | Array of `{id, sourcePath, status, error, retryCount}`. Statuses: `processing`, `pending`, `failed`. |
| `ingest-progress/<book-slug>-<hash>.json` | Per-book progress: `{chunkTotal, completedThrough, globalDigest, analyses[], updatedAt}`. |
| `ingest-cache.json` | Completed books: `{hash, filesWritten[]}` per entry. |
| `image-caption-cache.json` | Hash→caption map for images already processed. **Count growth here proves ingest is alive** even when chunks stall. |

### Common failure: "Chunk analysis stream failed"

This error means the LLM API streaming call was interrupted mid-response. Not an auth or quota issue — image captions use the same API and may continue succeeding while chunks fail.

**Diagnosis checklist** (check in this order):

1. **`maxContextSize` mismatch FIRST.** LLM Wiki's `maxContextSize` may exceed the model's actual context window. When `maxContextSize` exceeds the model's real limit, the API rejects requests → stream broken → error. LLM Wiki empirically self-corrects after failed attempts, so check whether the correction has already occurred.

   To verify: read `app-state.json` → `llmConfig.maxContextSize` and cross-check against the model's actual context.

2. **Reasoning/thinking timeout.** If `maxContextSize` is correct, check `app-state.json` → `llmConfig.reasoning`. v0.4.23 Settings UI exposes 7 options (`auto` / `off` / `low` / `medium` / `high` / `max` / `custom`); they're stored verbatim as `llmConfig.reasoning.mode`. With `mode` other than `off`, the thinking phase runs server-side with **zero data on the stream** for 30–120 seconds. LLM Wiki's stream reader has a ~60s timeout. Large prompts (sourceBudget=300k chars) extend thinking time past the timeout. Shorter prompts (first chunk = preface/TOC) succeed; denser body chapters fail.

   **Critical override (v0.4.23+):** LLM Wiki **automatically forces reasoning off on structured paths** (ingest chunk analysis, image captioning, review-item generation) regardless of what `llmConfig.reasoning.mode` is set to. This is to prevent the model from spending the entire output budget on `<think>...</think>` with no actual content. The Settings page displays this caveat verbatim: "结构化任务（如 ingest）可能会自动关闭 reasoning，避免模型只输出思考过程而没有正文." So changing UI reasoning to "low" will **not** speed up ingest — only chat/Q&A actually honors the chosen mode. Do not be misled by the active ingest failures into thinking the mode setting is broken; it just doesn't apply on the ingest path.

3. **Liveness check.** Check `ingest-progress/` for `completedThrough` — if >0, some chunks succeeded. Check `image-caption-cache.json` count — if growing, API connectivity is fine regardless of chunk status.

**Fixes** (choose one):
- Correct `maxContextSize` to match the model's actual window (most common fix)
- Switch to a non-reasoning model, OR set `llmConfig.reasoning.mode` to `"off"` (note: ingest is already forced off by the override above — this fix only helps non-structured paths like chat)
- Switch to a model with larger context (DeepSeek v4-pro)
- Reduce `sourceBudget` in the app's chunk settings

**Reasoning-mode persistence (v0.4.23):** The Settings page writes the chosen reasoning mode to BOTH `llmConfig.reasoning.mode` and `providerConfigs.<activeProvider>.reasoning.mode` immediately on UI change (no restart needed for the new mode to apply to subsequent requests). However, switching `model` / `customEndpoint` / `apiKey` still requires a restart — those fields are read at launch.

### Common failure: "HTTP 529 — overloaded_error" (provider cluster saturated)

When ingest (or chat) surfaces an error like:

```
Chunk analysis failed: HTTP 529: — {"type":"error","error":{"type":"overloaded_error","message":"当前服务集群负载较高，请稍后重试，感谢您的耐心等待。 (2064) (529)"},"request_id":"0677fbea..."}
```

…this is **NOT** a configuration problem with LLM Wiki. It's an upstream cluster capacity issue from the provider.

**Diagnostic markers:**
- HTTP status `529` — Anthropic-protocol "Overloaded" (not a standard HTTP 5xx; provider returns this under cluster pressure).
- `error.type` = `"overloaded_error"`; nested `code` field = provider's internal code for cluster saturation.
- `message` is in Chinese: "当前服务集群负载较高，请稍后重试，感谢您的耐心等待" (translated: "Current service cluster is under high load, please retry later, thank you for your patience").
- `request_id` is the provider's tracking ID — hand it to their support if the issue persists.

**Why it happens (and why reasoning.mode change doesn't fix ingest):**
- Reasoning models run server-side thinking for 30–120s before any byte hits the stream. Each in-flight request holds a cluster slot the whole time.
- When many users hit the provider concurrently, the cluster fills up; new requests get rejected with 529.
- Changing `reasoning.mode` to `low`/`off` would normally shorten each request — **but ingest path is already auto-forced off**, so reasoning mode change has zero effect on ingest.
- Effective levers when 529 clusters: (a) wait, (b) reduce `sourceBudget` so prompts are smaller → faster generation even without thinking, (c) switch provider to DeepSeek temporarily, (d) avoid peak hours.

**Recovery:**
- LLM Wiki's `ingest-queue.json` increments `retryCount` automatically on 529; the task is not failed unless `retryCount` hits the cap.
- Worker keeps running other queue items in the meantime — verify with `ingest-progress/` for active files (any JSON there with recent `updatedAt` = alive).
- If 529s persist for >10 minutes across multiple files, the cluster is genuinely degraded — switch provider or pause and resume later.

### Reasoning mode scope matrix (v0.4.23+)

When the user asks "does my reasoning setting apply to X?", use this table. The Setting UI on the model page exposes 7 modes (`auto` / `off` / `low` / `medium` / `high` / `max` / `custom`); they all map to `llmConfig.reasoning.mode`. But not every LLM call site honors that value.

| Code path | Honors `reasoning.mode`? | Notes |
|---|---|---|
| Ingest chunk analysis (PDF / docx → structured wiki pages) | **No — auto-overridden to off** | Forced off to avoid "thinking only, no body" output. UI caveat printed verbatim under the settings: "结构化任务（如 ingest）可能会自动关闭 reasoning". User's "high/max" choice is silently ignored here. |
| Image captioning (multimodal) | **No — auto-overridden to off** | Same reason. |
| Review-item generation (`review.json` write path) | **No — auto-overridden to off** | Same reason. |
| Provider test ("测试功能" button) | Yes | Single tiny completion; no override needed since it's not structured extraction. |
| Chat / Q&A (in-app chat with the wiki) | **Yes** | This is the only place where the user's chosen mode actually drives behavior. If the user reports chat is slow / cuts off, look here. |

**How to detect the override from the filesystem:** You can't, directly — the override is in-process. But if `ingest-progress/<book>.json` keeps `completedThrough` advancing on dense chapters while `reasoning.mode` is set to `"high"` or `"max"`, that's evidence the override is working (otherwise you'd see stream timeouts).

**If the user wants to speed up ingest:** reasoning mode is NOT the lever. Use these instead:
- Reduce `sourceBudget` (smaller prompts → shorter thinking even if reasoning were active, and faster API roundtrip in general)
- Switch `model` to a non-reasoning model (DeepSeek v4-pro)
- Correct `maxContextSize` if it's larger than the model's actual window

### Retrying a failed ingest task

The `/api/v1/projects/current/sources/rescan` endpoint **will not re-add files already in the queue** — it only detects new/changed files. To retry a `failed` task:

1. Backup `ingest-queue.json`
2. Find the failed entry, change `status` to `"pending"`, reset `retryCount` to 0, clear `error`
3. Move it to the end of the array so it doesn't interrupt the current processing task
4. Write back — LLM Wiki's file watcher picks up the change

### Switching the LLM model

LLM Wiki stores its active model config in `app-state.json` under `llmConfig`. Provider-specific API keys live under `providerConfigs.<name>`. To switch:

1. Backup `app-state.json`
2. In `llmConfig`: change `model`, `customEndpoint`, `apiKey` to the new provider
3. Remove or nullify `reasoning` if the new model uses a different format
4. **LLM Wiki must be restarted** to load the new `model` / `endpoint` / `apiKey` — those fields are read at launch. Note: `reasoning.mode` is per-request and DOES NOT require a restart; UI changes to reasoning apply to subsequent requests immediately (and persist to disk in `app-state.json` under both `llmConfig.reasoning` and `providerConfigs.<activeProvider>.reasoning`).

**⚠️ Pitfall: LLM Wiki auto-reverts manual app-state.json edits.** If LLM Wiki is running during the edit, the app may overwrite `app-state.json` with its in-memory state at shutdown, silently reverting `model`/`endpoint`/`apiKey`. To prevent this:
- Quit LLM Wiki before editing `app-state.json`
- Or verify the edit survived by reading the file back after ~30 seconds (if it reverted, the app was running)
- Symptom: `model` field changes to a different model even though you wrote `deepseek-v4-pro`

### Monitoring ingest progress (headless)

```bash
# Quick one-shot
python3 -c "
import json; from pathlib import Path
d = Path('~/Documents/知识库/全知全能/.llm-wiki/ingest-progress').expanduser()
for f in sorted(d.iterdir()):
    data = json.loads(f.read_text())
    print(f'{data[\"sourceIdentity\"]}: {data[\"completedThrough\"]}/{data[\"chunkTotal\"]}')
"

# Queue status
python3 -c "
import json; from pathlib import Path
from collections import Counter
q = json.loads(Path('~/Documents/知识库/全知全能/.llm-wiki/ingest-queue.json').expanduser().read_text())
print(Counter(i['status'] for i in q))
"
```

## "Why is the queue full of files I already digested?" (re-digestion diagnostic)

When the user says "I already digested these, why is it re-running?" or "the queue keeps growing" — the cause is almost never "the .md got lost". Cross-check **all four state files** under `{project}/.llm-wiki/` to find the real cause.

**State files and what they mean** (full schema in [references/llm-wiki-state-files.md](references/llm-wiki-state-files.md)):

| File | Role |
|------|------|
| `ingest-cache.json` | **Authoritative** record of completed digests: `{sourcePath → hash, timestamp, filesWritten[]}`. If entry exists, the .md files were written. |
| `ingest-queue.json` | Pending work. Items re-appear here when file-watcher detects hash mismatch. |
| `file-snapshot.json` | Current file inventory (list of paths). The watcher diffs this against disk. |
| `lancedb/wiki_chunks_v{N}.lance` | Vector index. Version `N` bumps when `embeddingConfig` (model/dim) changes — see `app-state.json.bak.pre-*` for rollback points. |

**Diagnostic procedure** (run all of these — don't skip):

```python
import json, hashlib
from pathlib import Path

proj = Path("~/Documents/知识库/<project>").expanduser()
cache = json.loads((proj/".llm-wiki/ingest-cache.json").read_text())
queue = json.loads((proj/".llm-wiki/ingest-queue.json").expanduser().read_text())
snapshot = json.loads((proj/".llm-wiki/file-snapshot.json").read_text())

# 1. Compare cache hash to current source hash
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
mismatches = []
for k, v in cache["entries"].items():
    src = proj/"raw/sources"/k
    if not src.exists():
        mismatches.append(("DELETED", k, "", v["hash"][:8]))
    elif sha(src) != v["hash"]:
        mismatches.append(("HASH_DRIFT", k, sha(src)[:8], v["hash"][:8]))

# 2. For each queue item, check if a same-named entry already exists in cache
duplicates = []
for q in queue:
    fname = Path(q["sourcePath"]).name
    if any(fname == Path(k).name for k in cache["entries"]):
        duplicates.append(q["sourcePath"])

# 3. Look for new directories in file-snapshot that weren't there before
from collections import Counter
c = Counter(Path(q["sourcePath"]).parent.name for q in queue)
print("queue items per parent dir:", c)
```

**Three common root causes** (and how to tell them apart):

| Symptom | Root cause | Fix |
|---------|-----------|-----|
| All `cache` hashes mismatch the current file hash, but file mtime is old and content is identical | **macOS metadata drift**: iCloud sync / Preview.app opening / FinderInfo rewrite changes file hash without changing content. The watcher re-queues because hash differs. | Disable `autoIngest` in `app-state.json` → `sourceWatchConfig.<projectId>.autoIngest: false`. Trigger manual `sources/rescan` only when content actually changes. |
| Queue has N items, all under a directory that didn't exist in yesterday's snapshot (e.g. `raw/sources/book/` appeared 6/9 02:16) | **New subdirectory scanned**: LLM Wiki's watcher discovers the new path and queues every PDF in it, even if older files in the same project have already been digested. | This is **not** re-digestion — these are genuinely new files. Let the queue run, or pause + review. |
| Queue has items where filename matches a cache entry but path differs (e.g. `raw/sources/X.pdf` in cache, `raw/sources/book/X.pdf` in queue) | **Path-blind hash check**: hash is computed on absolute path, so two copies of the same file in different folders are considered different. | This is one **real** duplicate. Either delete the second copy, or accept the new digest. |

**Critical pitfall — don't fall for the surface reading**:

When a user sees `160 pending + 7 .md files in wiki/sources/`, they will assume the .md files are being re-created. Almost always they are not. The 160 are a new workload, not a re-run. **Verify before acting**:

```bash
# Quick sanity check: are the .md files actually in place?
ls /Users/skyfend/Documents/知识库/<project>/wiki/sources/ | wc -l   # should match cache entry count
ls /Users/skyfend/Documents/知识库/<project>/wiki/entities/ | wc -l
ls /Users/skyfend/Documents/知识库/<project>/wiki/concepts/ | wc -l
```

If those counts match `ingest-cache.json` → the digest results are intact. The queue is new work, not re-runs.

**Pre-flight: parse-check the queue file first.** Before trusting any queue-driven diagnostic, run `json.loads((proj/".llm-wiki/ingest-queue.json").read_text())`. If that throws, the queue is corrupted (see "Ingest queue file is itself corrupted" above) and the watcher is in a stuck state — your Counter/regex analysis will silently fail on the bad read. Diagnose the corruption before counting queue entries.

**`app-state.json.bak.pre-X.<ts>` pattern** indicates a config rollback point:

```
app-state.json.bak.pre-embedding.1780837233  →  embedding model was changed
app-state.json.bak.pre-bge.1780838179        →  bge-m3 embedding was set up
```

When you see these, you know the user recently switched `embeddingConfig` (model, dim, endpoint). **This causes lancedb to bump to a new version** (`wiki_chunks_v1.lance` → `wiki_chunks_v2.lance`) and re-embed all chunks — but does **not** re-run the LLM digest. If the user reports "everything is being re-done" right after an embedding switch, it's actually just re-embedding, which is fast and expected.

## See also

- `references/queries-directory.md` — what's in `wiki/queries/`, the 5 categories (PDF-recovery / real-research / junk-research / short-Q / duplicate), and the disposition matrix
- `references/pdf-recovery-workflow.md` — end-to-end "处理 a Category 1 query" workflow (diagnose → OCR-validate → update entity/concept/source → dispose duplicate → log); the actionable counterpart to queries-directory.md's classification
- `references/broken-link-repair-workflow.md` — step-by-step workflow for triaging + fixing the review queue's `confirm` items (broken wikilinks), including the 5-bucket triage and the C-bucket "frontmatter + body" double-fix pitfall
- `references/queries-directory.md` — what's in `wiki/queries/`, the 5 categories (PDF-recovery / real-research / junk-research / short-Q / duplicate), and the disposition matrix
- `references/pdf-recovery-workflow.md` — end-to-end "处理 a Category 1 query" workflow (diagnose → OCR-validate → update entity/concept/source → dispose duplicate → log); the actionable counterpart to queries-directory.md's classification
- `references/broken-link-repair-workflow.md` — step-by-step workflow for triaging + fixing the review queue's `confirm` items (broken wikilinks), including the 5-bucket triage and the C-bucket "frontmatter + body" double-fix pitfall