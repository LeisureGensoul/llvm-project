#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C SOURCE_DATE_EPOCH=1791254664 PYTHONDONTWRITEBYTECODE=1
source "$(dirname -- "${BASH_SOURCE[0]}")/config.sh"
started=$("$PYTHON" -c 'import time; print(time.monotonic())')
select_workload() {
  local set=$1
  read -r WORKLOAD SET_TARGETS < <(awk -v selected="$set" '$1 == selected { print $2, $3; exit }' "$BENCH/workloads.list")
  SOURCE_COMMIT=${SOURCE_COMMITS[$WORKLOAD]:-}
  LLVM_TARGETS="$SET_TARGETS"
  WORKLOAD_BUILD+="/$set"
  INPUTS+="/$set"
  CHECKPOINTS+="/$set"
  RESULTS+="/$set"
}
stop_helper() {
  "$PYTHON" "$BENCH/scripts/bench.py" helper "$RESULTS" stop --sets "${INPUT_SETS[@]}"
}
case "${1:-}" in
  prepare)
    PROFILE_DIR="$BENCH/profiles/$PROFILE"
    source "$PROFILE_DIR/config.sh"
    CRIU=("$PYTHON" "$BENCH/checkpoint/criu.py" --socket "$CRIU_SOCKET")
    TOOLS="$TOOLROOT/bin"
    export PATH="$TOOLS:$PATH"
    source "$BENCH/scripts/prepare.sh"
    mkdir -p "$RESULTS" "$WORK/build" "$TOOLS"
    for tool in cmake ninja; do
      [[ -x "$TOOLS/$tool" ]] || ln -s "$(command -v "$tool")" "$TOOLS/$tool"
    done
    INPUT_COMMIT=$(git -C "$PROJECT" rev-parse "$INPUT_COMMIT^{commit}")
    snapshot_sources
    build_input_toolchain
    check_correctness & checks=$!
    for set in "${INPUT_SETS[@]}"; do
      (
        select_workload "$set"
        if [[ "$WORKLOAD" == clang || "$WORKLOAD" == lld ]]; then
          SOURCE_COMMIT=$(git -C "$PROJECT" rev-parse "$SOURCE_COMMIT^{commit}")
        fi
        mkdir -p "$INPUTS" "$RESULTS"
        plan_inputs
      )
    done
    "$PYTHON" "$BENCH/scripts/bench.py" runtime "$RESULTS" --sets "${INPUT_SETS[@]}"
    wait "$checks" ;;
  measure)
    "$PYTHON" "$BENCH/scripts/bench.py" helper "$RESULTS" start --sets "${INPUT_SETS[@]}"
    trap stop_helper EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    for set in "${INPUT_SETS[@]}"; do
      unshare --mount --pid --fork --mount-proc "$PYTHON" "$BENCH/scripts/bench.py" measure "$RESULTS/$set"
    done
    trap - EXIT
    stop_helper ;;
  summarize) "$PYTHON" "$BENCH/scripts/bench.py" summarize "$RESULTS" --sets "${INPUT_SETS[@]}" ;;
  *) printf 'Usage: bash run.sh {prepare|measure|summarize}\n' >&2; exit 2 ;;
esac

"$PYTHON" - "$RESULTS/timings.json" "$1" "$started" <<'PY'
import json
from pathlib import Path
import sys
import time

path, step, started = Path(sys.argv[1]), sys.argv[2], float(sys.argv[3])
elapsed = time.monotonic() - started
values = json.loads(path.read_text()) if path.exists() else {}
values[step + '_wall_s'] = elapsed
path.write_text(json.dumps(values, indent=2) + '\n')
print(f'{step}: {elapsed:.2f} s', flush=True)
PY
