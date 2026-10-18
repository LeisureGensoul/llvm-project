#!/usr/bin/env bash
# Prepare source snapshots, toolchains and workload commands.
check_correctness() {
  "$PYTHON" "$BENCH/scripts/correctness.py" "$PROJECT" "$WORK/src/check/llvm" \
    "$WORK/build/check" "$TOOLS" "$RESULTS/correctness" "$PROFILE_DIR/tests.list" \
    "$TEST_TARGETS" "$PREPARE_JOBS" "${CHECK_BUILD_TARGETS[@]}"
}

snapshot_source() {
  local revision=$1 destination=$2
  if [[ -f "$destination/.bench-revision" && "$(cat "$destination/.bench-revision")" == "$revision" ]]; then return; fi
  local snapshot
  snapshot=$(mktemp -d "$WORK/build/source.XXXXXX")
  git -C "$PROJECT" archive "$revision" | tar -x -C "$snapshot"
  mkdir -p "$destination"
  rsync -rclp --delete "$snapshot/" "$destination/"
  rm -rf -- "$snapshot"
  printf '%s\n' "$revision" > "$destination/.bench-revision"
}

snapshot_sources() {
  mkdir -p "$WORK/build"
  local latest
  latest=$("$PYTHON" - "$COMMITS" <<'PY'
import shlex, sys
rows = [row for line in open(sys.argv[1]) if (row := shlex.split(line, comments=True))]
print(rows[-1][1])
PY
)
  snapshot_source "$(git -C "$PROJECT" rev-parse "$latest^{commit}")" "$WORK/src/check"
}

build_input_toolchain() {
  INPUT_BIN="$INPUT_BUILD/bin"
  if [[ -f "$INPUT_BUILD/.bench-revision" && "$(cat "$INPUT_BUILD/.bench-revision")" == "$INPUT_COMMIT" ]]; then return; fi
  snapshot_source "$INPUT_COMMIT" "$INPUT_SOURCE"
  cmake -S "$INPUT_SOURCE/llvm" -B "$INPUT_BUILD" -G Ninja \
    -DCMAKE_MAKE_PROGRAM="$TOOLS/ninja" \
    -DCMAKE_C_COMPILER="$(command -v gcc)" -DCMAKE_CXX_COMPILER="$(command -v g++)" \
    -DCMAKE_BUILD_TYPE=Release '-DCMAKE_C_FLAGS_RELEASE=-O3 -DNDEBUG' \
    '-DCMAKE_CXX_FLAGS_RELEASE=-O3 -DNDEBUG' \
    -DLLVM_ENABLE_ASSERTIONS=OFF -DLLVM_ENABLE_LTO=OFF \
    -DLLVM_FORCE_VC_REVISION="$INPUT_COMMIT" \
    '-DLLVM_ENABLE_PROJECTS=clang;lld' -DLLVM_TARGETS_TO_BUILD="$TOOL_TARGETS" \
    -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
  cmake --build "$INPUT_BUILD" -j"$PREPARE_JOBS" --target \
    clang lld llvm-ar llvm-ranlib llvm-tblgen clang-tblgen
  printf '%s\n' "$INPUT_COMMIT" > "$INPUT_BUILD/.bench-revision"
}

read_programs() {
  "$PYTHON" "$BENCH/scripts/bench.py" programs "$RESULTS"
}

prepare_workload() {
  BUILD="$WORKLOAD_BUILD/$mode"
  if [[ "$WORKLOAD" == ctmark || "$WORKLOAD" == spec2017 ]]; then
    SUITE_OPTIONS=()
    SUITE_SUBDIR=CTMark
    if [[ "$WORKLOAD" == spec2017 ]]; then
      SUITE_SUBDIR=''
      while read -r name target binary; do SUITE_SUBDIR+="${binary%/*};"; done < <(read_programs)
      SUITE_SUBDIR=${SUITE_SUBDIR%;}
      SUITE_OPTIONS=(-DTEST_SUITE_SPEC2017_ROOT="$SPEC2017_ROOT" -DTEST_SUITE_RUN_TYPE=test -DTEST_SUITE_FORTRAN=OFF)
    fi
    FLAGS="$COMPILE_FLAGS"
    LINK="--ld-path=$INPUT_BIN/ld.lld -Wl,--build-id=none"
    if [[ "$mode" == NoLTO ]]; then
      FLAGS+=' -fno-lto'
      LINK+=' -fno-lto'
      for option in "${LLVM_OPTIONS[@]}"; do FLAGS+=" -mllvm $option"; done
    else
      FLAGS+=' -flto=full'
      LINK+=' -flto=full -Wl,--lto-O2,--lto-CGO2'
      for option in "${LLVM_OPTIONS[@]}"; do LINK+=",--mllvm=$option"; done
    fi
    cmake -S "$SUITE" -B "$BUILD" -G Ninja \
      -DCMAKE_MAKE_PROGRAM="$TOOLS/ninja" -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_C_COMPILER="$INPUT_BIN/clang" -DCMAKE_CXX_COMPILER="$INPUT_BIN/clang++" \
      -DCMAKE_AR="$INPUT_BIN/llvm-ar" -DCMAKE_RANLIB="$INPUT_BIN/llvm-ranlib" \
      -DCMAKE_C_FLAGS_RELEASE="$FLAGS" -DCMAKE_CXX_FLAGS_RELEASE="$FLAGS" \
      -DCMAKE_EXE_LINKER_FLAGS="$LINK" -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
      -DTEST_SUITE_SUBDIRS="$SUITE_SUBDIR" -DTEST_SUITE_RUN_BENCHMARKS=OFF \
      -DTEST_SUITE_COLLECT_COMPILE_TIME=OFF -DTEST_SUITE_REPORT_COMPILE_MAX_RSS=OFF \
      -DTEST_SUITE_COLLECT_CODE_SIZE=OFF -DTEST_SUITE_USE_PERF=OFF \
      "${SUITE_OPTIONS[@]}" "${CMAKE_OPTIONS[@]}"
  else
    "$PYTHON" "$BENCH/scripts/commands.py" project --results "$INPUTS" \
      --mode "$mode" --build "$BUILD" --tools "$TOOLS" --input-bin "$INPUT_BIN" \
      --source "$SOURCE_DIR" --project "$WORKLOAD" --source-commit "$SOURCE_COMMIT" \
      --llvm-targets "$LLVM_TARGETS" --jobs "$PREPARE_JOBS" --flags="$COMPILE_FLAGS" \
      "${LLVM_OPTIONS[@]/#/--llvm-option=}" "${CMAKE_OPTIONS[@]/#/--cmake-option=}"
  fi
}

plan_inputs() {
  SOURCE_DIR="$INPUT_SOURCE/llvm"
  SOURCE_REVISION="${SOURCE_COMMIT:-}"
  if [[ "$WORKLOAD" == ctmark || "$WORKLOAD" == spec2017 ]]; then
    SOURCE_REVISION=$(git -C "$SUITE" rev-parse HEAD)
    if [[ "$WORKLOAD" == spec2017 ]]; then SOURCE_REVISION+="/$(cat "$SPEC2017_ROOT/version.txt")"; fi
  elif [[ "$SOURCE_COMMIT" != "$INPUT_COMMIT" ]]; then
    SOURCE_DIR="$WORK/src/$WORKLOAD/$SOURCE_COMMIT/llvm"
    snapshot_source "$SOURCE_COMMIT" "$WORK/src/$WORKLOAD/$SOURCE_COMMIT"
  fi
  "$PYTHON" "$BENCH/scripts/bench.py" plan "$RESULTS" "$BENCH" "$INPUTS" \
    "$CHECKPOINTS" "$TOOLROOT" "$INPUT_BIN" "$WORKLOAD" "$PROFILE" "$INPUT_COMMIT" \
    --rounds "$ROUNDS" --cpu "$PASS_CPU" --collector-cpu "$COLLECTOR_CPU" \
    --bpf-clang "$BPF_CLANG" --jobs "$PREPARE_JOBS" \
    --project "$PROJECT" --flags="$COMPILE_FLAGS" --commits="$COMMITS" --input-set="$set" \
    --source-revision="$SOURCE_REVISION" --targets="$LLVM_TARGETS" \
    "${LLVM_OPTIONS[@]/#/--option=}" "${CMAKE_OPTIONS[@]/#/--cmake-option=}" \
    --modes "${MODES[@]}" --programs "${PROGRAMS[@]}" \
    --criu "$("$PYTHON" -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${CRIU[@]}")" \
    --criu-bin "$CRIU_BIN" \
    --criu-launch "$("$PYTHON" -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${CRIU_LAUNCH[@]}")"
  uname -a > "$RESULTS/environment.txt"
  lscpu >> "$RESULTS/environment.txt"
  for mode in "${MODES[@]}"; do
    PREPARED=false
    while read -r PROGRAM TARGET BINARY; do
      if [[ -f "$INPUTS/$mode/$PROGRAM/manifest.json" ]]; then continue; fi
      if [[ "$PREPARED" == false ]]; then prepare_workload; PREPARED=true; fi
      "$PYTHON" "$BENCH/scripts/commands.py" inputs "$RESULTS/experiment.json" "$mode" "$PROGRAM" \
        "$WORKLOAD_BUILD/$mode" "$TARGET" "$BINARY"
    done < <(read_programs)
  done
}
