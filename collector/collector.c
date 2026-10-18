#define _GNU_SOURCE
#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <linux/perf_event.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>
#include "collector.h"

static int log_message(enum libbpf_print_level level, const char *format,
                       va_list args) {
  return level == LIBBPF_DEBUG ? 0 : vfprintf(stderr, format, args);
}

static struct bpf_link *attach_tracepoint(struct bpf_program *program,
                                         const char *category, const char *name,
                                         unsigned int cpu) {
  char path[256];
  snprintf(path, sizeof(path), "/sys/kernel/tracing/events/%s/%s/id", category, name);
  FILE *file = fopen(path, "r");
  unsigned long long id;
  if (!file)
    return NULL;
  int parsed = fscanf(file, "%llu", &id);
  fclose(file);
  if (parsed != 1)
    return NULL;
  struct perf_event_attr attr = {
      .type = PERF_TYPE_TRACEPOINT, .size = sizeof(attr), .config = id};
  int fd = syscall(__NR_perf_event_open, &attr, -1, cpu, -1, PERF_FLAG_FD_CLOEXEC);
  if (fd < 0)
    return NULL;
  struct bpf_link *link = bpf_program__attach_perf_event(program, fd);
  if (libbpf_get_error(link)) {
    close(fd);
    return NULL;
  }
  return link;
}

static int remember(struct bpf_link ***links, size_t *count, struct bpf_link *link) {
  if (!link || libbpf_get_error(link))
    return -1;
  struct bpf_link **grown = realloc(*links, (*count + 1) * sizeof(**links));
  if (!grown) {
    bpf_link__destroy(link);
    return -1;
  }
  *links = grown;
  grown[(*count)++] = link;
  return 0;
}

static int dump(int fd) {
  unsigned int key = 0;
  struct metrics values, empty = {};
  static const char *names[] = PHASE_NAMES;
  static const unsigned char timed[] = TIMED_PHASES;
  if (bpf_map_lookup_elem(fd, &key, &values) ||
      bpf_map_update_elem(fd, &key, &empty, BPF_ANY))
    return -1;
  for (int i = 0; i < PHASE_COUNT; ++i) {
    printf("{\"phase\":\"%s\",\"cpu_ns\":", names[i]);
    if (timed[i])
      printf("%llu", values.cpu_ns[i]);
    else
      printf("null");
    printf(",\"peak_rss_bytes\":%llu,"
           "\"entry_rss_bytes\":%llu,"
           "\"peak_rss_increase_bytes\":%llu,"
           "\"calls\":%llu}\n",
           values.peak[i], values.entry_rss[i], values.peak_rss_increase[i],
           values.calls[i]);
  }
  printf("{\"status\":[%llu,%lld]}\n", values.errors, values.open);
  fflush(stdout);
  return 0;
}

int main(int argc, char **argv) {
  if (argc != 3) {
    fprintf(stderr, "Usage: collector OBJECT PROBES\n");
    return 2;
  }
  int result = 1;
  struct bpf_object *object = NULL;
  struct bpf_link **links = NULL;
  size_t count = 0;
  FILE *probes = NULL;
  char *line = NULL;
  size_t capacity = 0;
  libbpf_set_print(log_message);
  object = bpf_object__open_file(argv[1], NULL);
  if (libbpf_get_error(object)) {
    object = NULL;
    goto cleanup;
  }
  if (bpf_object__load(object))
    goto cleanup;
  probes = fopen(argv[2], "r");
  if (!probes) {
    perror(argv[2]);
    goto cleanup;
  }
  struct settings cfg = {};
  unsigned int key = 0;
  if (getline(&line, &capacity, probes) < 0 ||
      sscanf(line, "cpu\t%u", &cfg.compile_cpu) != 1)
    goto cleanup;
  int config_fd = bpf_object__find_map_fd_by_name(object, "config");
  int totals_fd = bpf_object__find_map_fd_by_name(object, "totals");
  if (config_fd < 0 || totals_fd < 0 ||
      bpf_map_update_elem(config_fd, &key, &cfg, BPF_ANY))
    goto cleanup;

  struct bpf_program *initializer = bpf_object__find_program_by_name(object, "init_rss");
  struct bpf_link *init_link = bpf_program__attach_iter(initializer, NULL);
  if (libbpf_get_error(init_link))
    goto cleanup;
  int init_fd = bpf_iter_create(bpf_link__fd(init_link));
  if (init_fd >= 0) {
    char buffer[64];
    ssize_t bytes;
    do {
      bytes = read(init_fd, buffer, sizeof(buffer));
    } while (bytes > 0);
    close(init_fd);
    if (bytes < 0)
      init_fd = -1;
  }
  bpf_link__destroy(init_link);
  if (init_fd < 0 || bpf_map_lookup_elem(config_fd, &key, &cfg) || !cfg.rss_ready) {
    fprintf(stderr, "Cannot initialize complete kernel RSS counters\n");
    goto cleanup;
  }

  struct bpf_program *program;
  bpf_object__for_each_program(program, object) {
    if (!strncmp(bpf_program__section_name(program), "uprobe/", 7) ||
        !strncmp(bpf_program__section_name(program), "iter/", 5))
      continue;
    char category[32], name[64];
    int tracepoint = sscanf(bpf_program__section_name(program),
                           "tracepoint/%31[^/]/%63s", category, name) == 2;
    struct bpf_link *link;
    if (tracepoint)
      link = attach_tracepoint(program, category, name, cfg.compile_cpu);
    else
      link = bpf_program__attach(program);
    if (remember(&links, &count, link))
      goto cleanup;
  }

  program = bpf_object__find_program_by_name(object, "boundary");
  while (getline(&line, &capacity, probes) >= 0) {
    unsigned int point, ret;
    unsigned long offset;
    int start;
    if (sscanf(line, "%u\t%u\t%lx\t%n", &point, &ret, &offset, &start) != 3)
      goto cleanup;
    char *path = line + start;
    path[strcspn(path, "\n")] = '\0';
    struct bpf_program *probe = point == 999 ?
        bpf_object__find_program_by_name(object, "register_task") : program;
    struct bpf_uprobe_opts opts = {
        .sz = sizeof(opts), .bpf_cookie = point, .retprobe = ret};
    struct bpf_link *link = bpf_program__attach_uprobe_opts(probe, -1, path,
                                                         offset, &opts);
    if (remember(&links, &count, link))
      goto cleanup;
  }
  puts("{\"ready\":true}");
  fflush(stdout);
  result = 0;
  while (getline(&line, &capacity, stdin) >= 0) {
    if (strcmp(line, "dump\n") || dump(totals_fd)) {
      result = 1;
      break;
    }
  }

cleanup:
  if (result)
    fprintf(stderr, "collector failed\n");
  for (size_t i = 0; i < count; ++i)
    bpf_link__destroy(links[i]);
  free(links);
  free(line);
  if (probes)
    fclose(probes);
  bpf_object__close(object);
  return result;
}
