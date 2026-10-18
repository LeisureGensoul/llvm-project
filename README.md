# LLVM 阶段性能比对

固定输入 → pass 入口镜像 → 恢复一次、fork 各版本 → 汇总。

## 三步运行

修改 [config.sh](config.sh)、[commits.list](commits.list)；每次使用新的 `RESULTS` 目录。

```bash
# 从 LLVM 工作目录进入同级实验目录
cd ../llvm-bench
bash run.sh prepare
bash run.sh measure
bash run.sh summarize
```

- `prepare`：输入编译器；末项提交的 LLVM 测试；命令与依赖；实现库与采集器
- `measure`：首次编译、保存；恢复、fork、执行 pass、记录指标；压缩留存
- `summarize`：逐轮数据；轮次均值；相对基线、前一提交的变化

- 后续比对：改提交清单、结果目录；复用输入与镜像；仍然三步
- 并行：前置准备；串行：被测 pass；固定测量核心
- CRIU 助手：`measure` 自动启停；完成、报错、中断后关闭
- 三步经过时间：自动记录 `timings.json`

## 当前本地基线

- `llvm-bench` 和 `llvm-project` 为同级工作树，分别使用实验分支和 LLVM 优化分支，可独立切换。
- 固定 main：`f14f34f217a99d77628d547b1b3ba9fa47fbd06b`；提交清单为此基线上的四个 PR、14 个优化提交。
- `WORK` 位于 `../llvm-project/llvm-bench`；现有编译器、负载构建、镜像和宿主 CRIU 入口使用其绝对路径。实验目录中的 `tools/`、`src/`、`build/`、`results/` 通过相对链接指向这些缓存。
- `prepare` 生成输入命令缓存；复用镜像时保持 `WORK` 的绝对路径。
- 后续补充优化：更新 `commits.list`、换新的 `RESULTS`，仍执行三步；输入和镜像继续复用。
- [PR 与实验记录](results/README.md)包含各次性能比较；十轮数据按数值去掉最大、最小值后，取剩余八轮均值。

## 配置

- [config.sh](config.sh)：本次实验与目录
- [commits.list](commits.list)：标签、提交；首项基线；后续优化顺序
- [workloads.list](workloads.list)：CTMark 10 项；SPEC CPU2017 C/C++ Rate 16 项；Clang-X86；LLD-X86
- [profiles/outliner/](profiles/outliner/)：[参数](profiles/outliner/config.sh)、[函数范围](profiles/outliner/phases.list)、[测试](profiles/outliner/tests.list)、实现适配

| 设置 | 用途 |
| --- | --- |
| `PROFILE`、`COMMITS` | 测量对象、提交清单；默认 Outliner |
| `INPUT_SETS`、`PROGRAMS` | 输入集；程序 `all`，或单个输入集中的程序名 |
| `MODES`、`ROUNDS` | NoLTO / FullLTO；默认十轮；每输入逐轮交替提交正序 / 逆序 |
| `INPUT_COMMIT`、`SOURCE_COMMITS` | 固定输入编译器；Clang、LLD 源码提交 |
| `TOOL_TARGETS`、`CMAKE_OPTIONS` | 输入编译器后端；负载构建参数 |
| `PREPARE_JOBS` | 准备并行度、NoLTO 每批单元上限；默认 16 |
| `PASS_CPU`、`COLLECTOR_CPU` | 被测 pass、采集器；不同物理核心 |
| `PROJECT`、`SUITE`、`SPEC2017_ROOT` | llvm-project、llvm-test-suite、授权 cpu2017 源码；默认同级 |
| `WORK`、`TOOLROOT` | 固定缓存目录、辅助工具；当前 `WORK` 为相对实验目录的 `../llvm-project/llvm-bench`，工具为其下的 `tools/<INPUT_COMMIT>/` |
| `INPUT_SOURCE`、`INPUT_BUILD`、`WORKLOAD_BUILD` | 编译器源码、编译器构建、负载构建；当前 `src/compiler-<INPUT_COMMIT>/`、`build/compiler-<INPUT_COMMIT>/`、`build/pr-metadata-f14/` |
| `INPUTS`、`CHECKPOINTS`、`RESULTS` | 命令、压缩镜像、本次结果；按输入集分目录 |
| `CRIU_CLIENT`、`CRIU_BIN`、`CRIU_SOCKET`、`CRIU_LAUNCH` | CRIU 客户端、程序、助手 socket、宿主启动命令 |

- 同级目录相对于配置文件定位，运行时解析为绝对路径；镜像复用保留原路径与文件
- 输入或构建参数变化：换 `INPUTS`、`CHECKPOINTS`；批大小变化：换 `CHECKPOINTS`
- 编译器变化：换源码、构建、工具目录
- 配置记录：`experiment.json`；配置变化后重新 `prepare`
- 其他 pass：新增 profile；函数边界 `@entry` / `@return`

## 镜像与测量

| 模式 | 一份镜像 | 执行方式 |
| --- | --- | --- |
| NoLTO | 一个批次管理进程 + 最多 `PREPARE_JOBS` 个编译父进程 | 每批恢复一次；逐编译单元 fork 各提交、各轮 |
| FullLTO | 一个链接目标的编译父进程；单后端分区 | 每目标恢复一次；fork 各提交、各轮 |

- 暂停点：pass 入口；Module、MachineFunction、目标与分析状态
- 保存前：停止 LLVM 全局线程池 → `malloc_trim(0)` → 单线程保存；前置并行保留
- 父进程：保持入口输入；子进程：加载实现 → pass → 写统计 → 退出
- 首次：每批 / 每目标保存一份；保留必要临时文件副本；内存页 `zstd -1` 压缩
- 复用：解压 → 恢复 → 测量 → 删除原始页面；压缩镜像留存；其他映射文件按原路径复用
- 测完释放父进程；FullLTO 分组准备、逐目标测量

默认 Outliner；替换 MachineOutliner、SuffixTree、SuffixTreeNode；固定 LLVM ABI。

| 阶段 | 函数范围 | 核心操作 / CPU 来源 |
| --- | --- | --- |
| `outliner` | `MachineOutliner::runOnModule()` 入口 → 返回 | 完整 pass；`getrusage` user + sys 差值 |
| `mapping` | `MachineOutliner::populateMapper()` 入口 → 返回 | 过滤、分类、整数映射、位置记录；BPF on-CPU |
| `tree` | `llvm::SuffixTree::SuffixTree()` 入口 → 返回 | 构建、节点元数据、叶节点归集；BPF on-CPU |
| `selection` | 后缀树构造返回 → `MachineOutliner::outline()` 返回 | 枚举、筛选、排序、贪心选择、函数创建、指令替换、树销毁；BPF on-CPU |

## 统计与产物

| 指标 | 口径 |
| --- | --- |
| CPU / s | 每轮各输入求和 → 轮次均值；含子调用；同阶段嵌套仅计最外层 |
| `peak_rss_mib` | 阶段内进程绝对峰值；每轮各输入取最大 → 轮次均值 |
| `entry_rss_mib` | 每次阶段调用入口的进程 RSS；每轮各输入取最大 → 轮次均值 |
| `peak_rss_increase_mib` | 每次阶段调用先计算期间峰值减去该次入口 RSS；每轮各输入取最大 → 轮次均值 |
| 相对变化 / % | `(本版/参照版−1)×100`；基线、前一提交；零参照留空 |
| 运行开销 / s | 镜像准备、保存、恢复、fork、加载、镜像收尾；经过时间 |

- RSS：BPF 入口记录、测量核心 `rss_stat`；FILE + ANON + SHMEM
- RSS：含此前保留的数据；阶段峰值不相加；内核批量记账；外部回收未覆盖
- RSS 增量：每次调用独立计算；不能用汇总后的峰值减去汇总后的入口。它表示进程驻留内存相对入口的最大增加，不等于分配器的总申请量。
- BPF CPU：含探针、中断；最终 mapper / candidate 清理计入完整 pass
- 保留全部轮次原值；均值结合逐轮数据查看
- 步骤耗时与 pass CPU 分别统计；`measure` 含实际 pass 执行
- 镜像准备：`measure` 内的前置编译 / 链接、首次保存或解压；保存为其中一项
- 镜像收尾：首次压缩；复用时仅删除解压页

| 位置 | 内容 |
| --- | --- |
| `RESULTS/REPORT.md` | 阶段 CPU、RSS 峰值均值；运行开销；镜像空间 |
| `RESULTS/{raw,comparison,overhead}.csv` | 逐轮数据、提交对比、运行开销 |
| `RESULTS/timings.json` | `prepare_wall_s`、`measure_wall_s`、`summarize_wall_s` |
| `RESULTS/collector/` | 共享采集器与探针 |
| `RESULTS/<输入集>/` | 配置、环境、`measurements.jsonl`、`overhead.json`、采集器日志；临时 `.active/` 正常结束后删除 |
| `CHECKPOINTS/<输入集>/NoLTO/<程序>/<批次>/` | 一批镜像；`units/` 为父进程控制文件；编号为首个单元索引，如 `00000`、`00016` |
| `CHECKPOINTS/<输入集>/FullLTO/<程序>/00000/` | 一个链接目标的镜像 |

- 镜像：`images/`；内存页 `pages-*.img.zst`；空间：原始、压缩留存
- 版本管理：配置、说明、脚本；忽略 `tools/`、`src/`、`build/`、`inputs/`、`results/`

## 一次性环境准备

root、BTF、tracefs、BPF / perf 权限、CRIU 4.2.1、检查点权限。

```bash
# Ubuntu
apt-get install -y gcc g++ python3 python3-pip binutils rsync tcl \
  clang-14 libbpf-dev libelf-dev pkg-config zstd
python3 -m pip install cmake==3.31.10 ninja==1.13.0
mountpoint -q /sys/kernel/tracing || mount -t tracefs tracefs /sys/kernel/tracing
```

- 当前 gensoul：宿主启动入口常驻；镜像目录限定于 `$WORK/results/checkpoint-validation/`
- 无过滤终端：`CRIU_LAUNCH=()`
