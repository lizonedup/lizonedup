# 逐图绘制规格书（LiZoneDedup 实验图）

供绘图智能体零上下文重绘论文全部实验图（正文图 4–9 + 附录图 10–12）。
图 1–3 为设计示意图（TikZ/手绘），不在本规格范围内。
每个面板在 `reference/current_panels/` 有当前版本 PDF 可对照。

---

## 0. 全局硬性规则（每张图都必须遵守）

**色板（v12，Tol 系唯一一套，已通过 dataviz 六项可计算校验：
相邻 CVD ΔE≥8、常规视觉 ΔE≥15、ramp 亮端对比度≥2:1；当前实现
`reference/figs_v12.py`）：**

| 语义 | 颜色 | HEX |
|---|---|---|
| NoDedup（参考基线，中性灰+黑描边为辅助编码） | 灰 | `#969696` |
| HostDedup | 青 | `#33BBEE` |
| LiZoneDedup（主角） | 蓝 | `#005F9E`（较 Tol 蓝加深，拉开蓝↔水鸭绿） |
| Balloon-ZNS | 水鸭绿 | `#00897A` |
| LiZone+Balloon | 橙 | `#EE7733` |
| PageFTLDedup / 描边 / 文本 / 仿真器菱形 | 黑 | `#000000` |
| 上限线 / 预算线 / 失败模式描边（仅此用途） | 红 | `#CC3311` |

- 软件变体：同色相浅色调 + 45° 斜纹（hatch `///`，黑描边辅助）：
  LiZone-sw `#9DC9E8`、Balloon-sw `#A8DCD0`、Combo-sw `#F5B48C`。
- 消融五层（图 8a/b）**复用系统五色**（全论文配色统一）：Global=灰
  （不可行参考）、DataPath=青、+Shard=绿、+Split=橙、**+Full cache=
  LiZone 蓝**（最终层=完整系统=主角蓝）。
- 图 9 的两条负载线都用 LiZone 蓝：**实线+实心标记 = container，虚线+空心
  标记 = rocksdb**（靠线型/标记区分，不引入新颜色）。

**版式规则：**
1. 所有面板**横向**，宽:高 ≥ 1.25:1（同行面板等高；同行总宽 ≤ 6.9in）。
2. 面板按**最终印刷物理尺寸**生成（下文每面板标注英寸），LaTeX 1:1 嵌入。
3. **禁止双 y 轴**；两物理量拆成两个面板。
4. 柱状图：黑描边（0.5pt）、y 轴从 0 起；截断轴必须在 y 轴底部画轴断斜线
   （双斜杠标记）并在子图注说明。
5. 去顶部/右侧轴线（despined）、无网格线、图例无边框；图例只放在
   已验证空白区或面板外（轴上方/下方），禁止压柱压线。
6. 数值标签：宽柱（≥12pt）柱顶横向白字；窄分组柱（~6pt，图 4/5）用
   柱内 90° 竖排白字（横向文字比柱宽会越界）；均不得与任何元素相交
   （交付前用 PDF 文字框求交检测，重叠对数必须为 0——唯一豁免：白色
   竖排标签下层穿过的红色上限点线，文字在上层且可读）。
7. 字体：Arial/DejaVu Sans 系；中等面板刻度 ~8pt、轴标 ~9pt、图例 6.5-8pt
   （下限 6.2pt 仅图 8 条状图例）。
8. 出图 300 dpi、PDF 矢量、`pdf.fonttype=42`。
9. 系统柱/线颜色按上表。负载系列仅两处，均复用系统色：图 11 用
   container=蓝 `#005F9E`、backup=橙 `#EE7733`（实线=迁移，虚线=无，
   4 项图例 2×2 置于 x 轴下方）；图 12 四负载线 fsl=青、container=蓝、
   vm=绿、backup=橙 + 区分标记（o/s/D/^）+ 右端彩色直接标注。
10. **柱宽按布局分级、同级严格一致**（figs_v12.py 的 `barw()` 按布局后
   实测轴宽计算，目标 0.0875in=6.3pt）：双列布局（图 4a/4b、7b）
   6.1–6.2pt；三列布局（图 5a/b/c、图 8）3.8–4.5pt（窄面板组距上限）；
   6 柱（7a）≈6.0pt、单柱（图 6）0.155in、2 柱（图 10）0.130in。
   同一 figure 内等柱数子图必须等宽（LaTeX 槽位 + PDF 尺寸都相等）。

---

## 1. 图 4 — 端到端吞吐（`fig:e2e-tput-w`，时序口径，**仅写方向**）

数据：`data/e4_perf.csv`。**单面板单栏**（3.12 × 2.45 in），8 臂（用户指定顺序），
无数值标签：

- nodedup 灰 → lizone-hw 蓝 → balloon-hw 绿 → hostdedup 青 →
  balloon-sw 浅绿纹 → combo-hw 橙 → lizone-sw 浅蓝纹 →
  PageFTLDedup 黑（oracle），+ 655 红点线横贯（图例第 9 项）。
- y 0–5000；x 缩写刻度；图例 9 项 3 列 3 行图内左上。
- 读方向不测（读吞吐数值进正文 §4.2 Read throughput 段）。

## 2. 图 5 — 端到端延迟（`fig:e2e-lat`，时序口径）

**2×2 四面板**（各 3.15 × 2.10 in = 0.485\textwidth），**7 臂**（5 硬件
系统 + LiZone-sw / Balloon-sw 浅色斜纹），x 刻度全名，**无数值标签**：

- (a) wp50 对数（3–800，刻度 10/30/100/300），7 项图例 3 列 3 行图内左上；
- (b) wp99 截断 185–255 + 轴断双斜线；
- (c) rp50（0–1.25×max，硬件 40.1µs 持平、sw +2–6µs）；
- (d) rp99（0–1.12×max）。

## 3. 图 6 — 自适应旁路（`fig:adaptive-bypass`，时序口径）

数据：`data/e4b_bypass.csv`。四组配置（**硬件方案**）：NoDedup、always-on
（v9-default）、bypass 5%/10%、bypass 15%。**两个柱状面板纵排（各占整栏）**，
各 3.12 × 1.95 in，无柱上数值：

- **(a) adaptive-bypass-w**：Write bandwidth (MB/s)，NoDedup 灰柱、
  LiZone 配置蓝柱。
- **(b) adaptive-bypass-r**：Read p99 (µs)，同样式；失败柱（80.2µs）
  红描边 + 红注记 "2× read tail"。

## 4. 图 7 — 去重 vs 压缩（`fig:dedup-reduction`，时序口径）

**单面板单栏**（3.12 × 2.45 in），**仅硬件 3 臂**（LiZone 蓝 / Balloon 绿 /
Combo 橙；sw 变体的物理削减与 hw 完全相同，已验证，故不画），
NoDedup ≡ 0% 不画；x 缩写刻度；y=Physical write reduction (%)，0–100；
图例 3 项置于面板外顶部单行。原 (b) 有效容量面板已删——capacity =
1/(1−reduction)，数学冗余（等价换算写进正文）。

## 5. 图 8 — 元数据消融（`fig:metadata-ablation`，功能口径）

数据：`data/e2_ablation.csv` + e4_perf。三个面板一行，各 2.10 × 1.70 in。

**(a) md-ra / (b) md-wamd**：x=6 负载（FSL, Cont., VM, Bkp, RDB, Cloud——
用缩写防碰撞），每组 5 柱 = 5 消融层，颜色 = 系统五色
（层序 globalindex/datapath/shard/split/v9 → 灰/青/绿/橙/蓝，
+Full cache = LiZone 蓝）。
- (a) y=Metadata reads (B/req, log)，取列 `ra`；
- (b) y=Metadata write ampl. (×, log)，取列 `wamd`；
- 层图例放 **(a) 面板外顶部**（3+2 两行：Global / DataPath / +Shard /
  +Split / +Cache，色块=系统五色），(a)(b)(c) 三面板因此等高。

**(c) md-wa：与 (a)(b) 完全同构**（x=6 负载 × 5 消融层，系统五色）—
y=Total write ampl. (×) 取 `e2_ablation.wa`；y=1 深灰虚线 + 左上
"NoDedup = 1.0×"；结论：globalindex/datapath 在 dedup-light 负载上
总 WA **超过 1.0**（1.6–1.7×，元数据重写超过去重收益），完整设计
0.15–0.85× 恒低于 1.0；共享 (a) 图例，(c) 不再单独设图例。

## 6. 图 9 — 敏感性与扩展性（`fig:sensitivity`，2×2 栅格，各 3.15 × 2.45 in）

**(a) sens-dram：Index DRAM (MB, log-log)** — x=容量 [1024, 2048, 4096]
（log2，刻度 1T/2T/4T；仅部署容量）。
数据 `data/e7_capacity.csv`：
- LiZone-hw container / rocksdb：`lz_dram_mb`（两线都是蓝；container 实线
  实心圆，rocksdb 虚线空心菱；两线恒为 45.9 / 37.1 MB）；
- PageFTLDedup（黑虚线空心三角，即该类系统所需全局索引的解析 DRAM
  需求 40B/页）：`globalindex_dram_gb_analytic × 1024`；
- HostDedup host（青点线倒三角）：`hostdedup_dram_gb_analytic × 1024`；
- y=128 红点划线 + 红字 "128 MB budget"；全部系列用**直接文字标注**
  （不放图例框），标注位置见 figs_v12.py。

**(b) sens-lat：Write p99 (µs) vs 容量 1–4TB** — x=[1024, 2048, 4096]，
刻度 1T/2T/4T。数据 `data/e7b_captiming.csv`（2026-09-14 已补 2T/4T：
container lizone wp99=207.9、rocksdb 204.3、nodedup 201.0，平坦结论成立）；
1024 点取 e4_perf 基线（container 207.4 / rocksdb 204.3）。
NoDedup 灰实线圆一条（两负载同值，右端标 "NoDedup (both)"）；
LiZone 蓝：container 实线菱、rocksdb 虚线空心，右端直接标注。

**(c) sens-nandlat：Write bandwidth (MB/s) vs t_prog (µs)** — x=[60, 100, 200]。
`data/e5_nandlat.csv`（container）+ `data/e5b_nandlat.csv`（rocksdb）。
三系统各一条：NoDedup 灰圆、HostDedup 青方、LiZone 蓝菱；
负载靠线型：实线=container、虚线=rocksdb（图例加两个线型条目说明）。

**(d) sens-zone：Write bandwidth (MB/s) vs zone 大小 (MB)** — x=[128, 256, 512]
（log2 刻度标 128/256/512）。`data/e8b_zonetiming.csv`，128MB 点取 e4_perf。
系列同 (b)，但 NoDedup 两条负载线**数值不同**（container 629.9 vs
rocksdb 559.0 MB/s），必须画两条灰线（实线圆=container、虚线空心=rocksdb），
禁止再标 "NoDedup (both)"；图例 4 项 2 列置于 x 轴下方（下方空带 0–500 MB/s
放不下图例，压线风险高）。

## 7. 图 10 — FEMU 交叉验证（`fig:femu-crossval`，附录，3.12 × 2.20 in）

**归一化带宽验证**（用户指定）：x=Container/Fsl/Rocksdb，两系统柱
（LiZone 蓝 / Balloon 绿），y=wbw/nodedup(wbw)（0–1.30，1.0 灰虚线
"NoDedup = 1.0×"）。全部值 0.97–1.03×（TCG 把 guest 顶死 ~100MB/s）——
图诚实呈现"该平台带宽无法区分设计"，设计信号由物理写削减承载
（数值在图注：container 99.4 vs 70%、rocksdb 49.7 vs 70%）。
数据 `data/e95_femu.csv` 的 `wbw_mbps`。

## 8. 图 11 — 迁移扫描（`fig:lifecycle-sweep`，附录，两面板纵向堆叠各 3.12 × 1.95 in 占整栏）

数据 `data/e47_lifecycle.csv`（workload ∈ {container, backup}，
migrate ∈ {0, 1}，按 `dead` 升序连线）。
- (a) lifecycle-trapped：y=Trapped (GB)，列 `trapped_dead_gb`；
- (b) lifecycle-gc：y=GC blocks，列 `gc_blocked`；
- x 两面板均为 Dead ratio (%)，刻度只留 30/50/70。
- container=蓝 `#005F9E`、backup=橙 `#EE7733`；开启迁移 = 实线+空心圆，
  关闭 = 虚线。图例 4 项（两负载 × 迁移开/关）2×2 置于 x 轴下方。

## 9. 图 12 — HostDedup 预算（`fig:hostdedup-budget`，附录，3.12 × 2.10 in）

数据 `data/e6_hostdram.csv`。x=预算 [512, 1024, 2048, 4096] MB（log2，
刻度 512MB/1GB/2GB/4GB）；每负载一线复用系统色（fsl=青 o、container=蓝 s、
vm=绿 D、backup=橙 ^）+ 右端彩色直接标注负载名：y=Write bandwidth (MB/s)，
列 `wbw`，y 从 0 起。

---

## CSV 列字典（非自明列）

| 文件.列 | 含义 |
|---|---|
| e4_perf.wbw / wphys | 逻辑写 / 物理写带宽 MB/s（时序，QD32 闭环） |
| e4_perf.rbw / rphys | 逻辑 / 物理读带宽 |
| e4_perf.wp50 / wp99 / rp99 | 写 p50、写 p99、读 p99（µs） |
| e4_perf.hit_rate | 时序口径命中（去重）率 |
| e4_perf.parts_w / parts_r | 写/读各阶段耗时分解（JSON 字符串） |
| e1*.phys_red | 物理写削减比例（功能口径，0–1） |
| e1*.recall | 相对 oracle 的召回 |
| e2_ablation.ra / wamd / wa | 元数据读放大 B/req、元数据写放大 ×、总写放大 × |
| e2_ablation.dram | 功能回放稳态 DRAM（MB，v9 层值偏旧，容量结论用 e7） |
| e5*.t_prog_us | 该行对应的 NAND 编程延迟档 |
| e7*.globalindex_dram_gb_analytic / hostdedup_... | 解析式全局索引/主机索引需求（GB，满容量假设） |
| e95_femu.phys_red | FEMU 实测物理写削减（0–1） |
| e47_lifecycle.dead / migrate | 人为死页比例 / 是否开启定向迁移 |

## 数值抽查锚点（画完自检）

- 图4a：FSL 组 NoDedup 631、LiZone 蓝 716、Combo 橙 1151；Backup 组
  LiZone 2748、oracle 菱形 2771。
- 图7b：Combo 柱标签 2.0 / 6.3 / 5.0 / 11.8 / 1.8。
- 图8c：全部系统柱 ≤1.0，NoDedup 恒 =1.0。
- 图9a：两条蓝线分别恒 45.9 与 37.1 MB；Global index 100GB→1.0GB 线性上升。
- 图10：container 组 LiZone FEMU 柱 99.4、黑菱（仿真器）72.8。
