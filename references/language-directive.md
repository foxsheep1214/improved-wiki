# Output Language Directive（NashSU `outputLanguage` parity）

**KB 只允许两种语言（用户裁定 2026-07-15）：** 默认 `auto` = 检测源文档语言，中文源 → 中文页，**其余任何语言**（英文、日文、法文、挪威文……）一律 → 英文页。不再是"每种源语言各自出一种页面语言"——旧策略会让单页语言（挪威语、法语等）混进 KB，且容易被误判触发（见下"已知误判"）。也可以用 env 锁定整个 KB 用同一语言（不受此折叠规则约束，原样透传）。以强指令 `## ⚠️ MANDATORY OUTPUT LANGUAGE` 注入提示词，且**始终原样保留专有名词/技术标识符/URL/文件名/论文标题**（如 BGA、IPC-4101、Tg 即便在非英文模式下也保留）。NashSU 把它注入约 7 个 prompt builder，含 ingest。

## 移植状态

improved-wiki 早就移植了 `scripts/_language.py`（`detect_language` + `build_language_directive` + `IMPROVED_WIKI_OUTPUT_LANGUAGE` 环境变量覆盖），但过去只接入了语义 lint，**没有**接入 ingest 的生成/分析提示词。已于 2026-06-29（commit c359232）修复：`build_language_directive(<源文本样本>)` 现在在 `# Role` 之前注入全部 active 生成/分析 builder（Stage 2.2/2.4/2.6；2.1、2.7 与独立 2.9 后续均已移除），默认 auto（按源文本检测后折叠为中/英二选一）。测试覆盖：`test_ingest_language_directive.py`（英文→英文、中文→中文、env 覆盖三种场景）+ `test_language.py`（`TestOutputLanguageCollapsesToTwoLanguages`：法/日/挪威源均折叠为英文）。

## 已知误判 & 已修（2026-07-15）

`detect_language` 内部一些拉丁语系判定曾经只要求"命中 1 个功能词"（如挪威语集合里的 `for`——英语里也极常见）。真实事故：一篇纯英文 arXiv 论文（Aalborg University），作者名带 `æ`/`ø`（如 Damkjær、Ørum），摘要里普普通通一个 "for" 就把整页误判成挪威语。已把 Norwegian / Danish / Swedish / Finnish / Turkish / Romanian / Portuguese 全部统一成和 German/French 一致的"≥2 个功能词命中"门槛（`_language.py` `_detect_latin`）。`detect_language` 本身仍能正确识别真实的挪威语/丹麦语等（回归测试见 `TestDiacriticNameNotNordic`），只是不再被单个碰巧同形的英文词触发。此前"São Paulo 陷阱"类误判都靠逐本设 `IMPROVED_WIKI_OUTPUT_LANGUAGE` 硬覆盖绕过；现在从根上修了 Portuguese 判定，且即便再误判，`get_output_language` 的二语言折叠也兜底成英文，不会再产生第三种页面语言。

**第二轮扫描（同日）：** 对 RadarWiki（10,623 页）+ HardwareWiki（4,151 页）+（准备迁移）硬件设计知识库（655 页）逐页跑 `detect_language`，起初命中 25 个"非中/非英"误判，逐一定位根因后修复到剩 2 个真阳性（就是本次事故的挪威语源页 + 它派生出的对比页，需要重新生成/翻译，不是检测逻辑的锅）：

- **Greek 2 字符连写**：`_GREEK_WORD_RUN` 门槛从 `{2,}` 提到 `{3,}` ——工程记号里 `σθ`、`αβ`（α-β 跟踪滤波器）、`2πΔf` 都是两个单字母希腊符号紧挨着写，不是希腊语单词；真实希腊语单词几乎总是 ≥3 字母。
- **Vietnamese / Polish / Czech / Hungarian 只查字符不查词**：之前只要出现一个变音符号（如 `ũ`、`Ćuk` 里的 `ć`）就直接判定，现在都补上和其他语言一致的"≥2 个功能词"门槛。真实事故：信号处理公式里 `ũ`（估计量/共轭符号常用波浪号）单独出现就被判成越南语；电力电子页提到人名 "Ćuk converter"（Ćuk 变换器，以 Slobodan Ćuk 命名）就被判成波兰语。
- **Spanish/Portuguese 词表本身太弱**：西班牙语词表里的 `el`/`los` 恰好是雷达领域缩写 EL（仰角）/LOS（视距）；葡萄牙语词表里的 `a`/`as`/`o`/`os` 是极常见的单字母英文词/数学变量名。把这几个高碰撞词从词表里删掉，换成更没有歧义的功能词。
- **CJK 分支"有假名就判日语"**：不管假名占比，只要中文文档里混了哪怕 1 个假名借词（如引用日文资料里的 "パス"）就整页判成日语。改成假名字符数占中日字符总数 ≥15% 才判日语，否则按主导文字（中文）算。

新增回归测试：`test_language.py` 的 `TestMathAndAcronymFalsePositivesStayEnglish`（7 个假阳性复现 + 6 个真实语种仍能正确识别的对照用例）。扫描脚本本身是一次性的，未入库，逻辑很简单：对每个 `wiki/**/*.md` 去掉 frontmatter 后取正文前 3000 字符跑 `detect_language`，筛出结果不在 (Chinese, English) 里的页面。

## 配置

```bash
# 'auto'（默认/不设）按源检测；设为如 Chinese / English 强制整个 KB
export IMPROVED_WIKI_OUTPUT_LANGUAGE=auto
```

见 `initial-setup.md` 的 bootstrap 清单。

## 检索侧影响

bge-m3 多语言 embedding 可跨语言检索，但关键词搜索是字面匹配，去重的 cosine 相似度阈值（0.82）在跨语言场景通常达不到。由于这个 KB 现存概念页约 76% 是英文，英文源默认生成英文页是最一致的选择（避免对已有英文页产生无法合并的近重复，从而碎片化 KB）。若要把整个 KB 锁定为单一语言，设置 `IMPROVED_WIKI_OUTPUT_LANGUAGE`。
