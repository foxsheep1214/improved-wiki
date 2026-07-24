
from __future__ import annotations

import json
import time
from pathlib import Path

from _config import Config
from _llm_api import call_anthropic_protocol
from _parse import parse_simple_yaml
from _retry import call_with_retry
from _page_ref import PageRef, PageRefError
from _paths import atomic_write
from _review_utils import review_id_for, resolve_review_path
from _schema import load_purpose_md, load_schema_md, schema_prompt_text


_REVIEW_TYPES = {
    "confirm",
    "suggestion",
    "missing-page",
    "contradiction",
    "duplicate",
}
_REVIEW_SEVERITIES = {"high", "medium", "low"}
_RESEARCH_REVIEW_TYPES = {"suggestion", "missing-page"}


def _review_preview(content: str, max_chars: int) -> str:
    """Return a bounded review preview without fabricating a broken file tail.

    Stage 3.4 used to pass ``content[:max_chars]`` to the reviewer.  That hard
    slice routinely ended in the middle of a word, wikilink, formula, or table
    row.  Because the prompt called the result a "page", the reviewer quite
    reasonably reported the synthetic preview boundary as source truncation.

    Keep a line-boundary prefix and the real file tail, separated by an
    explicit omission marker.  The tail lets the reviewer assess how the page
    actually ends while the marker makes it impossible to confuse the preview
    gap with content written to disk.
    """
    if max_chars <= 0 or len(content) <= max_chars:
        return content

    marker = (
        "\n\n> [!note] REVIEW PREVIEW GAP — 中间内容仅因审查上下文预算而省略；"
        f"原文件共 {len(content)} 字符，磁盘文件并未在此处结束。"
        "下面继续展示文件的真实结尾。\n\n"
    )
    tail_budget = min(400, max(240, max_chars // 4))
    head_budget = max(240, max_chars - tail_budget - len(marker))

    # End the prefix before a complete line, so a long wikilink/table row is
    # omitted rather than presented as a corrupt fragment.
    head_end = content.rfind("\n", 0, head_budget + 1)
    head = content[:head_end if head_end >= 0 else 0].rstrip()

    # Start the tail on a line boundary for the same reason.  If there is no
    # boundary in the tail window, omit the tail instead of manufacturing a
    # partial line.
    tail_start_floor = max(0, len(content) - tail_budget)
    tail_start = content.find("\n", tail_start_floor)
    tail = content[tail_start + 1:].lstrip() if tail_start >= 0 else ""
    return head + marker + tail


def _append_review_failure_log(config: Config, raw_file: Path, messages: list[str]) -> None:
    """Persist Stage 3.4 failure info to runtime_dir/ingest-warnings.log.

    Same entry format as _ingest_write._append_ingest_warning_log (not imported
    directly: _ingest_write imports this module, so importing back would be
    circular). Failures here now raise instead of silently degrading to 0
    reviews; the log keeps the failure inspectable after the run ends.
    """
    log_path = config.runtime_dir / "ingest-warnings.log"
    try:
        config.runtime_dir.mkdir(parents=True, exist_ok=True)
        entry_lines = [f"## {time.strftime('%Y-%m-%dT%H:%M:%S')} | {raw_file.name}", ""]
        entry_lines += [f"{i}. {m}" for i, m in enumerate(messages, 1)]
        entry_lines.append("")
        with log_path.open("a", encoding="utf-8") as f:
            f.write("\n".join(entry_lines) + "\n")
    except OSError as e:
        print(f"  ⚠️  failed to write ingest-warnings.log: {e}")


def _validate_review_items(items: list, config: Config) -> list[dict]:
    """Validate and normalize the complete Stage 3.4 response before writing.

    Review type is used as a directory name and affected pages become
    wikilinks, so permissive coercion here is unsafe: one malformed item must
    fail the response before any partial review set reaches disk.
    """
    normalized: list[dict] = []
    errors: list[str] = []

    for index, item in enumerate(items, 1):
        prefix = f"item {index}"
        if not isinstance(item, dict):
            errors.append(f"{prefix}: expected an object")
            continue

        rtype = item.get("type")
        if rtype not in _REVIEW_TYPES:
            errors.append(
                f"{prefix}: type must be one of "
                f"{', '.join(sorted(_REVIEW_TYPES))}")

        severity = item.get("severity")
        if severity not in _REVIEW_SEVERITIES:
            errors.append(
                f"{prefix}: severity must be one of "
                f"{', '.join(sorted(_REVIEW_SEVERITIES))}")

        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            errors.append(f"{prefix}: title must be a non-empty string")
            title = ""
        else:
            title = title.strip()

        description = item.get("description")
        if not isinstance(description, str) or not description.strip():
            errors.append(
                f"{prefix}: description must be a non-empty string")
            description = ""
        else:
            description = description.strip()

        affected_raw = item.get("affected_pages")
        affected: list[str] = []
        if not isinstance(affected_raw, list):
            errors.append(f"{prefix}: affected_pages must be a list")
        else:
            for page_index, value in enumerate(affected_raw, 1):
                if not isinstance(value, str) or not value.strip():
                    errors.append(
                        f"{prefix}: affected_pages[{page_index}] must be "
                        "a non-empty string")
                    continue
                try:
                    ref = PageRef.parse(
                        value, config.wiki_root, config.wiki_dir)
                except PageRefError as exc:
                    errors.append(
                        f"{prefix}: affected_pages[{page_index}] is unsafe: "
                        f"{exc}")
                    continue
                if ref.wiki_relative not in affected:
                    affected.append(ref.wiki_relative)

        queries_raw = item.get("search_queries")
        queries: list[str] = []
        if not isinstance(queries_raw, list):
            errors.append(f"{prefix}: search_queries must be a list")
        else:
            for query_index, value in enumerate(queries_raw, 1):
                if not isinstance(value, str) or not value.strip():
                    errors.append(
                        f"{prefix}: search_queries[{query_index}] must be "
                        "a non-empty string")
                    continue
                query = value.strip()
                if query not in queries:
                    queries.append(query)

        if rtype in _RESEARCH_REVIEW_TYPES:
            if not 2 <= len(queries) <= 3:
                errors.append(
                    f"{prefix}: {rtype} requires 2-3 unique "
                    "search_queries")
        elif queries:
            errors.append(
                f"{prefix}: {rtype or 'this review type'} must use an empty "
                "search_queries list")

        normalized.append({
            "id": item.get("id"),
            "type": rtype,
            "title": title,
            "description": description,
            "affected_pages": affected,
            "severity": severity,
            "search_queries": queries,
        })

    if errors:
        preview = "; ".join(errors[:12])
        if len(errors) > 12:
            preview += f"; +{len(errors) - 12} more"
        raise ValueError(f"invalid Stage 3.4 review schema: {preview}")
    return normalized


def _render_review_page(rtype: str, title: str, desc: str, affected: list[str],
                        queries: list[str], severity: str, date_str: str,
                        source_stem: str) -> str:
    """Render one review item's markdown page (frontmatter + body).

    Extracted from the write loop so the frontmatter shape — including the
    NashSU ``search_queries`` field — is unit-testable without an LLM call.
    """
    affected_links = "\n".join(f"- [[{p.replace('.md', '')}]]" for p in affected)
    # Deep-Research seed queries (NashSU searchQueries parity) — shown in the
    # body only when present, so non-applicable types render clean.
    if queries:
        search_section = (
            "\n## Search Queries (Deep Research)\n"
            + "\n".join(f"- {q}" for q in queries)
            + "\n"
        )
    else:
        search_section = ""
    # Content-stable review id (NashSU review-store.ts reviewIdFor parity):
    # FNV-1a over (type :: normalized-title). The same logical review keeps the
    # same id across re-ingest, so resolved state survives via field-union dedup.
    review_id = review_id_for(rtype, title)
    return f"""---
type: review
review_id: {review_id}
review_type: {rtype}
severity: {severity}
affected_pages: [{', '.join(affected)}]
search_queries: [{', '.join(f'"{q}"' for q in queries)}]
resolved: false
created: {date_str}
source_ingest: "{source_stem}"
---

# [{rtype}] {title}

{desc}

## Affected Pages
{affected_links}
{search_section}
## Resolution
_待审核。处理完成后将 frontmatter 中 `resolved: false` 改为 `resolved: true`（已解决项会保留为审计记录，不会被删除）。_
"""


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

    Output: wiki/REVIEW/<type>/<type>-<topic>-<YYYYMMDD>.md — human-browsable review pages
    (see _review_utils.review_filename; frontmatter review_id = NashSU content hash).
    Each page has frontmatter `resolved: false`. When resolved, user changes to true.
    Resolved pages are KEPT (never auto-deleted) — the content-stable review_id +
    resolved-wins dedup keeps them resolved across re-ingest (NashSU parity).
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
        new_pages.append(
            f"### {path} (完整文件 {len(content)} 字符；以下是带真实结尾的审查预览)\n"
            f"{_review_preview(content, 1500)}")

    # Sample existing wiki pages (up to 40)
    existing_pages: list[str] = []
    for sub in [
        "sources", "concepts", "entities", "comparisons", "findings",
        "methodology",
    ]:
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
            existing_pages.append(
                f"### {sub}/{f.name} (完整文件 {len(body)} 字符；以下是带真实结尾的审查预览)\n"
                f"{_review_preview(body, 1000)}")
            if len(existing_pages) >= 40:
                break
        if len(existing_pages) >= 40:
            break

    schema_text = schema_prompt_text(load_schema_md(config))
    purpose_text = load_purpose_md(config).strip()[:6000]

    user_content = f"""# schema.md
{schema_text}

# purpose.md
{purpose_text}

# Newly generated pages (from {raw_file.stem})
{chr(10).join(new_pages)}

# Existing wiki pages (sample of {len(existing_pages)})
{chr(10).join(existing_pages[:40])}
"""

    system_prompt = f"""你是 {config.wiki_root.name} 的 review agent。审阅当前 wiki 内容，找出 5 类可疑项：
1. confirm（需要人工确认）：数字、术语、矛盾点
2. suggestion（研究建议）：本源提出但未回答的研究问题、值得寻找的相关资料/来源、值得探索的连接或对比、内容不完整应补充（NashSU: "a research question, source type, or comparison that would materially improve the wiki"）
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
  search_queries: ["keyword query 1", "keyword query 2"]
```
对 suggestion 和 missing-page 类型，search_queries 必填：2-3 条关键词式 web 搜索查询
（用于 Deep Research——关键词丰富、具体、面向搜索引擎，不是标题或整句）；
其它类型用空数组 []。
输入中的页面可能是“开头 + REVIEW PREVIEW GAP + 文件真实结尾”的有界预览。
PREVIEW GAP 是审查上下文主动省略的中间内容，不是磁盘文件的截断点；不得把预览开头的末尾、
省略标记或其附近的半句话当成页面损坏。判断页面是否在结尾截断时，只能依据标记之后明确注明的
“文件真实结尾”。
只报告真实发现的问题；如果确实没有发现任何可疑项，输出空数组 []。不要为了凑数量而编造问题或写"未发现问题"类的确认项。数字、参数、公式要严格。"""

    prompt = f"{system_prompt}\n\n{user_content}"
    try:
        response, stop_reason = call_with_retry(
            lambda: call_anthropic_protocol(prompt, config, max_tokens=8192),
            max_retries=3, label="stage-3.4",
        )
    except Exception as e:
        # No silent degradation to 0 reviews: pages are already safely on disk
        # (3.4 runs post-write) and the conversation cache makes a resume cheap,
        # so fail loud and let the operator re-run.
        msg = f"stage 3.4 review LLM call failed after retries: {e}"
        _append_review_failure_log(config, raw_file, [msg])
        raise RuntimeError(msg) from e

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

    # A genuinely empty YAML list (``[]``) is now a legitimate "no issues
    # found" response (the "at least 5 items" padding requirement was dropped
    # for NashSU parity — 2026-07-15). Only a response that fails to parse
    # into a list at all (garbage text, a bare scalar, truncated output) is
    # treated as a real parse failure and raises.
    parse_failed = False
    try:
        import yaml
        parsed = yaml.safe_load(text.strip())
        if isinstance(parsed, list):
            items = parsed
        else:
            items = []
            parse_failed = True
    except Exception:
        fallback = parse_simple_yaml(text.strip())
        if isinstance(fallback, list) and fallback:
            items = fallback
        elif fallback:
            items = [fallback]
        else:
            items = []
            parse_failed = True

    if parse_failed:
        msg = (f"stage 3.4 review YAML parse failed — response did not "
               f"parse into a usable items list "
               f"(response {len(response)} chars, stop={stop_reason})")
        _append_review_failure_log(config, raw_file, [msg])
        raise RuntimeError(msg)

    try:
        items = _validate_review_items(items, config)
    except ValueError as exc:
        msg = f"stage 3.4 review schema validation failed: {exc}"
        _append_review_failure_log(config, raw_file, [msg])
        raise RuntimeError(msg) from exc

    # Write review pages to wiki/REVIEW/<review_type>/ (分子目录，一目了然).
    # Filename is human-readable <type>-<topic>-<YYYYMMDD>.md (see
    # _review_utils.review_filename); the canonical identity stays the
    # content-hash review_id in frontmatter, which sweep/process-reviews key on.
    date_str = time.strftime("%Y-%m-%d")      # frontmatter created:
    date_compact = time.strftime("%Y%m%d")    # filename segment

    written = 0
    page_refs: list[str] = []
    for it in items:
        rtype = it["type"]
        title = it["title"]
        desc = it["description"]
        affected = it["affected_pages"]
        severity = it["severity"]
        # NashSU searchQueries parity — 2-3 web search queries for Deep Research,
        # populated by the LLM for suggestion/missing-page reviews. Surfaced on
        # the review page so deep-research can seed its queries without a separate
        # optimize-research-topic LLM call.
        queries = it["search_queries"]

        reviews_dir = config.wiki_dir / "REVIEW" / rtype
        reviews_dir.mkdir(parents=True, exist_ok=True)
        # Readable <type>-<topic>-<date>.md name + content-hash id (collision-safe).
        page_path, _rid = resolve_review_path(reviews_dir, rtype, title, date_compact)

        md = _render_review_page(rtype, title, desc, affected, queries,
                                 severity, date_str, raw_file.stem)
        atomic_write(page_path, md)
        page_refs.append(PageRef.parse(
            page_path, config.wiki_root, config.wiki_dir).project_relative)
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
    atomic_write(sugg_path, json.dumps(sugg_data, ensure_ascii=False, indent=2))

    return {
        "items": written,
        "stop_reason": stop_reason,
        "page_refs": page_refs,
    }
