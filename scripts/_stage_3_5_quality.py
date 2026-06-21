"""Stage 3.5: Quality Scoring Card

Refactored 2026-06-21 for explicit stage naming.
"""
import time
from datetime import datetime


def _stage_3_5_calculate_quality_score(extracted_text, original_char_estimate, extracted_images,
    captioned_images, file_blocks, review_items, concept_merge_stats, dedup_was_run):
    metrics = {}
    text_coverage = min(1.0, len(extracted_text) / max(original_char_estimate, 1000))
    metrics["text_coverage"] = {"score": text_coverage, "weight": 0.25,
                                "details": "{}/{} chars".format(len(extracted_text), original_char_estimate)}

    # image_quality = caption coverage (captioned/extracted); 1.0 when no images.
    if extracted_images > 0:
        image_quality = min(1.0, captioned_images / max(extracted_images, 1))
    else:
        image_quality = 1.0
    metrics["image_quality"] = {"score": image_quality, "weight": 0.20,
                                "details": "{}/{} captioned".format(captioned_images, extracted_images)}

    concept_count = sum(1 for path, _ in file_blocks if "/concepts/" in path or path.startswith("concepts/"))
    text_kb = len(extracted_text) / 1000
    concept_density = min(1.0, (concept_count / max(text_kb, 1)) / 3.0)
    metrics["concept_density"] = {"score": concept_density, "weight": 0.25,
                                  "details": "{} concepts / {:.1f} KB".format(concept_count, text_kb)}

    total_blocks = len(file_blocks)
    review_quality = max(0.0, 1.0 - min(1.0, review_items / max(total_blocks, 1)))
    metrics["review_quality"] = {"score": review_quality, "weight": 0.20,
                                 "details": "{} review items / {} blocks".format(review_items, total_blocks)}

    # dedup_completeness only when dedup actually ran (multi-chunk); else excluded (renormalized out).
    if dedup_was_run:
        before, after = concept_merge_stats
        dedup_completeness = after / before if before > 0 else 1.0
        metrics["dedup_completeness"] = {"score": dedup_completeness, "weight": 0.10,
                                         "details": "{} -> {} concepts".format(before, after)}

    overall_score = sum(m["score"] * m["weight"] for m in metrics.values())
    return {"overall_score": round(overall_score, 3), "metrics": metrics,
            "needs_review": overall_score < 0.65}


def _stage_3_5_generate_quality_card_md(source_stem, quality_result):
    score = quality_result["overall_score"]
    metrics = quality_result["metrics"]
    needs_review = quality_result["needs_review"]
    display = {"text_coverage": "文本覆盖", "image_quality": "图片质量",
               "concept_density": "概念密度", "review_quality": "Review质量",
               "dedup_completeness": "去重完整性"}
    md = "---\ntype: audit\nsource: {stem}\ndate: {date}\noverall_score: {score}\nneeds_review: {nr}\n---\n\n# 质量评分卡 - {stem}\n\n## 总体评分\n\n**{pct}** {verdict}\n\n## 维度评分\n\n| 维度 | 评分 | 权重 | 说明 |\n|------|------|------|------|\n".format(
        stem=source_stem, date=datetime.now().isoformat(), score=score, nr=needs_review,
        pct="{:.1%}".format(score),
        verdict="⚠️ 需要复审" if needs_review else "✅ 合格")
    for name, metric in metrics.items():
        md += "| {} | {:.0f}% | {:.0f}% | {} |\n".format(
            display.get(name, name), metric["score"] * 100, metric["weight"] * 100, metric["details"])
    md += "\n## 诊断\n\n"
    if needs_review:
        md += "⚠️ **该 ingest 需要人工复审：**\n\n"
        diag = {"text_coverage": "文本覆盖率不足", "image_quality": "图片质量问题",
                "concept_density": "概念生成密度低", "review_quality": "review items过多",
                "dedup_completeness": "去重不完整"}
        for name, metric in metrics.items():
            if metric["score"] < 0.7:
                md += "- {}：{}\n".format(diag.get(name, name), metric["details"])
    else:
        md += "✅ **质量良好，无需特殊关注。**\n"
    return md


def _stage_3_5_verify_quality_scoring(checkpoint):
    return "quality_metrics" in checkpoint


def _stage_3_5_estimate_expected_chars(raw_file) -> int:
    """Estimate expected extracted-text volume from the raw source's page count.

    Without this, text_coverage compared extracted_text's length against
    itself (always ~1.0) and could never detect truncated/botched
    extraction. 500 chars/page matches the text/scanned-PDF threshold used
    by _stage_1_extract.py's PDF-type detection (signal ①), so a PDF that
    legitimately extracts less than that per page is already considered
    suspect there too.
    """
    EXPECTED_CHARS_PER_PAGE = 500
    if raw_file.suffix.lower() == ".pdf":
        try:
            import fitz
            with fitz.open(raw_file) as doc:
                return max(len(doc) * EXPECTED_CHARS_PER_PAGE, 1000)
        except Exception:
            pass
    return 1000  # no page-count signal available (pptx/docx/txt/md) — floor only


def stage_3_5_quality(raw_file, config, extracted_text, images_extracted,
                      images_captioned, file_blocks, review_items,
                      concept_merge_stats, dedup_was_run, *, verbose: bool = False) -> dict:
    """Stage 3.5: Quality scoring card (always runs; flags needs_review < 0.65).

    Computes the quality score, prints it, and writes a quality card to
    REVIEW/audit/ when needs_review. Returns the quality_result dict.
    """
    quality_result = _stage_3_5_calculate_quality_score(
        extracted_text, _stage_3_5_estimate_expected_chars(raw_file),
        images_extracted, images_captioned,
        file_blocks, review_items, concept_merge_stats, dedup_was_run)
    print(f"  [stage 3.5] Quality score: {quality_result['overall_score']:.1%}"
          f"{' ⚠️ needs_review' if quality_result['needs_review'] else ' ✅'}")
    if quality_result["needs_review"]:
        audit_dir = config.wiki_dir / "REVIEW" / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        card = _stage_3_5_generate_quality_card_md(raw_file.stem, quality_result)
        (audit_dir / f"{time.strftime('%Y-%m-%d')}-{raw_file.stem}-quality.md"
         ).write_text(card, encoding="utf-8")
        print(f"  [stage 3.5] Wrote quality card → REVIEW/audit/")
    return quality_result
