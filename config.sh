# 相对于本配置文件定位，运行时解析为绝对路径。
BENCH=$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")
PROJECT=../llvm-project
WORK=.
PROFILE=outliner
COMMITS=commits.list
INPUT_SETS=(ctmark)
PROGRAMS=(all)
MODES=(NoLTO FullLTO)
ROUNDS=10

INPUT_COMMIT=f14f34f217a99d77628d547b1b3ba9fa47fbd06b
declare -A SOURCE_COMMITS=()
CMAKE_OPTIONS=()
TOOL_TARGETS=X86
PREPARE_JOBS=16
SUITE=../llvm-test-suite
SPEC2017_ROOT=../cpu2017
PASS_CPU=
COLLECTOR_CPU=
BPF_CLANG=clang
PYTHON=python3
CRIU_BIN=criu
CRIU_LAUNCH=()

# 本机可覆盖缓存位置、工具及宿主启动入口。
RESULTS= INPUT_SOURCE= INPUT_BUILD= WORKLOAD_BUILD= INPUTS= CHECKPOINTS=
TOOLROOT= CRIU_CLIENT= CRIU_SOCKET=
bench_local_config=${BENCH_LOCAL_CONFIG:-"$BENCH/config.local.sh"}
[[ "$bench_local_config" = /* ]] || bench_local_config="$BENCH/$bench_local_config"
if [[ -f "$bench_local_config" ]]; then
  source "$bench_local_config"
elif [[ -n "${BENCH_LOCAL_CONFIG:-}" ]]; then
  printf 'Configuration not found: %s\n' "$bench_local_config" >&2
  return 1
fi

bench_path() {
  local value=$1
  [[ "$value" = /* ]] || value="$BENCH/$value"
  realpath -ms -- "$value"
}
PROJECT=$(bench_path "$PROJECT")
WORK=$(bench_path "$WORK")
RESULTS=${RESULTS:-"$WORK/results/run"}
INPUT_SOURCE=${INPUT_SOURCE:-"$WORK/src/compiler-$INPUT_COMMIT"}
INPUT_BUILD=${INPUT_BUILD:-"$WORK/build/compiler-$INPUT_COMMIT"}
WORKLOAD_BUILD=${WORKLOAD_BUILD:-"$WORK/build/workloads-$INPUT_COMMIT"}
INPUTS=${INPUTS:-"$WORK/inputs/$INPUT_COMMIT"}
CHECKPOINTS=${CHECKPOINTS:-"$WORK/checkpoints/$INPUT_COMMIT"}
TOOLROOT=${TOOLROOT:-"$WORK/tools/$INPUT_COMMIT"}
CRIU_CLIENT=${CRIU_CLIENT:-"$BENCH/checkpoint/criu.py"}
CRIU_SOCKET=${CRIU_SOCKET:-"$WORK/build/criu.sock"}
for bench_path_name in COMMITS SUITE SPEC2017_ROOT RESULTS INPUT_SOURCE INPUT_BUILD \
                       WORKLOAD_BUILD INPUTS CHECKPOINTS TOOLROOT CRIU_CLIENT CRIU_SOCKET; do
  printf -v "$bench_path_name" '%s' "$(bench_path "${!bench_path_name}")"
done
SOURCE_COMMITS[clang]=${SOURCE_COMMITS[clang]:-$INPUT_COMMIT}
SOURCE_COMMITS[lld]=${SOURCE_COMMITS[lld]:-$INPUT_COMMIT}
if [[ -z "$PASS_CPU" || -z "$COLLECTOR_CPU" ]]; then
  bench_cpus=$("$PYTHON" "$BENCH/scripts/check.py" cpus) || return 1
  read -r bench_pass_cpu bench_collector_cpu <<< "$bench_cpus"
  PASS_CPU=${PASS_CPU:-$bench_pass_cpu}
  COLLECTOR_CPU=${COLLECTOR_CPU:-$bench_collector_cpu}
fi
unset -f bench_path
unset bench_local_config bench_path_name bench_cpus bench_pass_cpu bench_collector_cpu
