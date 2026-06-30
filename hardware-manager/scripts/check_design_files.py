#!/usr/bin/env python3
"""验证 DesignExample 所有文件是否真实有效。不靠文件大小，只靠内容判断。"""
import fitz, sys, zipfile
from pathlib import Path

BASE = Path.home() / "Documents/知识库/硬件设计知识库/raw/DesignExample"

def check_pdf(path):
    size = path.stat().st_size
    if size < 500:
        return False, f"极小({size}B)"
    try:
        doc = fitz.open(str(path))
    except:
        return False, "无法打开"
    if len(doc) == 0:
        doc.close(); return False, "0页"
    text = doc[0].get_text()[:300]
    doc.close()
    fakes = ["We can't find this page", "Error 404", "signals.js",
             "collectGeoLocationWithPrompt"]
    for p in fakes:
        if p in text:
            return False, f"假({p})"
    if size < 5000 and len(text.strip()) < 50:
        return False, f"空({len(text.strip())}字)"
    if "Designator" in text[:200] and "Quantity" in text[:200]:
        return False, "BOM(非PDF)"
    return True, "OK"

def check_zip(path):
    size = path.stat().st_size
    if size < 100:
        return False, f"极小({size}B)"
    try:
        with zipfile.ZipFile(path) as z:
            if len(z.namelist()) == 0:
                return False, "空ZIP"
            z.testzip()
        return True, "OK"
    except zipfile.BadZipFile:
        return False, "损坏(BadZip)"

def check_txt(path):
    size = path.stat().st_size
    if size < 50:
        return False, f"极小({size}B)"
    text = path.read_text()[:200]
    if "Error 404" in text or "We can't find" in text:
        return False, "假的(404)"
    if '"signals.js"' in text:
        return False, "假的(JS)"
    return True, "OK"

CHECKERS = {".pdf": check_pdf, ".zip": check_zip, ".txt": check_txt}

if __name__ == "__main__":
    fake = 0; ok = 0; skipped = 0
    for f in BASE.rglob("*"):
        if not f.is_file(): continue
        ext = f.suffix.lower()
        if ext not in CHECKERS: 
            skipped += 1; continue
        valid, reason = CHECKERS[ext](f)
        if not valid:
            fake += 1; f.unlink()
        else:
            ok += 1
    print(f"✅ 有效: {ok}  |  🗑 删除: {fake}  |  ⏭ 跳过: {skipped}")
