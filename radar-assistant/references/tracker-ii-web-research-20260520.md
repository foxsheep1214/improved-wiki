# Tracker II 网络调研工作流（2026-05-20确立）

## web_search — 搜索首选

**触发条件**：web_search 失败（返回 432 错误）时，切换 zai backend 或用浏览器搜索。

```bash
web_search "查询内容" | python3 -c \
```

**输出格式**：JSON，包含 `organic[]` 数组，每项含 `title/link/snippet/date`。

**数据提取**：
```bash
web_search "查询内容" | python3 -c \
  "import sys,json; d=json.load(sys.stdin); [print(r['title'],'|',r['snippet']) for r in d.get('organic',[])]"
```

**关键发现**：
- web_search 返回的结果经过排序，前几条通常是官网/权威来源
- snippet 包含关键参数（探测距离/波段/功耗等），比 browser 更快
- 需注意：snippet 是摘要，可能截断；重要参数需访问官网或产品页验证

## 竞品调研标准工作流

> 已在 SKILL.md 中确立，本次补充 web_search 使用方法。

1. **识别目标公司**：从新闻报道/用户提示获取公司名
2. **web_search 初步摸底**：查公司官网产品页/参数

   web_search "公司名 产品名 探测距离 波段 功耗"
   ```
3. **二次深挖**：针对具体产品型号查详细参数
   ```bash
   web_search "产品型号 具体参数"
   ```
4. **验证官网**：访问官网产品页（browser_navigate 或 web_extract）
5. **整理写入**：竞品分析报告写入 `~/Documents/Tracker II/01_论证/竞品分析/产品名_分析报告.md`

**注意**：中文新闻（腾讯/搜狐）通常只有定性描述，缺乏工程参数。必须以官网英文原文为准。

## markdown 文档章节重编号失败教训

**事件**：将文档从 `3.x` 章节编号改为 `1,2,3...` 时，使用顺序 patch 替换导致编号混乱：
- 两次 `## 3.3`（探测能力 + 竞争力要素汇总）只成功替换了第一个
- 后续章节编号跳号

**根本原因**：顺序字符串替换无法区分同名标题（`## 3.3` 出现两次），且 `patch` 以 offset/limit 读取文件，每次 patch 后行号偏移，导致后续 patch 错位。

**正确做法**：见 `markdown-document-restructuring` skill：
- 涉及 5+ 章节变更时 → **直接 write_file 重写整个文件**（最终采用）
- 涉及 2-4 个章节 → 单次 patch 完成，或用 `write_file` 替换整个章节块

**验证命令**：
```bash
grep -n "^## " 文件.md   # 确认章节编号连续无重复
```
