# 配置与准备

## 环境

使用 Linux x86_64、Python 3.10+ 和 4096 字节内存页。测量需要 root、可读 BTF 与 tracefs `rss_stat`、支持 BPF 的 Clang，以及 CRIU、mount / PID namespace 权限。

```bash
# Ubuntu
apt-get install -y gcc g++ python3 python3-pip binutils rsync tcl \
  clang libbpf-dev libelf-dev pkg-config zstd criu
python3 -m pip install cmake==3.31.10 ninja==1.13.0
mountpoint -q /sys/kernel/tracing || mount -t tracefs tracefs /sys/kernel/tracing
```

SPEC2017 输入需要授权的 `cpu2017/` 源码。

## 本机设置

复制 [config.local.sh.example](../config.local.sh.example) 为 `config.local.sh`，只覆盖本机需要调整的设置；该文件忽略提交。`BENCH_LOCAL_CONFIG=路径` 可选择另一份配置。所有相对路径均从 `config.sh` 所在目录解析。

[commits.list](../commits.list) 每行填写标签和提交，首项为基线，后续为待测优化；所有提交须在 `PROJECT` 仓库中可获取。示例使用固定基线和 `outliner-dev`。可通过 `COMMITS` 选择本机清单。

| 设置 | 用途与默认值 |
| --- | --- |
| `PROFILE`、`COMMITS` | 测量适配与版本清单；`outliner`、`commits.list` |
| `INPUT_SETS`、`PROGRAMS` | 输入集与程序；`ctmark`、`all` |
| `MODES`、`ROUNDS` | `NoLTO` / `FullLTO`，各十轮 |
| `INPUT_COMMIT`、`SOURCE_COMMITS` | 固定输入编译器提交；Clang、LLD 负载源码提交 |
| `TOOL_TARGETS`、`CMAKE_OPTIONS` | 输入编译器后端；负载额外构建参数 |
| `PREPARE_JOBS` | 准备并行度、NoLTO 每批单元上限；16 |
| `PASS_CPU`、`COLLECTOR_CPU` | 测量与采集核心；默认选择两个可用的不同物理核心 |
| `PROJECT`、`SUITE`、`SPEC2017_ROOT` | `../llvm-project`、`llvm-test-suite/`、`cpu2017/` |
| `WORK` | 缓存根目录；实验目录 |
| `TOOLROOT` | 辅助工具与实现库；`WORK/tools/<INPUT_COMMIT>/` |
| `INPUT_SOURCE`、`INPUT_BUILD` | `WORK/src/compiler-<INPUT_COMMIT>/`、`WORK/build/compiler-<INPUT_COMMIT>/` |
| `WORKLOAD_BUILD` | 负载构建；`WORK/build/workloads-<INPUT_COMMIT>/` |
| `INPUTS`、`CHECKPOINTS` | 输入清单与镜像；`WORK/inputs/<INPUT_COMMIT>/`、`WORK/checkpoints/<INPUT_COMMIT>/` |
| `RESULTS` | 本次结果；`WORK/results/run/` |
| `PYTHON`、`BPF_CLANG` | Python 与 BPF 编译器；`python3`、`clang` |
| `CRIU_CLIENT`、`CRIU_BIN` | 仓库内客户端 `checkpoint/criu.py`、`criu` |
| `CRIU_SOCKET`、`CRIU_LAUNCH` | 助手 socket 与宿主启动命令；`WORK/build/criu.sock`、空数组 |

`CRIU_LAUNCH=()` 从未被 seccomp 过滤的 root 环境直接启动助手。容器中需配置宿主允许的启动命令；程序在命令末尾追加 Python、`checkpoint/helper.py` 及其参数。宿主要求固定客户端路径时设置 `CRIU_CLIENT`。

## 缓存复用

输入编译器、负载构建、实现库、输入清单与镜像可在同一机器上复用。结果目录记录本次配置、原始数据和报告。切换 LLVM 分支不会改变独立的 `llvm-bench` 工作目录。

- 输入或构建参数变化：使用新的 `INPUTS` 和 `CHECKPOINTS`；批大小变化时使用新的 `CHECKPOINTS`。
- 输入编译器变化：使用对应的源码、构建和工具目录；默认路径按提交区分。
- 配置变化后重新 `prepare`，配置记录于各输入集的 `experiment.json`。

CMake 构建和进程镜像依赖绝对路径。复用已有支持时，通过本机配置指向原目录，保留原路径与文件；其他机器重新准备缓存。

## 测量适配

[workloads.list](../workloads.list) 提供 CTMark 10 项、SPEC CPU2017 C/C++ Rate 16 项及 Clang-X86、LLD-X86。

[profiles/outliner/](../profiles/outliner/) 包含共用编译参数、阶段边界、测试选择和 checkpoint 实现适配。默认替换 MachineOutliner、SuffixTree 和 SuffixTreeNode，保持固定 LLVM ABI；适配器会拒绝其替换范围之外的实现改动。测量其他 pass 时增加对应 profile。
