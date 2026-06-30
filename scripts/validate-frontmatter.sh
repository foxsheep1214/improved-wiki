#!/usr/bin/env bash
# validate-frontmatter.sh — Post-processing: 验证并修复缺失的 frontmatter 字段
# 在每次 ingest 完成后运行，确保所有页面都有完整的 frontmatter

set -e

# 默认 wiki 目录是当前目录
WIKI_ROOT="${1:-.}"

echo "[frontmatter] 验证 frontmatter 元数据 ($WIKI_ROOT)"
echo ""

# 查找所有 .md 文件
MD_FILES=$(find "$WIKI_ROOT/wiki" -name "*.md" -type f 2>/dev/null)

if [ -z "$MD_FILES" ]; then
    echo "没有找到 .md 文件"
    exit 0
fi

total=$(echo "$MD_FILES" | wc -l | tr -d ' ')
fixed=0

for md in $MD_FILES; do
    # 检查是否有 frontmatter
    if ! head -1 "$md" | grep -q '^---$'; then
        continue
    fi

    # 检查 frontmatter 是否完整
    # 简单检查：是否有 type, title, created, updated
    has_type=$(grep -E "^type:" "$md" | head -1)
    has_title=$(grep -E "^title:" "$md" | head -1)
    has_created=$(grep -E "^created:" "$md" | head -1)
    has_updated=$(grep -E "^updated:" "$md" | head -1)

    if [ -z "$has_type" ] || [ -z "$has_title" ] || [ -z "$has_created" ] || [ -z "$has_updated" ]; then
        basename=$(basename "$md")
        echo "  ✗ $basename: 缺少必要字段"

        # 尝试修复
        year=$(date +%Y)
        month=$(date +%m)
        day=$(date +%d)

        # 在第二个 --- 之后插入 frontmatter
        sed -i '' '
        /---$/a\
type: unknown\
title: "'$basename'"\
created: "'$year-$month-$day'"\
updated: "'$year-$month-$day'"\
tags: []\
related: []\
' "$md" 2>/dev/null && echo "  → 已自动补全"
        ((fixed++))
    else
        echo "  ✓ $basename"
    fi
done

echo ""
echo "[frontmatter] 完成: $total 个文件, $fixed 个已修复"