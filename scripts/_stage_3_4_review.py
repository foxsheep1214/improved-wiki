
from _stage_2_base import *

def stage_3_4_review_suggestions(file_blocks: list[tuple[str, str]], raw_file: Path,
                                  config: Config, *, verbose: bool = False) -> dict:
    """Stage 3.4: Run LLM review over newly generated wiki pages (quality assurance).

    NashSU trigger conditions (ingest.ts): any of —
      - >= 4 FILE blocks
      - >= 10K chars of generation output

    The "incomplete REVIEW block" trigger was dropped: it only ever fired on the
    retired ``raw_response``, which (being "\n".join of *parsed* FILE-block
    bodies) could never contain a ``---REVIEW:`` marker — REVIEW blocks are not
    FILE blocks and never survived parse_file_blocks. So the check was already
    inert; the volume signal now reads directly off ``file_blocks`` instead.

    Output: wiki/REVIEW/<type>/<date>-<source>-<short-slug>.md — human-browsable review pages.
    Each page has frontmatter `resolved: false`. When resolved, user changes to true.
    On next ingest, resolved pages are auto-cleaned.
    Also writes review-suggestions.json to runtime dir for tooling.
    """
    # Generation-volume signal: total chars across the pages generated this pass.
    # file_blocks content is the full FILE-block content (frontmatter + body) —
    # the same text the retired raw_response concatenated, so the 10K threshold
    # carries the same intent ("did this source produce substantial content").
    gen_chars = sum(len(content) for _, content in file_blocks)
    # ``file_blocks`` reflects only THIS conversation-mode replay pass, not the
    # whole ingest. A source that needed several replays (e.g. one pass does the
    # real Stage 2.4 generation, a later pass replays after those pages are
    # already on disk and correctly emits 0 new blocks) would otherwise have a
    # substantial earlier pass's work invisible to this threshold check,
    # silently skipping review even though 20+ pages were genuinely generated
    # (confirmed live: Plett BMS Vol.2 — an earlier pass generated 26 blocks and
    # a real review answer was cached and accepted, but the final completing
    # pass saw file_blocks=1 and skipped, so the cached review was never written
    # to wiki/REVIEW/).
    conv_dir = config.runtime_dir / "conversation" / (config.conversation_prefix or "00000000")
    cumulative_blocks = len(file_blocks)
    if conv_dir.exists():
        for f in conv_dir.glob("Stage-2-*-*.txt"):
            try:
                cumulative_blocks += f.read_text(encoding="utf-8").count("---FILE:")
            except OSError:
                continue
    if cumulative_blocks < 4 and gen_chars < 10000:
        print(f"[stage 3.4] Skipped — {len(file_blocks)} blocks this pass "
              f"({cumulative_blocks} cumulative across replays), {gen_chars} chars "
              f"(all below NashSU thresholds)")
        return {"skipped": True, "reason": "below-thresholds"}

    print(f"[stage 3.4] Running review over {len(file_blocks)} new pages + existing wiki...")

    # Collect new page contents
    new_pages: list[str] = []
    for path, content in file_blocks:
        new_pages.append(f"### {path}\n{content[:1500]}")

    # Sample existing wiki pages (up to 40)
    existing_pages: list[str] = []
    for sub in ["sources", "concepts", "entities", "comparisons", "findings"]:
        d = config.wiki_dir / sub
        if not d.exists():
            continue
        for f in sorted(d.iterdir()):
            if f.suffix != ".md":
                continue
            content = f.read_text(encoding="utf-8")
            if content.startswith("---"):
                end = content.find("\n---", 3)
                body = content[end + 4:] if end != -1 else content
            else:
                body = content
            existing_pages.append(f"### {sub}/{f.name}\n{body[:1000]}")
            if len(existing_pages) >= 40:
                break
        if len(existing_pages) >= 40:
            break

    schema_text = ""
    schema_path = config.wiki_dir / "schema.md"
    if schema_path.exists():
        schema_text = schema_path.read_text(encoding="utf-8")[:2000]

    user_content = f"""# wiki/schema.md
{schema_text}

# Newly generated pages (from {raw_file.stem})
{chr(10).join(new_pages)}

# Existing wiki pages (sample of {len(existing_pages)})
{chr(10).join(existing_pages[:40])}
"""

    system_prompt = """你是 HardwareWiki 的 review agent。审阅当前 wiki 内容，找出 5 类可疑项：
1. confirm（需要人工确认）：数字、术语、矛盾点
2. suggestion（改进建议）：内容不完整、应补充、可加链接
3. missing-page（缺页）：[[wikilink]] 指向不存在的页面
4. contradiction（页面间矛盾）
5. duplicate（内容重复）

输出严格按 YAML 数组（只输出 YAML）：
```yaml
- id: 1
  type: confirm|suggestion|missing-page|contradiction|duplicate
  title: "一句话标题"
  description: "详细描述"
  affected_pages: ["sources/xxx.md", "concepts/yyy.md"]
  severity: high|medium|low
```
至少 5 个 items。数字、参数、公式要严格。"""

    prompt = f"{system_prompt}\n\n{user_content}"
    try:
        response, stop_reason = call_with_retry(
            lambda: call_anthropic_protocol(prompt, config, max_tokens=8192),
            max_retries=3, label="stage-3.4",
        )
    except Exception as e:
        print(f"[stage 3.4] LLM call failed after retries: {e}")
        return {"error": str(e)}

    if verbose:
        print(f"[stage 3.4] Response ({len(response)} chars, stop={stop_reason}):\n{response[:2000]}...\n")

    # Parse YAML
    text = response
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("yaml"):
            text = text[4:]
        if text.endswith("```"):
            text = text[:-3]

    try:
        import yaml
        items = yaml.safe_load(text.strip())
    except Exception:
        items = parse_simple_yaml(text.strip())
        if not isinstance(items, list):
            items = [items] if items else []

    if not isinstance(items, list):
        items = []

    # Write review pages to wiki/REVIEW/<review_type>/ (分子目录，一目了然)
    date_str = time.strftime("%Y-%m-%d")
    safe_source = re.sub(r'[^\w\s-]', '', raw_file.stem).strip()[:40]

    written = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        rtype = it.get("type", "suggestion")
        title = it.get("title", "Untitled")
        desc = it.get("description", "")
        affected = it.get("affected_pages", [])
        if isinstance(affected, str):
            affected = [affected]
        severity = it.get("severity", "medium")

        # Build short-slug from title (kebab-case, English only, max 40 chars)
        import unicodedata
        slug_raw = unicodedata.normalize('NFKD', title).encode('ascii', 'ignore').decode('ascii')
        short_slug = re.sub(r'[^\w\s-]', '', slug_raw).strip().lower()
        short_slug = re.sub(r'[-\s]+', '-', short_slug)[:50].strip('-')
        if not short_slug:
            short_slug = f"item-{written + 1}"

        reviews_dir = config.wiki_dir / "REVIEW" / rtype
        reviews_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{date_str}-{safe_source}-{short_slug}.md"
        page_path = reviews_dir / filename

        # Build wikilinks for affected pages
        affected_links = "\n".join(f"- [[{p.replace('.md', '')}]]" for p in affected)

        md = f"""---
type: review
review_type: {rtype}
severity: {severity}
affected_pages: [{', '.join(affected)}]
resolved: false
created: {date_str}
source_ingest: "{raw_file.stem}"
---

# [{rtype}] {title}

{desc}

## Affected Pages
{affected_links}

## Resolution
_待审核。处理完成后将 frontmatter 中 `resolved: false` 改为 `resolved: true`，下次 ingest 时自动清理。_
"""
        tmp = page_path.with_suffix(page_path.suffix + ".tmp")
        tmp.write_text(md, encoding="utf-8")
        tmp.rename(page_path)
        written += 1

    print(f"[stage 3.4] {written} review pages -> wiki/REVIEW/")

    # Also write JSON for tooling (backward compat)
    runtime_dir = config.runtime_dir
    runtime_dir.mkdir(parents=True, exist_ok=True)
    sugg_path = runtime_dir / "review-suggestions.json"
    sugg_data = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": config.llm_model,
        "stop_reason": stop_reason,
        "items": items,
    }
    tmp = sugg_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sugg_data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(sugg_path)

    return {"items": written, "stop_reason": stop_reason}
