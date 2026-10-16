#pragma once
#include "llvm/ADT/Twine.h"
#include "llvm/IR/Module.h"
#include "llvm/Pass.h"
#include "llvm/Support/ErrorHandling.h"
#include <cstdlib>
#include <dlfcn.h>
#include <fstream>
#include <malloc.h>
#include <sched.h>
#include <sstream>
#include <string>
#include <sys/resource.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#ifdef LLVM_BENCH_PLUGIN
extern "C" __attribute__((noinline, noclone, visibility("default")))
void llvm_bench_register(unsigned long File, unsigned long Anon,
                         unsigned long Shmem) {
  asm volatile("" : : "r"(File), "r"(Anon), "r"(Shmem) : "memory");
}
#endif

static double benchClock() {
  timespec T;
  clock_gettime(CLOCK_MONOTONIC, &T);
  return T.tv_sec + T.tv_nsec / 1e9;
}

static double benchCPU() {
  rusage R;
  getrusage(RUSAGE_SELF, &R);
  return R.ru_utime.tv_sec + R.ru_utime.tv_usec / 1e6 +
         R.ru_stime.tv_sec + R.ru_stime.tv_usec / 1e6;
}

using BenchRegistration = void (*)(unsigned long, unsigned long, unsigned long);
extern "C" void llvm_bench_prepare_fork();

struct BenchRSS {
  unsigned long File = 0, Anon = 0, Shmem = 0;
  unsigned long total() const { return File + Anon + Shmem; }
};

static BenchRSS benchRSS() {
  BenchRSS RSS;
  std::ifstream Status("/proc/self/status");
  std::string Line, Key;
  while (std::getline(Status, Line)) {
    std::istringstream Field(Line);
    unsigned long Value;
    if (!(Field >> Key >> Value))
      continue;
    if (Key == "RssFile:") RSS.File = Value * 1024;
    if (Key == "RssAnon:") RSS.Anon = Value * 1024;
    if (Key == "RssShmem:") RSS.Shmem = Value * 1024;
  }
  return RSS;
}

static void benchRegister(BenchRegistration Register) {
  auto RSS = benchRSS();
  Register(RSS.File, RSS.Anon, RSS.Shmem);
}

template<class ConfigType, class NativeBody>
static bool benchRunPass(llvm::Pass *Pass, llvm::Module &M,
                         const ConfigType &Config, NativeBody Native) {
  const char *Checkpoint = std::getenv("LLVM_BENCH_CHECKPOINT");
  if (!Checkpoint)
    return Native();
  llvm_bench_prepare_fork();
  std::string Root = Checkpoint;
  double TrimStart = benchClock();
  auto Before = benchRSS();
  malloc_trim(0);
  auto After = benchRSS();
  std::ofstream(Root + "/trim.json") << "{\"seconds\":"
      << benchClock() - TrimStart << ",\"rss_before_bytes\":"
      << Before.total() << ",\"rss_after_bytes\":" << After.total() << "}\n";
  { std::ofstream(Root + "/ready.tmp") << getpid() << '\n'; }
  rename((Root + "/ready.tmp").c_str(), (Root + "/ready").c_str());
  for (;;) {
    std::string Action, Library, Output;
    int CPU = 0;
    {
      std::ifstream Choice(Root + "/choice");
      std::getline(Choice, Action);
      std::getline(Choice, Library);
      std::getline(Choice, Output);
      Choice >> CPU;
    }
    if (Action.empty()) { usleep(1000); continue; }
    unlink((Root + "/choice").c_str());
    if (Action == "finish") _exit(0);
    double Start = benchClock();
    pid_t Child = fork();
    if (Child < 0) llvm::report_fatal_error("checkpoint: fork failed");
    if (Child) {
      int Status;
      waitpid(Child, &Status, 0);
      { std::ofstream(Root + "/done.tmp") << Status << '\n'; }
      rename((Root + "/done.tmp").c_str(), (Root + "/done").c_str());
      continue;
    }
    double Resume = benchClock() - Start;
    cpu_set_t Mask;
    CPU_ZERO(&Mask);
    CPU_SET(CPU, &Mask);
    if (sched_setaffinity(0, sizeof(Mask), &Mask))
      llvm::report_fatal_error("checkpoint: cannot set measurement CPU");
    double Load = benchClock();
    void *Handle = dlopen(Library.c_str(), RTLD_NOW | RTLD_LOCAL);
    if (!Handle)
      llvm::report_fatal_error(llvm::Twine("checkpoint: ") + dlerror());
    using Entry = bool (*)(llvm::Pass *, llvm::Module *, const ConfigType *, double *);
    auto Run = reinterpret_cast<Entry>(dlsym(Handle, "llvm_bench_run_pass"));
    auto Register = reinterpret_cast<BenchRegistration>(dlsym(Handle, "llvm_bench_register"));
    if (!Run || !Register) llvm::report_fatal_error("checkpoint: missing implementation entry");
    Load = benchClock() - Load;
    benchRegister(Register);
    double CPUTime = 0;
    Run(Pass, &M, &Config, &CPUTime);
    {
      std::ofstream Stats(Output + "/native.json");
      Stats.precision(12);
      Stats << "{\"cpu_s\":" << CPUTime << ",\"load_s\":" << Load
            << ",\"fork_s\":" << Resume << "}\n";
    }
    _exit(0);
  }
}
