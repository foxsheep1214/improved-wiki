# PDF 理解工作流（截击机方案等中文技术文档）

## 适用场景
- 中文技术方案 PDF（含图表）
- 需要提取系统架构、数据链路、指标参数
- 文档中有图片需要 AI 视觉分析

## 工作流

### Step 1：复制原始文件到文档目录
```bash
mkdir -p ~/Documents/{项目名}
cp ~/Downloads/{原始文件}.pdf ~/Documents/{项目名}/
```

### Step 2：文本提取（pdftotext）
```bash
pdftotext ~/Documents/{项目名}/{文件}.pdf - 2>/dev/null | head -300  # 看前300行了解结构
pdftotext ~/Documents/{项目名}/{文件}.pdf - 2>/dev/null | tail -200  # 看结尾
pdftotext ~/Documents/{项目名}/{文件}.pdf - 2>/dev/null | wc -l       # 总行数
```

### Step 3：找图（pdfimages）
```bash
pdfimages -list ~/Documents/{项目名}/{文件}.pdf 2>&1 | head -30
# 看有多少图片、哪些页面有图、图的大小
```

### Step 4：渲染关键页面
```bash
mkdir -p /tmp/pdf_pages
pdftoppm -r 150 -f {起始页} -l {结束页} ~/Documents/{项目名}/{文件}.pdf /tmp/pdf_pages/page
# -r 150: 150 DPI
# -f/-l: 页码范围
```

### Step 5：PPM → PNG（sips，macOS内置）
```bash
for f in /tmp/pdf_pages/*.ppm; do
  sips -s format png "$f" --out "${f%.ppm}.png" 2>/dev/null
done
ls /tmp/pdf_pages/*.png | wc -l  # 确认转换数量
```

### Step 6：图片分析
```bash
tesseract /tmp/pdf_pages/page-XX.png stdout -l chi_sim 2>&1
# 或用 browser_vision 分析图片内容
```

### Step 7：写理解文档
- 路径规范：`~/Documents/{项目名}/{文件名}-理解.md`
- 结构：产品定位 → 系统架构 → 核心指标 → 功能要求 → 软件/硬件设计 → 存疑点
- 有图时：引用 OCR/vision 分析结果，标注图片来源页码

### Step 8：清理临时文件
```bash
rm -rf /tmp/pdf_pages
```

## 关键陷阱

### sips vs ImageMagick
- macOS 内置 `sips` 可直接转 PPM→PNG，不需要安装 ImageMagick/Pillow
- 不要在 execute_code 里 import PIL（大多数环境没有 Pillow）

### pdftotext 中文编码
- 某些 PDF 中文是 CID 字体，pdftotext 可能输出乱码，但仍是可搜索文本
- 检查方法：`pdftotext file.pdf - | head -5`，有文字输出就有内容

### 图片页码定位
- pdfimages 列出的页码是准确的
- 但同一页可能有多张图（表格/截图混排），挨个分析时注意

### 图片分析工具选择
- `tesseract` 对中文文档 OCR 基础识别（需安装 chi_sim 语言包）
- `browser_vision` 可直接在会话中分析图片内容
- 格式：`tesseract {图片路径} stdout -l chi_sim`

### HEIC 图片处理（macOS 照片）
- OCR 工具不直接支持 `.heic` 格式，需先用 `sips` 转换
- **解法**：用 `sips` 先转 JPEG/PNG 再分析：
  ```bash
  sips -s format jpeg "输入.heic" --out /tmp/输出.jpg
  tesseract /tmp/输出.jpg stdout -l chi_sim
  ```
- sips 是 macOS 内置工具，无需额外安装
- 转换后质量足够 AI 分析使用

## 参考案例
- 本次消化：~/Documents/截击机/截击机设计方案-理解.md（Thunder STD100截击机系统方案，2026-05-15）
