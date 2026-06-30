# Docx 原位编辑工作流

> 本文档是 `hardware-manager` skill 的支持文件。
> 最后更新：2026-05-11

## 何时使用

用户有现成的 docx 模板/彩页，需要替换/追加内容时，在原文基础上做 in-place 修改，不生成新文件。

## 工具准备

```bash
pip install python-docx -q
```

## 标准工作流

### 1. 读取文档结构

```python
from docx import Document

path = "/path/to/file.docx"
doc = Document(path)

# 读所有段落
for i, para in enumerate(doc.paragraphs):
    if para.text.strip():
        print(f"[{i}] {para.text}")

# 读所有表格
for i, table in enumerate(doc.tables):
    print(f"=== Table {i} ===")
    for j, row in enumerate(table.rows):
        for k, cell in enumerate(row.cells):
            if cell.text.strip():
                print(f"  [{j},{k}]: {cell.text[:100]}")
```

### 2. 定位目标段落/表格

通常按段落序号定位（枚举输出后人工确认序号）。

**注意**：同一段落的文本可能分散在多个 `run` 中，直接打印 `para.text` 会合并显示，但修改时需要找到包含目标文本的那个 `run`。

### 3. 修改文本

```python
p = doc.paragraphs[目标序号]

# 方式A：目标文本在单个 run 内
for run in p.runs:
    if '目标文本' in run.text:
        run.text = run.text.replace('目标文本', '新文本')
        break

# 方式B：文本被多个 run 拆分（少见）
if '目标文本' in ''.join(r.text for r in p.runs):
    full_text = ''.join(r.text for r in p.runs)
    p.runs[-1].text = full_text.replace('目标文本', '新文本')
    for run in p.runs[:-1]:
        run.text = ''
```

### 4. 保存

```python
doc.save(path)
```

## 陷阱

1. **段落文本被多个 run 拆分**：`para.text` 合并显示正常，但 `run.text` 中目标文本不存在于任何一个 run。解法：用 `''.join(r.text for r in p.runs)` 合并判断，再操作最后一个 run 并清空前面的 run。
2. **保存路径含中文**：python-docx 对中文路径支持良好，无需特殊处理。
3. **段落序号偏移**：修改后段落数量不变，但若删除了段落则后续序号会漂移。
4. **表格内容修改**：直接通过 `table.cell(row, col).text = '新文本'` 修改，但会丢失格式。

## Spotter Air 彩页修改记录

| 日期 | 修改内容 | 目标段落 |
|------|---------|---------|
| 2026-05-11 | 替换安装条件为机载载重要求 | 第7段 |
| 2026-05-11 | 补充固定翼/边境监测/敌我识别场景描述 | 第7段 |
