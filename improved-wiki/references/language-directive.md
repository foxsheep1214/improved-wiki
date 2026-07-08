# Output Language Directive（NashSU `outputLanguage` parity）

NashSU 有明确的 KB 文档语言策略（`outputLanguage` 设置）：默认 `auto` = 检测**每份源文档自身**的语言并用该语言生成（英文书 → 英文页，中文资料 → 中文页，日文论文 → 日文页）；也可以锁定整个 KB 用同一语言。以强指令 `## ⚠️ MANDATORY OUTPUT LANGUAGE` 注入提示词，且**始终原样保留专有名词/技术标识符/URL/文件名/论文标题**（如 BGA、IPC-4101、Tg 即便在非英文模式下也保留）。NashSU 把它注入约 7 个 prompt builder，含 ingest。

## 移植状态

improved-wiki 早就移植了 `scripts/_language.py`（`detect_language` + `build_language_directive` + `IMPROVED_WIKI_OUTPUT_LANGUAGE` 环境变量覆盖），但过去只接入了语义 lint，**没有**接入 ingest 的生成/分析提示词。已于 2026-06-29（commit c359232）修复：`build_language_directive(<源文本样本>)` 现在在 `# Role` 之前注入全部生成/分析 builder（Stage 2.2/2.4/2.6/2.7/2.9，共 5 个——修复时含 2.1 共 6 个，2.1 已于 2026-07-08 移除），默认 auto（按源文本检测）。测试覆盖：`test_ingest_language_directive.py`（英文→英文、中文→中文、env 覆盖三种场景）。

## 配置

```bash
# 'auto'（默认/不设）按源检测；设为如 Chinese / English 强制整个 KB
export IMPROVED_WIKI_OUTPUT_LANGUAGE=auto
```

见 `initial-setup.md` 的 bootstrap 清单。

## 检索侧影响

bge-m3 多语言 embedding 可跨语言检索，但关键词搜索是字面匹配，去重的 cosine 相似度阈值（0.82）在跨语言场景通常达不到。由于这个 KB 现存概念页约 76% 是英文，英文源默认生成英文页是最一致的选择（避免对已有英文页产生无法合并的近重复，从而碎片化 KB）。若要把整个 KB 锁定为单一语言，设置 `IMPROVED_WIKI_OUTPUT_LANGUAGE`。
