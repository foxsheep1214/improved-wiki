# FPV飞控选型参考

> 场景：快速拦截、高机动FPV无人机（10寸）
> 负责人：胡杨 / 智能硬件部
> 建立时间：2026-05-16

---

## 快速拦截机飞控选型要点

### 核心需求（高机动拦截）
- gyro采样率 ≥ 32kHz
- 处理器 F7 或 H7（响应延迟低）
- 陀螺环带宽 ≥ 4kHz
- 重量 50-70g

### 推荐型号

| 型号 | 处理器 | 特点 |
|------|--------|------|
| Holybro Kakute H7 | STM32H7 480MHz | 极致机动首选 |
| SpeedyBee F7 4in1 45A | STM32F7 | 板载4in1 ESC，一体化低延迟 |
| iFlight Blitz F7 Pro | STM32F7 | 陀螺响应快，OSD调参方便 |
| Foxeer Hummingbird H7 | STM32H7 | 轻量化，专为高机动 |
| Matek H743 | STM32H743 | 成熟稳定 |

### 10寸机建议
- 优先选飞控+4in1 ESC一体化套装，缩短信号线，降低延迟
- UART预留 ≥5个（拦截弹可能接电台/数据链）
- BEC 12V输出给图传供电

### 不推荐
- STM32F4系列：性能尚可但扩展性弱
- 纯穿越机低端飞控：接口少，不适合需要数传/数据链的重载拦截场景

---

## 闲鱼反爬技术障碍（避坑）

### 问题
在Apple Silicon Mac上，`undetected-chromedriver`安装后运行报错：
```
OSError: [Errno 86] Bad CPU type in executable
```
原因：undetected-chromedriver预编译二进制为x86架构，Mac为Apple Silicon(ARM)不兼容。

### 结论
- 普通`selenium` + 系统Chrome可用（能加载页面）
- 闲鱼商品内容通过JS动态加载，未登录状态下为空壳
- 阿里系反爬强，需登录态或手机App API才能获取商品数据
- 如需抓取闲鱼：建议用户本地浏览器登录后导出cookie，或改用淘宝/孔夫子/多抓鱼搜索

---

## 飞控术语：PSD

在飞控中PSD = **Power Spectral Density（功率谱密度）**，用于：
- IMU传感器噪声表征（单位：°/s/√Hz 或 mg/√Hz）
- 大气紊流/阵风载荷建模（Dryden模型）
- 机载振动频谱分析

---

## "一次做对：高速PCB和系统设计的实用手册"

- **原版英文名**：《Right the First Time: A Practical Handbook on High Speed PCB and System Design》
- **作者**：Lee W. Ritchey
- **出版社**：Speeding Edge（2003年）
- **ISBN**：0974193607
- **官网**（有Introduction免费PDF）：https://speedingedge.com/wp-content/uploads/Right-the-First-Time-Intro-06-05-03.pdf
- **全本购买**：亚马逊约30-60美元，或在淘宝/闲鱼搜"Right the First Time Ritchey"
- **定位**：高速PCB设计经典，与Howard Johnson黑魔法书同级别，适合作为设计手册查阅
