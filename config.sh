# 相对于本配置文件定位，运行时解析为绝对路径。
BENCH=$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")
PROJECT=$(realpath -m -- "$BENCH/../llvm-project")
# 固定构建和镜像的绝对路径。
WORK="$PROJECT/llvm-bench"

# 比对
PROFILE=outliner
COMMITS="$BENCH/commits.list"
INPUT_SETS=(ctmark spec2017 clang-X86 lld-X86) # workloads.list
PROGRAMS=(all)                              # 单个输入集可选单项
MODES=(NoLTO FullLTO)
ROUNDS=10
RESULTS="$WORK/results/outliner-f14" # 每次换名称

# 固定输入编译器与负载
INPUT_COMMIT=f14f34f217a99d77628d547b1b3ba9fa47fbd06b
declare -A SOURCE_COMMITS=(
  [clang]="$INPUT_COMMIT"
  [lld]="$INPUT_COMMIT"
)
CMAKE_OPTIONS=()
TOOL_TARGETS=X86
PREPARE_JOBS=16 # 准备并行度、NoLTO 每批单元上限
SUITE=$(realpath -m -- "$BENCH/../llvm-test-suite")
SPEC2017_ROOT=$(realpath -m -- "$BENCH/../cpu2017")
INPUT_SOURCE="$WORK/src/compiler-$INPUT_COMMIT"
INPUT_BUILD="$WORK/build/compiler-$INPUT_COMMIT"
WORKLOAD_BUILD="$WORK/build/pr-metadata-f14"
INPUTS="$WORK/inputs/pr-metadata-f14"
CHECKPOINTS="$WORK/results/checkpoint-validation/pr-metadata-f14/checkpoints" # 宿主入口限定范围

# 测量；pass 与采集器使用不同物理核心
PASS_CPU=8
COLLECTOR_CPU=10

# 工具与 CRIU 助手
TOOLROOT="$WORK/tools/$INPUT_COMMIT"
BPF_CLANG=clang-14
PYTHON=/usr/bin/python3
CRIU_CLIENT="$WORK/checkpoint/criu.py"
CRIU_BIN=/usr/local/sbin/criu
CRIU_SOCKET="$WORK/build/criu.sock"
CRIU_LAUNCH=("$PYTHON" /usr/local/bin/criu-helper launch) # gensoul 无过滤启动入口
