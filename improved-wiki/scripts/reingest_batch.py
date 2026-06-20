#!/usr/bin/env python3
"""reingest_batch.py — 对已有图片的书重跑 ingest.py Stage 1→2→3

处理逻辑：
1. 在 raw/ 中查找 PDF
2. 备份源页并删除（绕过 Stage 0 去重）
3. 清理 stale checkpoint
4. 调用 ingest.py
5. 记录结果
"""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Documents/知识库/HardwareWiki"
INGEST = Path.home() / ".agents/skills/improved-wiki/scripts/ingest.py"
RUNTIME = PROJECT / ".llm-wiki"
RAW = PROJECT / "raw"
SOURCES = PROJECT / "wiki" / "sources"

# Books to re-ingest: all 21 text-only + 5 stubs
# We list them explicitly
TARGETS: list[tuple[str, str]] = [
    # (source_page_stem, raw_subpath relative to raw/)
    # Stub books (5) — never fully ingested
    ("Electromagnetic Waves and Antennas - 2016 - Orfanidis", "book"),
    ("High Speed Digital Design - 1993 - Johnson", "book"),
    ("INA1H94-SEP", "datasheet/05_放大器与比较器/差分放大器"),
    ("Microelectronic Circuits - 2020 - Sedra", "book"),
    ("硬件十万个为什么 无源器件篇 - 2024 - 朱晓明", "book"),
    # Text-only books that missed Stage 0.5 (21)
    ("EMC电磁兼容设计与测试案例分析 - 2018 - 郑军奇", "book"),
    ("Electrical Power Systems Technology - 2009 - Fardo", "book"),
    ("Electronic Filter Design Handbook - 2006 - Williams", "book"),
    ("High Speed Serial IO Made Simple - 2005 - Athavale", "book"),
    ("High Speed Signal Propagation - 2003 - Johnson", "book"),
    ("High-Speed Digital System Design - 2000 - Hall", "book"),
    ("Lessons in Electric Circuits Volume III Semiconductors - 2007 - Kuphaldt", "book"),
    ("Lessons in Electric Circuits Volume IV Digital - 2007 - Kuphaldt", "book"),
    ("Microwave and RF Design A Systems Approach - 2013 - Steer", "book"),
    ("RF and Microwave Circuit Design Theory and Applications - 2021 - Free", "book"),
    ("Reliability Engineering for Electronic Design - 2021 - Fuqua", "book"),
    ("Right the First Time Vol1 - 2003 - Ritchey", "book"),
    ("传感器原理及应用 - 2017 - 苑会娟", "book"),
    ("功率半导体器件 原理特性和可靠性 - 2013 - Lutz", "book"),
    ("实用开关电源设计 - 2006 - Lenk", "book"),
    ("嵌入式系统设计 - 2013 - Marwedel", "book"),
    ("开关电源SPICE仿真与实用设计 - 2009 - Basso", "book"),
    ("模拟电子技术基础 - 2022 - 童诗白", "book"),
    ("硬件十万个为什么 开发流程篇 - 2024 - 王玉皞", "book"),
    ("高速电路设计进阶 - 2024 - 王剑宇", "book"),
    ("从零开始学散热 - 2018 - 陈继良", "book"),
]

results: list[dict] = []


def find_pdf(stem: str, raw_subpath: str) -> Path | None:
    """Find PDF in raw/."""
    pdf = RAW / raw_subpath / f"{stem}.pdf"
    if pdf.exists():
        return pdf
    # Try recursive search
    for pdf in RAW.rglob(f"{stem}.pdf"):
        return pdf
    return None


def clear_runtime(stem: str) -> None:
    """Clear lock and stale progress for this book."""
    (RUNTIME / ".ingest-lock").unlink(missing_ok=True)
    # Clear progress files
    progress_dir = RUNTIME / "ingest-progress"
    if progress_dir.exists():
        for f in progress_dir.glob("*.json"):
            f.unlink()


def main() -> None:
    env = {**__import__("os").environ, "IMPROVED_WIKI_ROOT": str(PROJECT)}
    ok = 0
    fail = 0

    for i, (stem, raw_subpath) in enumerate(TARGETS):
        print(f"\n{'='*60}")
        print(f"[{i+1}/{len(TARGETS)}] {stem[:60]}")
        print(f"{'='*60}")

        pdf = find_pdf(stem, raw_subpath)
        if not pdf:
            print(f"  ✗ PDF not found for '{stem}'")
            fail += 1
            continue

        # Backup source page
        src = SOURCES / f"{stem}.md"
        bak = SOURCES / f"{stem}.reingest-bak.md"
        if src.exists():
            shutil.copy2(src, bak)
            src.unlink()
            print(f"  [backup] Source page → .reingest-bak")

        # Clear stale state
        clear_runtime(stem)

        # Run ingest
        t0 = time.time()
        try:
            result = subprocess.run(
                [sys.executable, str(INGEST), str(pdf)],
                env=env, capture_output=True, text=True, timeout=3600,
            )
            elapsed = time.time() - t0

            if result.returncode == 0:
                ok += 1
                results.append({"stem": stem, "status": "ok", "elapsed": elapsed})
                print(f"  ✅ OK ({elapsed:.0f}s)")
                # Print key lines
                for line in result.stdout.split("\n"):
                    if any(kw in line for kw in ["Result:", "[OK]", "[stage 2.3] Done", "files_written"]):
                        print(f"  {line.strip()[:120]}")
            else:
                fail += 1
                results.append({"stem": stem, "status": "failed", "elapsed": elapsed})
                print(f"  ❌ Failed ({elapsed:.0f}s)")
                # Restore backup
                if bak.exists():
                    shutil.move(str(bak), str(src))
                    print(f"  [restore] Source page restored from backup")
                print(f"  stderr: {result.stderr[-500:]}")
        except subprocess.TimeoutExpired:
            fail += 1
            results.append({"stem": stem, "status": "timeout"})
            print(f"  ❌ Timeout (1h)")
            if bak.exists():
                shutil.move(str(bak), str(src))

    # Summary
    print(f"\n{'='*60}")
    print(f"Summary: {ok} OK, {fail} Failed out of {len(TARGETS)}")
    for r in results:
        print(f"  {'✅' if r['status'] == 'ok' else '❌'} {r['stem'][:60]} ({r.get('elapsed', '?')}s)")


if __name__ == "__main__":
    main()
