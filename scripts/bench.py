#!/usr/bin/env python3
"""Prepare, measure and summarize a commit series from shared checkpoints."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
from threading import Event
import time

from checkpoint import (compact_unit, finish_checkpoint, measure_fork, prepare_nolto_batch,
                        prepare_unit, restore_image, write_json)
from phases import (Collector, build_collector, merge_metrics,
                    phase_config, probe_manifest, probe_specs, read_list, read_metrics, require)
from report import (compare, overhead_rows, report, summarize, summarize_input_set,
                    write_csv)


def plan(args):
    root, bench = args.root.resolve(), args.bench.resolve()
    root.mkdir(parents=True, exist_ok=True)
    rows = [row for row in read_list((bench / 'workloads.list').read_text())
            if row[0] == args.input_set]
    require(rows and all(len(row) == 6 for row in rows), 'workloads.list: expected six columns')
    require(all(row[1:3] == [args.workload, args.targets] for row in rows),
            f'{args.input_set}: inconsistent workload or backend')
    catalog = {name: dict(target=target, binary=binary) for _, _, _, name, target, binary in rows}
    require(len(catalog) == len(rows), 'workloads.list: duplicate program')
    selected = catalog if args.programs == ['all'] else {name: catalog[name] for name in args.programs}
    require(args.rounds > 0 and args.jobs > 0 and selected, 'select programs, positive rounds and jobs')
    recipe = dict(compiler=args.input_commit, workload=args.workload, source=args.source_revision,
                  targets=args.targets, cmake_options=args.cmake_option,
                  flags=args.flags, options=args.option)
    commits = {}
    for version, revision in read_list(args.commits.read_text()):
        require(version not in commits and version.replace('_', '').isalnum(), 'invalid or duplicate version')
        commits[version] = subprocess.check_output(
            ['git', '-C', str(args.project), 'rev-parse', revision + '^{commit}'], text=True).strip()
    require(len(commits) >= 2, 'commits.list: select a baseline and at least one change')
    config = dict(commits=commits, bench=str(bench), input_set=args.input_set,
                  project=str(args.project.resolve()), toolroot=str(args.toolroot.resolve()),
                  inputs=str(args.inputs.resolve()), checkpoints=str(args.checkpoints.resolve()),
                  workload=args.workload, profile=args.profile, input_commit=args.input_commit,
                  frontend=str(args.input_bin.resolve()), recipe=recipe, modes=args.modes,
                  programs=selected, rounds=args.rounds, criu=json.loads(args.criu),
                  criu_bin=args.criu_bin, criu_launch=json.loads(args.criu_launch),
                  cpu=args.cpu, collector_cpu=args.collector_cpu, bpf_clang=args.bpf_clang,
                  jobs=args.jobs, phases=phase_config(
                      (bench / 'profiles' / args.profile / 'phases.list').read_text()))
    for mode in config['modes']:
        for program in selected:
            manifest = Path(config['inputs']) / mode / program / 'manifest.json'
            if manifest.exists():
                require(json.loads(manifest.read_text())['recipe'] == recipe,
                        f'{manifest}: input changed; use a new INPUTS directory')
    write_json(root / 'experiment.json', config)


def prepare_runtime(root, names):
    configs = [json.loads((root / name / 'experiment.json').read_text()) for name in names]
    config = configs[0]
    shared = ('profile', 'input_commit', 'frontend', 'toolroot', 'cpu', 'bpf_clang',
              'commits', 'phases', 'criu', 'criu_bin', 'criu_launch')
    require(all(all(item[key] == config[key] for key in shared) for item in configs),
            'runtime: input sets must share the compiler, commits and measurement settings')
    path = Path(config['bench']) / 'profiles' / config['profile'] / 'checkpoint.py'
    spec = importlib.util.spec_from_file_location('adapter', path)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    config['checkpoint'] = adapter.build(config)
    protocol = config['phases']
    directory = root / 'collector'
    build_collector(protocol, directory, config['bpf_clang'])
    probes = deepcopy(protocol)
    aliases = config['checkpoint'].get('aliases', {})
    for point in probes['points'].values():
        point['function'] = aliases.get(point['function'], point['function'])
    binaries = list(dict.fromkeys(config['checkpoint']['plugins'].values()))
    manifest = probe_manifest(probes, [Path(p) for p in binaries], config['cpu'])
    register = dict(points=dict(register=dict(function='llvm_bench_register', event='entry')))
    for path in binaries:
        binary = Path(path)
        offset = probe_specs(str(binary), register)['register']
        manifest += f'999\t0\t{offset:x}\t{binary.resolve()}\n'
    (directory / 'checkpoint.probes').write_text(manifest)
    for name, item in zip(names, configs):
        item['checkpoint'] = config['checkpoint']
        item['collector'] = str(directory)
        write_json(root / name / 'experiment.json', item)


def manage_helper(root, names, action):
    config = json.loads((root / names[0] / 'experiment.json').read_text())
    command = [*config['criu'], action]
    if action == 'start':
        command.extend(['--root', str(Path(config['checkpoints']).parent),
                        '--criu', config['criu_bin'], '--launch', *config['criu_launch']])
    subprocess.run(command, check=True)


def unit_commands(config, mode, program):
    manifest = json.loads((Path(config['inputs']) / mode / program / 'manifest.json').read_text())
    require(manifest['recipe'] == config['recipe'], f'{program}: input settings changed')
    if mode == 'FullLTO':
        require(len(manifest['commands']) == 1, 'checkpoint FullLTO requires one link command')
    return manifest['commands']


def metric_events(data, protocol):
    events = []
    for phase in protocol['phases']:
        row = data[phase['name']]
        events.append(dict(phase=phase['name'], calls=row['calls'],
            cpu_ns=int(row['cpu_seconds'] * 1e9) if phase['cpu'] == 'bpf' else None,
            peak_rss_bytes=int(row['peak_rss_mib'] * 2**20),
            entry_rss_bytes=int(row['entry_rss_mib'] * 2**20),
            peak_rss_increase_bytes=int(row['peak_rss_increase_mib'] * 2**20)))
    return [*events, dict(status=[0, 0])]


def prepare_full(config, programs, commands):
    batch = [(commands[name][0],
              Path(config['checkpoints']) / 'FullLTO' / name / '00000')
             for name in programs]
    cached = [(case / 'checkpoint.json').exists() for _, case in batch]
    began, cancelled = time.monotonic(), Event()
    def prepare(item):
        slot, (command, case) = item
        return prepare_unit(config, command, case, 'FullLTO', slot=slot, cancelled=cancelled)
    pool = ThreadPoolExecutor(max_workers=len(batch))
    try:
        saved = list(pool.map(prepare, enumerate(batch)))
    except BaseException:
        cancelled.set()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return dict(zip(programs, zip(saved, cached))), time.monotonic() - began


def measure_unit(config, protocol, case, collector, active, aggregates, native, overhead):
    versions = list(config['commits'])
    for number in range(1, config['rounds'] + 1):
        for version in versions if number % 2 else versions[::-1]:
            value = collector.run(lambda: measure_fork(config, case, version, active),
                                  active / 'phase-metrics.jsonl')
            key = (number, version)
            merge_metrics(aggregates.setdefault(key, {}), read_metrics(active / 'phase-metrics.jsonl', protocol))
            native[key] = native.get(key, 0.0) + value['cpu_s']
            overhead['fork_s'] += value['fork_s']
            overhead['load_s'] += value['load_s']


def measure(root):
    config = json.loads((root / 'experiment.json').read_text())
    protocol = config['phases']
    require(os.geteuid() == 0, 'measure: root and BPF permissions required')
    command_sets = {mode: {program: unit_commands(config, mode, program)
                           for program in config['programs']} for mode in config['modes']}
    controller_pid = 10000 + (config['jobs'] + 32) * 1024
    require(controller_pid < int(Path('/proc/sys/kernel/pid_max').read_text()),
            'measure: checkpoint input PID range exceeds kernel pid_max')
    Path('/proc/sys/kernel/ns_last_pid').write_text(str(controller_pid))
    active = root / '.active'
    active.mkdir(exist_ok=True)
    overheads = {}
    with (root / 'measurements.jsonl').open('w') as output, \
            Collector(Path(config['collector']) / 'checkpoint.probes', root / 'collector.log', config['collector_cpu']) as collector:
        for mode in config['modes']:
            command_lists = command_sets[mode]
            mode_overhead = dict(preparation_wall_s=0.0, programs={})
            overheads[mode] = mode_overhead
            programs = list(config['programs'])
            group_size = config['jobs'] if mode == 'FullLTO' else 1
            for offset in range(0, len(programs), group_size):
                group = programs[offset:offset + group_size]
                if mode == 'FullLTO':
                    print('FullLTO: prepare ' + ', '.join(group), flush=True)
                    full, elapsed = prepare_full(config, group, command_lists)
                    mode_overhead['preparation_wall_s'] += elapsed
                for program in group:
                    commands = command_lists[program]
                    aggregates, native = {}, {}
                    overhead = dict(save_s=0.0, restore_s=0.0, fork_s=0.0, load_s=0.0,
                                    compact_s=0.0, image_bytes=0, stored_image_bytes=0,
                                    max_trim_released_bytes=0)
                    mode_overhead['programs'][program] = overhead
                    batch_size = config['jobs'] if mode == 'NoLTO' else 1
                    directory = Path(config['checkpoints']) / mode / program
                    for start in range(0, len(commands), batch_size):
                        image = directory / f'{start:05d}'
                        if mode == 'NoLTO':
                            subset = commands[start:start + batch_size]
                            print(f'NoLTO {program}: batch {start + 1}-{start + len(subset)}/{len(commands)}', flush=True)
                            saved, reused, elapsed, restored = prepare_nolto_batch(config, subset, image)
                            units = [(image / 'units' / f'{slot:05d}', unit)
                                     for slot, unit in enumerate(saved['units'])]
                            mode_overhead['preparation_wall_s'] += elapsed
                            overhead['restore_s'] += restored
                        else:
                            saved, reused = full[program]
                            overhead['restore_s'] += restore_image(config, image)
                            units = [(image, saved)]
                        overhead['save_s'] += 0 if reused else saved['save_s']
                        overhead['image_bytes'] += saved['image_bytes']
                        try:
                            for case, unit in units:
                                overhead['max_trim_released_bytes'] = max(overhead['max_trim_released_bytes'],
                                    unit['trim']['rss_before_bytes'] - unit['trim']['rss_after_bytes'])
                                measure_unit(config, protocol, case, collector, active, aggregates, native, overhead)
                        finally:
                            finish_checkpoint(image)
                            compressed = compact_unit(image)
                            overhead['compact_s'] += compressed['compact_s']
                            overhead['stored_image_bytes'] += compressed['stored_image_bytes']
                    for (number, version), data in aggregates.items():
                        record = dict(mode=mode, program=program, round=number, version=version,
                                      native_cpu_seconds=native[(number, version)], metrics=metric_events(data, protocol))
                        output.write(json.dumps(record) + '\n')
                    output.flush()
                    write_json(root / 'overhead.json', overheads)
                    print(f'{mode} {program}: complete; {len(commands)} inputs', flush=True)
    shutil.rmtree(active)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('plan')
    for name in ('root', 'bench', 'inputs', 'checkpoints', 'toolroot', 'input_bin'):
        p.add_argument(name, type=Path)
    for name in ('workload', 'profile', 'input_commit'):
        p.add_argument(name)
    for name in ('rounds', 'cpu', 'collector_cpu', 'jobs'):
        p.add_argument('--' + name.replace('_', '-'), type=int, required=True)
    for name in ('bpf_clang', 'flags', 'input_set', 'criu_bin', 'criu_launch'):
        p.add_argument('--' + name.replace('_', '-'), required=True)
    p.add_argument('--commits', type=Path, required=True)
    p.add_argument('--source-revision', default='')
    p.add_argument('--targets', default='')
    p.add_argument('--cmake-option', action='append', default=[])
    p.add_argument('--option', action='append', default=[])
    p.add_argument('--project', type=Path, required=True)
    p.add_argument('--criu', required=True)
    for name in ('modes', 'programs'):
        p.add_argument('--' + name, nargs='+', required=True)
    for name in ('measure', 'programs'):
        commands.add_parser(name).add_argument('root', type=Path)
    for name in ('runtime', 'summarize', 'helper'):
        action = commands.add_parser(name)
        action.add_argument('root', type=Path)
        action.add_argument('--sets', nargs='+', required=True)
        if name == 'helper':
            action.add_argument('action', choices=('start', 'stop'))
    args = parser.parse_args()
    if args.command == 'plan':
        plan(args)
    elif args.command == 'programs':
        config = json.loads((args.root / 'experiment.json').read_text())
        for name, data in config['programs'].items():
            print(name, data['target'], data['binary'])
    elif args.command == 'runtime':
        prepare_runtime(args.root.resolve(), args.sets)
    elif args.command == 'measure':
        measure(args.root.resolve())
    elif args.command == 'helper':
        manage_helper(args.root.resolve(), args.sets, args.action)
    else:
        summarize(args.root.resolve(), args.sets)
