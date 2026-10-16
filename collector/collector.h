#ifndef LLVM_BENCH_COLLECTOR_H
#define LLVM_BENCH_COLLECTOR_H

#include "config.h"

struct metrics {
  unsigned long long cpu_ns[PHASE_COUNT];
  unsigned long long peak[PHASE_COUNT];
  unsigned long long calls[PHASE_COUNT];
  unsigned long long errors, last;
  long long open;
  unsigned int running;
};

struct settings {
  unsigned int compile_cpu;
};

#endif
