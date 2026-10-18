# 测量与统计

## 执行过程

固定编译输入，在 pass 入口停止 LLVM 线程池并执行 `malloc_trim(0)`，保存单线程进程镜像。测量时恢复父进程，从相同入口状态 fork 各版本实现；父进程保留输入。

| 模式 | 一份镜像 | 测量方式 |
| --- | --- | --- |
| NoLTO | 一个批次管理进程及最多 `PREPARE_JOBS` 个编译父进程 | 每批恢复一次，逐编译单元 fork |
| FullLTO | 一个链接目标的编译父进程，单后端分区 | 每目标恢复一次，逐版本 fork |

准备可并行，测量串行并固定核心。每输入逐轮交替提交正序和逆序。首次保存后使用 `zstd -1` 压缩内存页；复用时解压并恢复，结束后删除解压页，保留压缩镜像及依赖文件。

## Outliner 阶段

| 阶段 | 函数范围 | 内容与 CPU 来源 |
| --- | --- | --- |
| `outliner` | `MachineOutliner::runOnModule()` 入口至返回 | 完整 pass；`getrusage` user + sys 差值 |
| `mapping` | `MachineOutliner::populateMapper()` 入口至返回 | 过滤、分类、整数映射、位置记录；BPF on-CPU |
| `tree` | `llvm::SuffixTree::SuffixTree()` 入口至返回 | 构建、元数据、叶节点归集；BPF on-CPU |
| `selection` | 后缀树构造返回至 `MachineOutliner::outline()` 返回 | 枚举、筛选、排序、贪心选择、指令替换与树销毁；BPF on-CPU |

同阶段嵌套调用只计最外层。BPF CPU 包含探针与中断开销；最终 mapper 和候选清理计入完整 pass。

## 指标

| 指标 | 汇总口径 |
| --- | --- |
| `cpu_seconds` | 每轮各输入求和，再取轮次均值 |
| `peak_rss_mib` | 阶段内进程绝对 RSS 峰值；每轮各输入取最大，再取轮次均值 |
| `entry_rss_mib` | 每次阶段调用入口 RSS；每轮各输入取最大，再取轮次均值 |
| `peak_rss_increase_mib` | 每次调用期间峰值减去该次入口 RSS；每轮各输入取最大，再取轮次均值 |
| 相对变化 / % | `(本版 / 参照版 − 1) × 100`；比较基线和前一提交，零参照留空 |
| 运行开销 / s | 镜像准备、保存、恢复、fork、实现加载、镜像收尾的经过时间 |

通用 `summarize` 保留全部逐轮原值，比较表使用全部轮次的算术均值。需要去除极值的实验应从逐轮数据另行复算，并在实验报告中注明口径。

RSS 在调用入口记录，并通过测量核心的 `rss_stat` 跟踪 FILE + ANON + SHMEM。数据包含此前保留的驻留内存，受内核批量记账影响，未覆盖其他核心上的外部回收。各阶段峰值不能相加。

RSS 增量逐次调用计算，不能用汇总后的峰值减去汇总后的入口。它表示相对入口的驻留内存最大增加，不等于分配器的总申请量。

步骤耗时与 pass CPU 分别统计。`measure` 的镜像准备包含前置编译 / 链接、首次保存或解压；保存是其中一项。镜像收尾首次压缩，复用时仅删除解压页。

## 产物

| 位置 | 内容 |
| --- | --- |
| `RESULTS/REPORT.md` | 阶段 CPU、RSS 均值、运行开销与镜像空间 |
| `RESULTS/raw.csv` | 逐轮阶段数据 |
| `RESULTS/comparison.csv` | 版本均值、相对基线与前一提交的变化 |
| `RESULTS/overhead.csv` | 各输入集与模式的运行开销 |
| `RESULTS/timings.json` | 准备、测量、汇总步骤的经过时间 |
| `RESULTS/collector/` | 采集器与探针 |
| `RESULTS/<输入集>/` | 配置、环境、原始 `measurements.jsonl`、`overhead.json` 与日志 |
| `CHECKPOINTS/<输入集>/NoLTO/<程序>/<批次>/` | 批次镜像与 `units/` 控制文件；批次以首个单元下标命名 |
| `CHECKPOINTS/<输入集>/FullLTO/<程序>/00000/` | 链接目标镜像 |

原始数据、构建、镜像和本机配置忽略提交。本地 PR 材料与专用实验脚本可保存在 `results/`，与其原始数据一起保留。
