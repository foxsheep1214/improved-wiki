"""Stage 2.8: Cross-source Query Resolution"""
from pathlib import Path
import re

def extract_query_blocks(file_blocks: list[tuple]) -> list[dict]:
    queries = []
    for idx, (path, content) in enumerate(file_blocks):
        if "/queries/" in path:
            title_match = re.search(r'title:\s*([^\n]+)', content)
            title = title_match.group(1).strip() if title_match else path.split("/")[-1]
            queries.append({
                "slug": Path(path).stem,
                "title": title,
                "block_index": idx,
                "path": path,
                "full_content": content,
            })
    return queries

def find_related_wiki_pages(wiki_root: Path, query_title: str, threshold: float = 0.7) -> list[str]:
    if not wiki_root.is_dir():
        return []
    related = []
    query_words = set(query_title.lower().split())
    for page_dir in [wiki_root / "concepts", wiki_root / "entities"]:
        if not page_dir.is_dir():
            continue
        for page_file in page_dir.glob("*.md"):
            try:
                content = page_file.read_text(encoding="utf-8", errors="ignore")
                title_match = re.search(r'title:\s*([^\n]+)', content)
                if title_match:
                    title = title_match.group(1).strip()
                    page_words = set(title.lower().split())
                    if page_words and query_words:
                        overlap = len(query_words & page_words) / len(query_words | page_words)
                        if overlap >= threshold:
                            related.append(page_file.stem)
            except:
                pass
    return related

def resolve_queries(file_blocks: list[tuple], wiki_root: Path, queries: list[dict]) -> dict:
    resolutions = {}
    for query in queries:
        related_pages = find_related_wiki_pages(wiki_root, query["title"])
        if not related_pages:
            resolutions[query["slug"]] = {
                "status": "kept",
                "resolution_pages": [],
                "reason": "未找到相关已有页面",
            }
        elif len(related_pages) >= 2:
            resolutions[query["slug"]] = {
                "status": "closed",
                "resolution_pages": related_pages,
                "reason": f"发现 {len(related_pages)} 个相关页面，答案完整",
            }
        else:
            resolutions[query["slug"]] = {
                "status": "rewritten_as_comparison",
                "resolution_pages": related_pages,
                "reason": "答案不完整，建议与相关页面对比",
            }
    return resolutions

def update_file_blocks_after_resolution(file_blocks: list[tuple], resolutions: dict) -> list[tuple]:
    closed_slugs = {slug for slug, res in resolutions.items() if res["status"] == "closed"}
    result = []
    for path, content in file_blocks:
        slug = Path(path).stem
        if "/queries/" in path and slug in closed_slugs:
            continue
        result.append((path, content))
    return result

def verify_query_resolution(checkpoint: dict) -> bool:
    return "query_resolutions" in checkpoint
