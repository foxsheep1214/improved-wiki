# Raw 文件命名规范检查

## 原则

**每个知识库项目的 raw 文件命名规则是项目特定的，应当记录在项目本身内，不属于 skill 的通用规则。**

- 项目规则文件：`<project>/raw/NAMING.md`
- 项目检查脚本：`~/.agents/skills/improved-wiki/scripts/normalize_raw_names.py`

## Skill 的行为要求

### Stage 1.1 之前（每次 ingest）

1. **检查 `raw/NAMING.md` 是否存在**
   - 存在 → 继续
   - 不存在 → **提醒用户先创建规则**，列出当前 raw/ 下的文件夹和文件样本，询问用户希望采用什么命名规则。帮用户起草 `raw/NAMING.md` 和 `normalize_raw_names.py`。

2. **检查新文件是否符合规则**
   - 如果有 `normalize_raw_names.py` → 运行 `--check`，报告不符合的文件
   - 如果只有 `raw/NAMING.md` 没有脚本 → 手动对照规则检查
   - 如果不符合 → **阻止 ingest，提醒用户先修正命名**

3. **不要跳过检查**
   - 命名检查必须在文件选择之前执行（因为不规范的文件名会在后续 Stage 产生歧义）。注意：`ingest-cache.json` **不是**去重依据（Never rely on ingest-cache.json for dedup）——去重只看 `wiki/sources/<path>.md` 是否已存在

### 用户把新文件放入 raw/ 时

**标准工作流**：用户先把文件放入 `raw/` 对应子目录，然后提示 Claude 按规则改名。Claude 应该：

1. **找最新文件**：用 `find raw/ -type f -mmin -<N>` 或 `normalize_raw_names.py --recent <N>` 定位最近放入的文件
2. **对照 `raw/NAMING.md` 检查命名**
3. **不符合 → 建议改名**（给出改名前后的对比），用户确认后执行
4. **规则不存在 → 提醒制定规则**

不要全量扫描整个 `raw/`，只关注用户刚放入的文件。

### 示例对话流

```
用户: 我加了几个文件到 raw/Datasheet/05_放大器/，帮我改名
Skill: 检查最近 30 分钟内新增的文件... 找到 3 个：
        ❌ LM358.pdf → TI - LM358.pdf
        ❌ opa2134.pdf → TI - OPA2134.pdf
        ✅ TI - TLV9301.pdf（已符合规则）
      要执行改名吗？
```

```
用户: 我传了本书到 raw/Book/
Skill: 检查 raw/NAMING.md... 不存在。
      raw/ 目前有这些文件夹：
        book/ (98 files), datasheet/ (236 files), paper/ (0 files)
      book 文件夹的现有文件格式：「书名 - 年份 - 作者.pdf」
      要为 HardwareWiki 创建 raw/NAMING.md 吗？可以沿用现有格式。
```

## NAMING.md 格式

参考 HardwareWiki 的 `raw/NAMING.md` 作为模板。每个项目至少定义：

- 通用规则（分隔符、禁止字符）
- 每种 raw 子目录的命名格式（含正例/反例）
- Vendor 简称表（如果项目有 Datasheet/Applicationnote 等）

## normalize_raw_names.py 约定

脚本位于 skill 内：`~/.agents/skills/improved-wiki/scripts/normalize_raw_names.py`

```
# 自动检测项目（从 CWD 向上找 raw/NAMING.md）
python3 ~/.agents/skills/improved-wiki/scripts/normalize_raw_names.py --check

# 指定项目
python3 ~/.agents/skills/improved-wiki/scripts/normalize_raw_names.py --project <path> --check

# 只检查最近文件
python3 ~/.agents/skills/improved-wiki/scripts/normalize_raw_names.py --recent 30 --fix

# 查看每项处理的详情
python3 ~/.agents/skills/improved-wiki/scripts/normalize_raw_names.py --check --verbose
```

`--check` 检查所有文件是否符合规范；`--fix` 自动修正可识别的命名问题（如补 Vendor 前缀）；`--recent N` 只检查最近 N 分钟内修改的文件；`--verbose` 输出每项处理的详情。

### 检查结果分级：error vs warn

检查项分两级：

- **error（❌）**：硬性违规，如缺分隔段、年份格式不对、Vendor 未识别。`--fix` 仅作用于 error；error 会使脚本退出码为 1。
- **warn（⚠️）**：启发式怀疑，不阻断。warn 不参与 `--fix`，不影响退出码。

### 作者段启发式（warn）

当某类型的 YAML 规则带 `author_field` + `surname_only: true` 时（如 Book/Paper/Presentation，Paper/Presentation 经 `extends: Book` 继承），脚本对作者段做保守启发式检查，告警以下高置信违规：

- 含 `et al` / `等`（多作者标记，规则要求只写第一作者姓氏）
- 含名字缩写（`Y-M`、`M.`、`J` 等独立大写字母串）
- ≥3 词（疑似全名或多作者列表）

**已知盲区（不告警）**：

- **2 词作者段**：无法区分多词姓氏 `Ben Salah` 与全名 `Hong Zhangjie`，均不告警，需人工把关。
- **纯 CJK 作者段**：Book 规则允许中文全名，且 CJK 姓氏切分需字典，跳过。

NAMING.md 中启用方式（YAML rules 块）：

```yaml
  Book:
    pattern: "书名 - 年份 - 作者"
    min_parts: 3
    year_field: 1
    author_field: -1      # 末段为作者（-1 = 最后一段）
    surname_only: true    # 启用作者段启发式
  Paper:
    extends: Book         # 自动继承 author_field + surname_only
```

规则来源：各项目 `raw/NAMING.md` 中的 ` ```yaml rules ``` ` 块。
脚本不包含项目特定数据（Vendor 前缀等），全部从 NAMING.md 解析。
