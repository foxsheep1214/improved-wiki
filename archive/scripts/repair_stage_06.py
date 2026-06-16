#!/usr/bin/env python3
"""repair_stage_06.py — 补做漏掉的 Stage 0.6（图片 caption）

扫描 wiki/media/*/ 下所有缺 .caption.txt 的图片，用 MiniMax M3 批量并行生成 caption。
支持多线程并发，断点续传。
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROJECT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(os.environ.get("IMPROVED_WIKI_ROOT", os.getcwd()))
API_KEY_FILE = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/tmp/_api_key.txt")
WORKERS = int(sys.argv[3]) if len(sys.argv) > 3 else 4
BATCH_SIZE = 8
MODEL = "MiniMax-M3"

API_KEY = API_KEY_FILE.read_text().strip()
MEDIA_ROOT = PROJECT / "wiki" / "media"

SYSTEM_PROMPT = """你是硬件知识库的图像解读专家。每次给你若干张图，按顺序逐张描述。
要求：1-3 句中文，不超过 100 字。
聚焦：图类型（电路/波形/框图/PCB/曲线/参数表/公式/实物/示意/照片等）+ 关键内容 + 关键参数/标注。

输出格式：严格按以下 JSON 数组：
```json
[
  {"idx": 1, "caption": "..."},
  {"idx": 2, "caption": "..."}
]
```
每个对象都要有，即使图不清楚也尽量给个最合理的简短描述。"""

_lock = threading.Lock()
_done_count = 0
_total = 0
_start_time = 0
_consecutive_errors = 0


def find_missing_captions():
    missing = []
    for media_dir in sorted(MEDIA_ROOT.rglob("*")):
        if not media_dir.is_dir() or media_dir.name.startswith("."):
            continue
        # Skip type-only dirs like book/, datasheet/ — only process leaf dirs with images
        if not any(f.is_file() for f in media_dir.iterdir()):
            continue
        for f in sorted(media_dir.iterdir()):
            if f.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                continue
            cap_path = media_dir / (f.name + ".caption.txt")
            if not cap_path.exists() or cap_path.stat().st_size < 20:
                missing.append((media_dir.name, f, media_dir, cap_path))
    return missing


def caption_batch(batch_items):
    content = [{"type": "text", "text": f"请按顺序描述以下 {len(batch_items)} 张图：\n\n"}]

    for i, (book, img_path, media_dir, cap_path) in enumerate(batch_items):
        content[0]["text"] += f"[图{i+1}] 来源: {book}, 文件: {img_path.name}\n"
        with open(img_path, "rb") as fh:
            img_data = base64.standard_b64encode(fh.read()).decode()
        ext = img_path.suffix.lower().lstrip(".")
        media_type = f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}"
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": img_data},
        })
        content.append({"type": "text", "text": f"[/图{i+1}]\n"})

    payload = {
        "model": MODEL,
        "max_tokens": 4096,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.3,
    }

    req = urllib.request.Request(
        "https://api.minimaxi.com/anthropic/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                text = "".join(c["text"] for c in result["content"] if c["type"] == "text").strip()
                usage = result.get("usage", {})
                return text, usage, None
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")[:500]
            if attempt < 2:
                time.sleep((attempt + 1) * 10)
            else:
                return None, None, f"HTTP {e.code}: {err_body[:200]}"
        except Exception as e:
            if attempt < 2:
                time.sleep(10)
            else:
                return None, None, f"{type(e).__name__}: {e}"
    return None, None, "max retries"


def parse_captions(text, count):
    t = text.strip()
    if t.startswith("```"):
        parts = t.split("```", 2)
        if len(parts) >= 2:
            t = parts[1]
            if t.startswith("json"):
                t = t[4:]
        if t.endswith("```"):
            t = t[:-3]
    try:
        caps = json.loads(t.strip())
        return [c.get("caption", "").strip() for c in caps[:count]]
    except json.JSONDecodeError:
        return [f"（图{i+1}，解析失败）" for i in range(count)]


def process_batch(args):
    batch_idx, batch = args
    global _done_count, _consecutive_errors, _total, _start_time

    book = batch[0][0]
    t0 = time.time()

    # Skip already captioned
    still_missing = [item for item in batch if not item[3].exists() or item[3].stat().st_size < 20]
    if not still_missing:
        with _lock:
            _done_count += len(batch)
        return len(batch), 0, 0, 0

    text, usage, err = caption_batch(still_missing)
    elapsed = time.time() - t0

    if err:
        with _lock:
            _consecutive_errors += 1
            print(f"  [{batch_idx+1:4d}] {book[:40]:40s} ✗ {err}")
        return 0, 0, 0, elapsed

    captions = parse_captions(text, len(still_missing))
    saved = 0
    for j, cap in enumerate(captions):
        if j < len(still_missing):
            still_missing[j][3].write_text(cap, encoding="utf-8")
            saved += 1

    in_tok = usage.get("input_tokens", 0) if usage else 0
    out_tok = usage.get("output_tokens", 0) if usage else 0

    with _lock:
        _done_count += saved
        _consecutive_errors = 0
        pct = _done_count / _total * 100 if _total else 0
        elapsed_total = time.time() - _start_time if _start_time else 1
        eta = (_total - _done_count) / max(_done_count, 1) * elapsed_total / WORKERS
        fname = still_missing[0][1].name if still_missing else "?"
        print(f"  [{batch_idx+1:4d}] {book[:40]:40s} {fname:30s} ✓ {saved}/{len(still_missing)}"
              f" ({elapsed:.1f}s, {pct:.1f}%, ETA {eta/60:.0f}m) | in={in_tok} out={out_tok}")

    return saved, in_tok, out_tok, elapsed


def main():
    global _total, _start_time, flat_batches

    all_missing = find_missing_captions()
    if not all_missing:
        print("All images have captions!")
        return

    by_book = {}
    for item in all_missing:
        by_book.setdefault(item[0], []).append(item)

    book_order = sorted(by_book.keys(), key=lambda b: len(by_book[b]))
    flat_batches = []
    for book in book_order:
        items = by_book[book]
        for i in range(0, len(items), BATCH_SIZE):
            flat_batches.append(items[i:i + BATCH_SIZE])

    _total = len(all_missing)
    _start_time = time.time()
    print(f"Found {_total} images without captions across {len(by_book)} books")
    print(f"Batches: {len(flat_batches)}, Workers: {WORKERS}")
    print(f"Estimated: ~{len(flat_batches) * 18 / WORKERS / 60:.0f} min with {WORKERS}x parallelism")
    print()

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {
            executor.submit(process_batch, (i, batch)): i
            for i, batch in enumerate(flat_batches)
        }
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f"  Worker exception: {e}")

    total_elapsed = time.time() - _start_time
    print(f"\n=== Done ===")
    print(f"  Captioned: {_done_count}/{_total}")
    print(f"  Time: {total_elapsed/60:.1f} min")
    if _done_count < _total:
        print(f"  Remaining: {_total - _done_count} (re-run to retry)")


if __name__ == "__main__":
    main()
