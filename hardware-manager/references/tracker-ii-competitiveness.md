# Tracker II 竞争分析参考数据

> 最后更新：2026-05-11

---

## Robin Radar IRIS 3D 补充信息（2026-05-11新增）

**产品定位**：轻量级 3D 反无人机雷达，X-band FMCW，360°+60° 覆盖，29 kg，<15 min 展开

**核心技术**：
- 机械旋转扫描（30 rpm = 1 Hz 全刷新）+ 微多普勒 + DNN 分类
- 非真 3D AESA（仰角 60° 固定），与 Echodyne MESA 电扫不同
- LRM（Long-Range Mode）：纯软件升级，5 km ↔ 12 km 可切换

**关键差异化**：
- 乌克兰战场实战检验 → LRM 12 km 升级由操作员反馈驱动
- 软件定义升级能力（5→12 km 不换硬件）
- OTM 车载模式（100 km/h）

**对 Tracker II 启示**：
- X-band 选择一致
- 软件升级能力值得借鉴（硬件留余量，功能软件化）
- OTM 车载能力参考
- DNN 分类随样本积累性能提升，Tracker II 也需强化 AI 分类

**报告路径**：`~/Documents/Tracker II/01_论证/竞品分析/Robin_Radar_IRIS_3D技术分析报告.md`
**产品图**：`~/Documents/Tracker II/01_论证/参考图片/Robin_IRIS_3D_product.png`
> 来源：产品驾驶舱 Excel + 需求分析框架.md

---

## Tracker II 项目基本信息

- **产品经理**：何小静
- **项目经理**：贺仪
- **项目定位**：X波段中型地基雷达，针对Shahed无人机15km探测，总功耗≤1kW
- **核心场景**：城市要点防护（复杂杂波/多径）、边境/工业要点防护（相对干净）
- **典型目标**：Shahed-136/238（RCS≈0.1㎡）、御3（RCS≈0.01㎡）、FPV/穿越机（RCS≈0.01㎡）
- **论证文件夹**：`~/Documents/Tracker II/01_论证/`
  - `需求分析/Tracker II 需求分析框架.md` — 需求分析主框架（V1.2，含产品竞争力分析）
  - `X波段距离方程_*.xlsx` — 链路预算工具

---

## 硬件链路关键参数（实测/已知）

| 参数 | 数值 | 说明 |
|------|------|------|
| **单元辐射功率** | **0.5 W** | 硅基多功能芯片（美辰微 GR 系列或安其微 97420），器件参数，与系统功耗解耦 |
| 系统总功耗 | ≤1,000 W | Tracker II 约束上限 |
| 峰值辐射功率 | 取决于阵面规模 | 0.5W × 单元数 |
| nMHR 单元辐射功率 | ~10 W（GaN）| 器件级参数，与系统功耗 700W 解耦 |
| nMHR 阵面 | 260 mm × 520 mm | 16×32 = 512 单元 |
| nMHR 峰值辐射功率 | ~5 kW | 512 × 10 W |
| Tracker II vs nMHR 单元功率比 | 0.5W vs 10W | Tracker II 单元功率仅为 nMHR 的 1/20，需要更多单元数补偿 |

**重要概念区分**：
- **单元辐射功率**：GaN/硅基 PA 器件的输出功率（器件物理参数）
- **峰值辐射功率**：阵面所有单元同时发射时的总射频输出 = 单元数 × 单元辐射功率
- **系统总功耗**：整个雷达系统供电，与辐射功率通过占空比和 PAE 效率关联
- 三者是独立参数，通过雷达方程和功率链路设计协调

---

## 竞争产品关键参数

| 指标 | Tracker II | Echodyne EchoShield |
|------|-----------|-------------------|
| 波段 | X | Ku（15.4-16.6 GHz）|
| 探测距离（Shahed 类）| 精灵4: 1.2km（实测）| 精灵4: 1km（实测）|
| 方位 FOV | **±50°**（当前版本）/ ±50°（国产替代版本）| **官方未公开**（130°×90°无官方出处，不应引用）|
| 俯仰 FOV | **±40°**（当前版本）/ ±45°（国产替代版本）| **官方未公开** |
| 测角精度 | 方位≤0.5° / 俯仰≤0.5°（目标，2026-05-02修正）| 方位**<0.5°** / 俯仰**<0.5°**（EchoShield官方）|
| 多目标同时监视 | ≥8目标，5目标@20Hz（火控模式）| 最大20目标@10Hz（无法同时）|
| 功耗 | **≤1kW**（2026-05-02修正，原误写100W）| 50W |
| 机载能力 | 不具备（地基/车载定位）| EchoFlight: 650m@RCS=0.01㎡ |
| 威胁度评估 | **行业领先（多数竞品无）**| 无 |
| 环境自适应完成率 | 20% | — |
| 识别完成率 | 50% | — |

**主要差距**：俯仰 FOV、SWaP（功率级别不同不可直接比较）、机载能力（空白 vs 有）、威胁度自主评估（Tracker II 独有）

---

## 产品竞争力分析框架（两大类）

**大类一：对 Shahed 类巡飞弹 远距准确探测与识别**
- 解决"发现不了、识别不准"痛点
- 覆盖：Shahed远距探测、测角精度→光电引导、覆盖范围、快速发现、杂波抑制、机动目标跟踪、微小目标识别（FPV）、鸟类鉴别

**大类二：多目标同时监视与火控引导**
- 解决"看得见但打不了"痛点（Shahed集群入侵→光电/火控闭环）
- 覆盖：多目标同时监视、火控接口（延迟/精度/通道数）、威胁度自主评估、SWaP_C（≤1kW）、环境自适应、快速机动部署

---

## 核心需求对照表

| # | 要求 | 关键技术 |
|---|------|---------|
| 1 | **探测威力** | 雷达方程约束（功率/增益/噪声/积累时间权衡），Shahed 15km @ RCS=0.1㎡ |
| 2 | **角精度** | 单脉冲测角（和差波束）或DBF数字波束形成，≤0.5度方位/俯仰 |
| 3 | **多目标同时监视** | 8架Shahed依次起飞场景；JPDA/MHT/PDA多目标跟踪；目标优先级/重捕获 |
| 4 | **机动目标跟踪稳定性** | IMM多模型（机动转弯/匀速/匀加速）；航迹分裂概率≤1%；重捕获≤1秒 |
| 5 | **微多普勒识别** | 旋翼微多普勒特征提取（时频分析）；鸟类vs无人机鉴别；分类器 |
| 6 | **地基杂波抑制** | ⚠️ **非机载，无STAP**；MTI/MTD + CFAR自适应门限 + 杂波图 + 极化滤波 + 城市多径抑制 |
| 7 | **威胁度自主评估** | 多维度加权（RCS/速度/距离/航迹意图）；高/中/低三级；驱动光电转台优先级 |
| 8 | **光电引导与数据融合** | 雷达粗瞄→光电惯导精跟→融合修正；坐标转换/时间同步/目标交接协议 |
| 9 | **火控接口** | 位置精度≤20m，速度精度≤1m/s，延迟≤100ms，≥4个火控通道 |
| 10 | **覆盖范围** | 单台：方位±50°/俯仰±40°（国产替代版±50°/±45°），4台准半球防御 |

**用户关注三要素**：微小目标识别概率、云雨杂波下性能下降、火控能力

---

## Excel驾驶舱修改注意事项

当Sheet包含大量合并单元格时，直接调用`ws.unmerge_cells()`或逐行移动数据会触发两类错误：

1. **`AttributeError: 'MergedCell' object attribute 'value' is read-only`**
   — `ws.unmerge_cells()`后，原来合并格的从属格暴露为`MergedCell`对象，无法直接赋值

2. **`TypeError: expected <class 'int'>` inside `ws.unmerge_cells()`**
   — Sheet中存在空的/格式损坏的合并格引用时触发

**正确的修改流程**：

```python
from openpyxl import load_workbook, Workbook

# Step 1: 读取原始数据（含合并格处理）
merged_ranges = list(ws.merged_cells.ranges)
def get_val(ws, row, col):
    for mr in merged_ranges:
        if mr.min_row <= row <= mr.max_row and mr.min_col <= col <= mr.max_col:
            return ws.cell(row=row, column=col).value if (row == mr.min_row and col == mr.min_col) else None
    return ws.cell(row=row, column=col).value

raw = []
for r in range(1, max_r + 1):
    raw.append([get_val(ws, r, c) for c in range(1, max_c + 1)])

# Step 2: 内存中重组行数据
new_rows = [...]

# Step 3: 创建新Workbook写入
wb_new = Workbook()
wb_new.remove(wb_new.active)
for name in wb_orig.sheetnames:
    ws_new = wb_new.create_sheet(title=name)
    if name == target_sheet:
        for r_idx, row in enumerate(new_rows, start=1):
            for c_idx, val in enumerate(row, start=1):
                if val is not None:
                    ws_new.cell(row=r_idx, column=c_idx).value = val
    else:
        ws_src = wb_orig[name]
        for r in range(1, ws_src.max_row + 1):
            for c in range(1, ws_src.max_column + 1):
                v = get_val(ws_src, r, c)
                if v is not None:
                    ws_new.cell(row=r, column=c).value = v
wb_new.save(OUT)
```

**关键教训**：
- `ws.unmerge_cells()` 在有62个合并格的Sheet上不可靠，应完全避免
- 路径含空格时必须加引号
- 读取后用显式索引`row[i]`，列数固定为`max_c`
- 时间值用字符串存储，避免datetime解析出错

---

## 产品竞争力分析文档创建方法

> 文档路径：`~/Documents/Tracker II/01_论证/需求及方案/产品竞争力分析.md`
> 参考模板：`TrackerII_产品驾驶舱_20260429.xlsx` → tab"产品竞争力驾驶舱-二代雷达"
> 关联文件：`references/tracker-ii-competitiveness.md`（本文件）

**产品竞争力数据文件**已移至本 references 文件夹。

**文件结构**：
```
产品竞争力分析.md
├── 一、客户痛点与产品竞争力映射（12条详细展开）
├── 二、竞争力要素大类汇总
│   ├── 大类一：远距探测与识别（4小类 × 3细分）
│   └── 大类二：多目标监视与火控引导（4小类 × 3细分）
├── 三、产品竞争力要素汇总（优先级★★★/★★☆/★☆☆排序）
├── 四、竞争格局定位
└── 五、待解决风险
```
