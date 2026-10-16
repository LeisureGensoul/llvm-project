#!/usr/bin/env bash
# Toolchains, native commands and checkpoint runtime.
check_correctness() {
  "$PYTHON" - "$BENCH/scripts" "$PROJECT" "$WORK/src/check/llvm" "$WORK/build/check" \
    "$TOOLS" "$RESULTS/correctness" "$PROFILE_DIR/tests.list" "$TEST_TARGETS" \
    "$PREPARE_JOBS" "${CHECK_BUILD_TARGETS[@]}" <<'PY'
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET
sys.path.insert(0, sys.argv[1])
from phases import read_list

PROJECT, SOURCE, BUILD, TOOLS, RESULTS = map(Path, sys.argv[2:7])
targets, jobs, build_targets = sys.argv[8], sys.argv[9], sys.argv[10:]
unit_tests, lit_patterns = [], []
for row in read_list(Path(sys.argv[7]).read_text()):
    if row[0] == 'unit' and len(row) == 3:
        unit_tests.append(row[1:])
    elif row[0] == 'lit' and len(row) == 2:
        lit_patterns.append(row[1])
    else:
        sys.exit(f'tests.list: invalid entry {row}')
if not unit_tests and not lit_patterns:
    print('correctness: no tests selected', flush=True)
    sys.exit(0)


def run(name, command):
    print(name, flush=True)
    with (RESULTS / f'{name}.log').open('w') as output:
        output.write(' '.join(str(arg) for arg in command) + '\n')
        output.flush()
        subprocess.run([str(arg) for arg in command], cwd=PROJECT,
                       stdout=output, stderr=subprocess.STDOUT, check=True)


RESULTS.mkdir(parents=True, exist_ok=True)
run('configure', [TOOLS / 'cmake', '-S', SOURCE, '-B', BUILD,
                 '-G', 'Ninja', f'-DCMAKE_MAKE_PROGRAM={TOOLS / "ninja"}',
                 '-DCMAKE_BUILD_TYPE=Release', '-DLLVM_ENABLE_ASSERTIONS=ON',
                 f'-DLLVM_TARGETS_TO_BUILD={targets}', '-DLLVM_INCLUDE_TESTS=ON'])
run('build', [TOOLS / 'cmake', '--build', BUILD, '-j', jobs, '--target', *build_targets])
for index, (binary, test_filter) in enumerate(unit_tests):
    xml = RESULTS / f'unit-{index}.xml'
    run(f'unit-{index}', [BUILD / binary, f'--gtest_filter={test_filter}',
                         f'--gtest_output=xml:{xml}'])
    if not int(ET.parse(xml).getroot().attrib['tests']):
        sys.exit(f'{binary}: no unit tests matched {test_filter}')
if lit_patterns:
    tests = set()
    for pattern in lit_patterns:
        matched = list((SOURCE / 'test').glob(pattern))
        if not matched:
            sys.exit(f'tests.list: no lit tests matched {pattern}')
        tests.update(matched)
    run('lit', [sys.executable, BUILD / 'bin/llvm-lit', '-sv', '-j', jobs,
                '-o', RESULTS / 'lit.json', *sorted(tests)])
print(f'Results: {RESULTS}', flush=True)
PY
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
    -DCMAKE_C_COMPILER=/usr/bin/gcc -DCMAKE_CXX_COMPILER=/usr/bin/g++ \
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
