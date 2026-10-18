# LLVM 阶段性能比对

在固定编译输入的 pass 入口保存进程镜像，再从相同状态 fork 各版本实现，比较阶段 CPU 时间和 RSS。

## 使用

需要 Linux x86_64、Python 3.10+、root，以及 BPF、CRIU 和进程命名空间权限。依赖安装与本机配置见 [配置说明](docs/configuration.md)。

```bash
cd llvm-bench
cp config.local.sh.example config.local.sh
# 修改本机配置和 commits.list，确保待测提交在 LLVM 仓库中可获取。
bash run.sh check
bash run.sh prepare
bash run.sh measure
bash run.sh summarize
```

| 命令 | 职责 |
| --- | --- |
| `check` | 检查工具、内核接口、CPU、源码及待测提交 |
| `prepare` | 准备固定输入编译器、输入命令、实现库和采集器；验证末项提交 |
| `measure` | 保存或恢复镜像，逐版本、逐轮测量；自动启停 CRIU 助手 |
| `summarize` | 生成逐轮数据、版本比较和报告 |

默认测量 CTMark，NoLTO / FullLTO，各十轮。每次实验指定新的 `RESULTS`；`prepare` 和 `measure` 会拒绝覆盖已有原始测量数据。结果入口为 `RESULTS/REPORT.md`，统计口径见 [测量说明](docs/measurement.md)。

## 组织

| 位置 | 职责 |
| --- | --- |
| [config.sh](config.sh)、[commits.list](commits.list)、[workloads.list](workloads.list) | 默认配置、待测版本、输入集 |
| [config.local.sh.example](config.local.sh.example) | 本机配置示例 |
| [scripts/](scripts/) | 准备、测量和汇总流程 |
| [collector/](collector/) | BPF 阶段 CPU 与 RSS 采集 |
| [checkpoint/](checkpoint/) | CRIU 客户端、宿主助手和 pass 入口运行时 |
| [profiles/outliner/](profiles/outliner/) | Outliner 编译参数、阶段边界、测试选择和实现适配 |
| [docs/](docs/) | 配置、缓存复用与测量口径 |

`scripts/prepare.sh` 负责源码与构建准备，`correctness.py` 执行正确性检查，`commands.py` 收集编译命令。`bench.py` 调度实验，`checkpoint.py` 管理镜像与 fork，`phases.py` 配置采集，`report.py` 汇总并生成报告。

## 本机目录

```text
工作目录/
├── llvm-project/
└── llvm-bench/
    ├── llvm-test-suite/  # 独立 Git 检出
    ├── cpu2017/          # 授权源码；选择 SPEC 时需要
    ├── tools/           # 辅助工具、checkpoint host、各版本实现库
    ├── src/             # 固定源码快照
    ├── build/           # 编译器、正确性检查和负载构建
    ├── inputs/          # 编译命令与输入清单
    ├── checkpoints/     # 压缩进程镜像
    └── results/         # 原始数据、报告与本地 PR 材料
```

这些依赖与产物均忽略提交，可通过配置或相对符号链接复用已有目录。配置中的相对路径从实验目录解析，与启动时的工作目录无关。已有构建和镜像依赖原绝对路径，复用时保留原文件及路径；新机器重新准备本机缓存。
