#!/usr/bin/env python3
"""Check prerequisites before preparing or restoring an experiment."""
import argparse
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys


def core(cpu):
    root = Path(f'/sys/devices/system/cpu/cpu{cpu}/topology')
    return tuple((root / name).read_text().strip()
                 for name in ('physical_package_id', 'core_id'))


def cpus():
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise ValueError('The checkpoint collector currently supports Linux x86_64.')
    selected, cores = [], set()
    for cpu in sorted(os.sched_getaffinity(0)):
        identity = core(cpu)
        if identity not in cores:
            selected.append(cpu)
            cores.add(identity)
        if len(selected) == 2:
            return selected
    raise ValueError('Two available physical CPU cores are required; check CPU affinity.')


def check(args):
    errors = []

    def require(condition, message):
        if not condition:
            errors.append(message)
        return condition

    def command(argv):
        return subprocess.run(argv, text=True, capture_output=True)

    def revision(value):
        result = command(['git', '-C', str(args.project), 'rev-parse', '--verify',
                          value + '^{commit}'])
        require(result.returncode == 0,
                f'LLVM revision is unavailable: {value}; fetch it or edit {args.commits}.')

    require(platform.system() == 'Linux' and platform.machine() == 'x86_64',
            'The checkpoint collector currently supports Linux x86_64.')
    require(sys.version_info >= (3, 10), 'Python 3.10 or newer is required.')
    require(os.geteuid() == 0, 'Run checkpoint measurements as root with BPF and namespace privileges.')
    require(os.sysconf('SC_PAGE_SIZE') == 4096, 'The RSS collector requires 4096-byte pages.')
    pid_max = int(Path('/proc/sys/kernel/pid_max').read_text())
    require(args.jobs > 0, 'PREPARE_JOBS must be positive.')
    require(10000 + (args.jobs + 32) * 1024 < pid_max,
            'The checkpoint PID range exceeds kernel.pid_max; increase that host setting.')
    tools = ['git', 'gcc', 'g++', 'cmake', 'ninja', 'rsync', 'tar', 'pkg-config',
             'zstd', 'unshare', 'nsenter', 'nm', 'readelf', 'c++filt', 'lscpu',
             args.bpf_clang, args.criu]
    available = {tool: shutil.which(tool) for tool in tools}
    for tool, path in available.items():
        require(path, f'Missing tool: {tool}; install it or update config.local.sh/PATH.')
    if available['pkg-config']:
        require(command(['pkg-config', '--exists', 'libbpf', 'libelf']).returncode == 0,
                'Install the libbpf and libelf development packages.')
    if available[args.bpf_clang]:
        probe = subprocess.run([args.bpf_clang, '-target', 'bpf', '-O2', '-x', 'c',
                                '-c', '-', '-o', os.devnull], input='int value;\n',
                               text=True, capture_output=True)
        require(probe.returncode == 0,
                f'{args.bpf_clang} cannot compile BPF; choose a BPF-enabled Clang.')
    for path in ['/sys/kernel/btf/vmlinux',
                 '/sys/kernel/tracing/events/kmem/rss_stat/format']:
        require(os.access(path, os.R_OK), f'Required kernel interface is unavailable: {path}.')
    allowed = os.sched_getaffinity(0)
    valid_cpus = all(cpu in allowed for cpu in [args.cpu, args.collector_cpu])
    require(valid_cpus, 'PASS_CPU and COLLECTOR_CPU must be in the process CPU affinity.')
    if valid_cpus:
        require(core(args.cpu) != core(args.collector_cpu),
                'PASS_CPU and COLLECTOR_CPU must use different physical cores.')
    if available['unshare']:
        require(command(['unshare', '--mount', '--pid', '--fork', '--mount-proc', 'true']).returncode == 0,
                'Mount/PID namespaces are unavailable; check container permissions.')
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines()
                  if ':' in line)
    launch = json.loads(args.launch)
    if launch:
        require(shutil.which(launch[0]), f'CRIU launcher is unavailable: {launch[0]}.')
        for item in launch[1:]:
            if item.startswith('/'):
                require(Path(item).is_file(), f'CRIU launcher file is unavailable: {item}.')
    else:
        require(int(status['Seccomp']) == 0,
                'Configure CRIU_LAUNCH in config.local.sh or use an unfiltered root environment.')
    require(args.client.is_file(), f'CRIU client is unavailable: {args.client}.')
    if available['git'] and require((args.project / 'llvm').is_dir(),
                                    f'PROJECT is not an LLVM source checkout: {args.project}.'):
        revision(args.input_commit)
        if require(args.commits.is_file(), f'Commit list is unavailable: {args.commits}.'):
            rows = [fields for line in args.commits.read_text().splitlines()
                    if (fields := shlex.split(line, comments=True))]
            if require(len(rows) >= 2 and all(len(row) == 2 for row in rows),
                       f'{args.commits}: select a baseline and at least one change.'):
                require(len({label for label, _ in rows}) == len(rows) and
                        all(label.replace('_', '').isalnum() for label, _ in rows),
                        f'{args.commits}: version labels must be unique and alphanumeric.')
                for label, value in rows:
                    revision(value)
        if any(name in args.sets for name in ['ctmark', 'spec2017']):
            if require((args.suite / 'CMakeLists.txt').is_file(),
                       f'Fetch llvm-test-suite and set SUITE: {args.suite}.'):
                require(command(['git', '-C', str(args.suite), 'rev-parse', 'HEAD']).returncode == 0,
                        f'SUITE must be a Git checkout: {args.suite}.')
        if 'spec2017' in args.sets:
            require((args.spec / 'version.txt').is_file(),
                    f'Provide licensed SPEC CPU2017 sources and set SPEC2017_ROOT: {args.spec}.')
    if errors:
        raise ValueError('\n'.join(errors))
    print(f'Environment ready; pass CPU {args.cpu}, collector CPU {args.collector_cpu}.')


def main():
    if sys.argv[1:] == ['cpus']:
        print(*cpus())
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--commits', type=Path, required=True)
    parser.add_argument('--input-commit', required=True)
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--spec', type=Path, required=True)
    parser.add_argument('--cpu', type=int, required=True)
    parser.add_argument('--collector-cpu', type=int, required=True)
    parser.add_argument('--jobs', type=int, required=True)
    parser.add_argument('--bpf-clang', required=True)
    parser.add_argument('--criu', required=True)
    parser.add_argument('--client', type=Path, required=True)
    parser.add_argument('--launch', required=True)
    parser.add_argument('--sets', nargs='+', required=True)
    check(parser.parse_args())


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        sys.exit(f'Environment check failed:\n{error}')
