"""Stage 3.5: Quality Scoring Card"""
from datetime import datetime

def calculate_quality_score(extracted_text: str, original_char_estimate: int, extracted_images: int,
    expected_images: int, file_blocks: list[tuple], review_items: int, concept_merge_stats: tuple) -> dict:
    metrics = {}
    text_coverage = min(1.0, len(extracted_text) / max(original_char_estimate, 1000))
    metrics["text_coverage"] = {"score": text_coverage, "weight": 0.25, "details": f"{len(extracted_text)}/{original_char_estimate} chars"}
    
    image_quality = (extracted_images / expected_images) * 0.8 if expected_images > 0 else (1.0 if extracted_images == 0 else 0.5)
    metrics["image_quality"] = {"score": image_quality, "weight": 0.20, "details": f"{extracted_images}/{expected_images} images"}
    
    concept_count = sum(1 for path, _ in file_blocks if "/concepts/" in path)
    text_kb = len(extracted_text) / 1000
    concept_density = min(1.0, (concept_count / max(text_kb, 1)) / 3.0)
    metrics["concept_density"] = {"score": concept_density, "weight": 0.25, "details": f"{concept_count} concepts / {text_kb:.1f} KB text"}
    
    total_blocks = len(file_blocks)
    review_quality = max(0.0, 1.0 - min(1.0, review_items / max(total_blocks, 1)))
    metrics["review_quality"] = {"score": review_quality, "weight": 0.20, "details": f"{review_items} review items / {total_blocks} blocks"}
    
    before, after = concept_merge_stats
    dedup_completeness = after / before if before > 0 else 1.0
    metrics["dedup_completeness"] = {"score": dedup_completeness, "weight": 0.10, "details": f"{before} → {after} concepts"}
    
    overall_score = sum(m["score"] * m["weight"] for m in metrics.values())
    return {"overall_score": round(overall_score, 3), "metrics": metrics, "needs_review": overall_score < 0.65}

def generate_quality_card_md(source_stem: str, quality_result: dict) -> str:
    score = quality_result["overall_score"]
    metrics = quality_result["metrics"]
    needs_review = quality_result["needs_review"]
    md = f"""---
type: audit
source: {source_stem}
date: {datetime.now().isoformat()}
overall_score: {score}
needs_review: {needs_review}
---

# 质量评分卡 - {source_stem}

## 总体评分

**{score:.1%}** {'⚠️ 需要复审' if needs_review else '✅ 合格'}

## 维度评分

| 维度 | 评分 | 权重 | 说明 |
|------|------|------|------|
"""
    for name, metric in metrics.items():
        display_name = {"text_coverage": "文本覆盖", "image_quality": "图片质量", "concept_density": "概念密度", "review_quality": "Review质量", "dedup_completeness": "去重完整性"}.get(name, name)
        score_pct = metric["score"] * 100
        weight_pct = metric["weight"] * 100
        md += f"| {display_name} | {score_pct:.0f}% | {weight_pct:.0f}% | {metric['details']} |\n"
    
    md += "\n## 诊断\n\n"
    if needs_review:
        md += "⚠️ **该 ingest 需要人工复审：**\n\n"
        for name, metric in metrics.items():
            if metric["score"] < 0.7:
                display_name = {"text_coverage": "文本覆盖率不足", "image_quality": "图片质量问题", "concept_density": "概念生成密度低", "review_quality": "review items过多", "dedup_completeness": "去重不完整"}.get(name, name)
                md += f"- {display_name}：{metric['details']}\n"
    else:
        md += "✅ **质量良好，无需特殊关注。**\n"
    return md

def verify_quality_scoring(checkpoint: dict) -> bool:
    return "quality_metrics" in checkpoint
