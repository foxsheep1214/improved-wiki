# LLM Wiki Ingest Files Reference

## Key file paths (relative to project root, e.g. `~/Documents/知识库/全知全能/`)

### `.llm-wiki/ingest-queue.json`
The master ingest queue. Array of task objects:

```json
{
  "id": "ingest-1780927238701-osxesc",
  "projectId": "47dfa87b-...",
  "sourcePath": "raw/sources/book/Advanced Metric Wave Radar - 2020 - Wu.pdf",
  "folderContext": "book",
  "status": "processing",     // "pending" | "processing" | "failed"
  "addedAt": 1780927238701,
  "error": "Chunk analysis stream failed",  // null if no error
  "retryCount": 1             // LLM Wiki caps at 3; then marks failed
}
```

**Editing to retry a failed task**:
1. Back up: `cp *.json ~/.hermes/backups/llm-wiki-retry/<ts>/`
2. Find the failed entry, change `status` → `"pending"`, `retryCount` → `0`, `error` → `null`
3. Move it to the **end** of the array (so it doesn't interrupt the current processing task)
4. Save — LLM Wiki's file watcher picks it up

### `.llm-wiki/ingest-progress/<slug>-<hash>.json`
Per-file chunk analysis progress. Created when a file enters processing, deleted on completion. Key fields:

| Field | Meaning |
|---|---|
| `chunkTotal` | Total number of chunks |
| `completedThrough` | How many chunks finished (1-indexed) |
| `updatedAt` | Unix ms timestamp — **primary liveness signal** |
| `sourceBudget` | Chars fed to LLM per chunk (e.g. 300000) |
| `targetChars` | Expected output chars per chunk (e.g. 60000) |
| `globalDigest` | Running cross-chunk summary |
| `analyses` | Array of completed chunk outputs |

If `updatedAt` is recent (< 5 min ago), LLM Wiki is still actively working — even if `ingest-queue.json` shows an error with a retry count.

### `.llm-wiki/image-caption-cache.json`
Key-value store: `{ sha256_of_image: { caption: "..." } }`. Growing count = LLM Wiki is still live. Captions use non-streaming mode and are unaffected by streaming timeouts.

### `~/Library/Application Support/com.llmwiki.app/app-state.json`
LLM Wiki desktop app state. Contains:

```json
{
  "apiConfig": { "token": "sk-...", "enabled": true, "allowUnauthenticated": true },
  "llmConfig": {
    "provider": "custom",
    "model": "deepseek-chat",
    "customEndpoint": "https://api.deepseek.com/v1",
    "apiKey": "sk-...",
    "apiMode": "chat_completions",
    "maxContextSize": 1000000,
    "reasoning": { "mode": "auto" }
  },
  "providerConfigs": {
    "deepseek": {
      "model": "deepseek-v4-pro",
      "baseUrl": "https://api.deepseek.com/v1",
      "apiKey": "sk-...",
      "maxContextSize": 1000000
    }
  }
}
```

**To switch LLM provider**: edit `llmConfig.model`, `llmConfig.customEndpoint`, `llmConfig.apiKey`. Remove `reasoning` to disable thinking mode. Restart LLM Wiki to apply. **Never restart while a task is processing** — check `ingest-progress/` for a recent `updatedAt` first.

## Common error: "Chunk analysis stream failed"

**Root cause**: Reasoning models (e.g. DeepSeek R1) enter a `<think>...</think>` phase that produces zero stream bytes for 30–120 seconds while reasoning server-side. LLM Wiki's HTTP stream reader has a ~60s timeout. Large/complex chunks trigger this.

**Fix options** (pick one):
1. Switch to a non-reasoning model (e.g. DeepSeek without thinking)
2. Remove `reasoning` from `llmConfig` in `app-state.json`
3. Reduce `sourceBudget` so chunks are smaller and thinking is shorter
