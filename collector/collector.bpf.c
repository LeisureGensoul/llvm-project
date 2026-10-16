#include <linux/bpf.h>
#include <linux/ptrace.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_tracing.h>
#include "collector.h"
#include "layout.h"

struct task_struct {
  int pid, tgid;
} __attribute__((preserve_access_index));

struct task_state {
  unsigned int depth[PHASE_COUNT];
};

struct process_state {
  unsigned int active[PHASE_COUNT];
  long long rss[4];
};

#define HASH_MAP(name, value_type, capacity) \
  struct { \
    __uint(type, BPF_MAP_TYPE_HASH); \
    __uint(max_entries, capacity); \
    __type(key, unsigned int); \
    __type(value, value_type); \
  } name SEC(".maps")

#define ARRAY_MAP(name, value_type) \
  struct { \
    __uint(type, BPF_MAP_TYPE_ARRAY); \
    __uint(max_entries, 1); \
    __type(key, unsigned int); \
    __type(value, value_type); \
  } name SEC(".maps")

HASH_MAP(tasks, struct task_state, 64);
HASH_MAP(processes, struct process_state, 64);
ARRAY_MAP(totals, struct metrics);
ARRAY_MAP(config, struct settings);

static __always_inline struct metrics *summary(void) {
  unsigned int zero = 0;
  return bpf_map_lookup_elem(&totals, &zero);
}

static __always_inline struct settings *settings(void) {
  unsigned int zero = 0;
  return bpf_map_lookup_elem(&config, &zero);
}

static __always_inline void charge(struct metrics *s) {
  unsigned long long now = bpf_ktime_get_ns();
  struct process_state *p = bpf_map_lookup_elem(&processes, &s->running);
  const unsigned char timed[] = TIMED_PHASES;
#pragma unroll
  for (int i = 0; i < PHASE_COUNT; ++i)
    if (p && timed[i] && p->active[i])
      s->cpu_ns[i] += (now - s->last) * p->active[i];
  s->last = now;
}

static __always_inline unsigned long long rss(struct process_state *p) {
  return p->rss[0] + p->rss[1] + p->rss[3];
}

static __always_inline void begin_phase(struct metrics *s, struct task_state *t,
                                       struct process_state *p, int i, int parent,
                                       unsigned long long total) {
  if (parent >= 0 && !t->depth[parent])
    return;
  if (t->depth[i]++)
    return;
  p->active[i]++;
  s->open++;
  s->calls[i]++;
  if (total > s->peak[i])
    s->peak[i] = total;
}

static __always_inline void end_phase(struct metrics *s, struct task_state *t,
                                     struct process_state *p, int i, int parent) {
  if (t->depth[i]) {
    if (--t->depth[i])
      return;
    unsigned long long total = rss(p);
    if (total > s->peak[i])
      s->peak[i] = total;
    p->active[i]--;
    s->open--;
  } else if (parent < 0 || t->depth[parent]) {
    s->errors++;
  }
}

SEC("uprobe/phase")
int boundary(struct pt_regs *ctx) {
  unsigned long long id = bpf_get_current_pid_tgid();
  unsigned int tid = id, pid = id >> 32;
  struct task_state *t = bpf_map_lookup_elem(&tasks, &tid);
  if (!t)
    return 0;
  struct process_state *p = bpf_map_lookup_elem(&processes, &pid);
  struct metrics *s = summary();
  struct settings *cfg = settings();
  if (!s || !cfg)
    return 0;
  if (!p || bpf_get_smp_processor_id() != cfg->compile_cpu) {
    s->errors++;
    return 0;
  }
  unsigned long long total = rss(p);
  charge(s);
  s->running = pid;
  switch (bpf_get_attach_cookie(ctx)) {
    PHASE_ACTIONS
  }
  return 0;
}

SEC("uprobe/register")
int register_task(struct pt_regs *ctx) {
  unsigned long long id = bpf_get_current_pid_tgid();
  unsigned int tid = id, pid = id >> 32;
  struct task_state t = {};
  struct process_state p = {};
  p.rss[0] = PT_REGS_PARM1(ctx);
  p.rss[1] = PT_REGS_PARM2(ctx);
  p.rss[3] = PT_REGS_PARM3(ctx);
  if (bpf_map_update_elem(&tasks, &tid, &t, BPF_ANY) ||
      bpf_map_update_elem(&processes, &pid, &p, BPF_ANY)) {
    struct metrics *s = summary();
    if (s) s->errors++;
  }
  return 0;
}

SEC("tracepoint/kmem/rss_stat")
int rss_update(struct rss_event *ctx) {
  unsigned long long id = bpf_get_current_pid_tgid();
  unsigned int tid = id, pid = id >> 32;
  struct task_state *t = bpf_map_lookup_elem(&tasks, &tid);
  if (!t)
    return 0;
  struct metrics *s = summary();
  if (!s)
    return 0;
  struct process_state *p = bpf_map_lookup_elem(&processes, &pid);
  if (!p || !ctx->curr)
    return 0;
  int member = ctx->member;
  if (member != 0 && member != 1 && member != 3)
    return 0;
  p->rss[member] = ctx->size;
  if (!s->open)
    return 0;
  unsigned long long total = rss(p);
#pragma unroll
  for (int i = 0; i < PHASE_COUNT; ++i) {
    if (p->active[i] && total > s->peak[i])
      s->peak[i] = total;
  }
  return 0;
}

SEC("raw_tracepoint/sched_switch")
int task_switch(struct bpf_raw_tracepoint_args *ctx) {
  struct settings *cfg = settings();
  if (!cfg || bpf_get_smp_processor_id() != cfg->compile_cpu)
    return 0;
  struct metrics *s = summary();
  if (!s || !s->open)
    return 0;
  charge(s);
  struct task_struct *next = (void *)ctx->args[2];
  unsigned int tid = BPF_CORE_READ(next, pid);
  s->running = bpf_map_lookup_elem(&tasks, &tid) ? BPF_CORE_READ(next, tgid) : 0;
  return 0;
}

SEC("tracepoint/sched/sched_process_exit")
int task_exit(void *ctx) {
  unsigned long long id = bpf_get_current_pid_tgid();
  unsigned int tid = id, pid = id >> 32;
  struct task_state *t = bpf_map_lookup_elem(&tasks, &tid);
  if (!t)
    return 0;
  struct metrics *s = summary();
  if (!s)
    return 0;
#pragma unroll
  for (int i = 0; i < PHASE_COUNT; ++i)
    if (t->depth[i])
      s->errors++;
  bpf_map_delete_elem(&tasks, &tid);
  bpf_map_delete_elem(&processes, &pid);
  return 0;
}

char LICENSE[] SEC("license") = "GPL";
