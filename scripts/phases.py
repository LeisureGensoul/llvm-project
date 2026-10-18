#!/usr/bin/env python3
"""Configure phase boundaries and aggregate on-CPU time and RSS in BPF."""
from collections import defaultdict
import json
from pathlib import Path
import re
import shlex
import subprocess


def merge_metrics(total, sample):
    for name, values in sample.items():
        row = total.setdefault(name, dict(cpu_seconds=0.0, calls=0, peak_rss_mib=0.0))
        row['cpu_seconds'] += values['cpu_seconds'] or 0
        row['calls'] += values['calls']
        row['peak_rss_mib'] = max(row['peak_rss_mib'], values['peak_rss_mib'])
        for metric in ('entry_rss_mib', 'peak_rss_increase_mib'):
            if metric in values:
                row[metric] = max(row.get(metric, 0.0), values[metric])


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_list(text):
    return [fields for line in text.splitlines()
            if (fields := shlex.split(line, comments=True))]


def phase_config(text):
    rows = read_list(text)
    require(rows, 'phases.list: expected at least one stage')
    phases, points, ids = [], {}, {}
    for row in rows:
        require(len(row) == 5, 'phases.list: expected name, begin, end, CPU, parent')
        name, begin, end, cpu, within = row
        within = '' if within == '-' else within
        require(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name),
                f'{name}: invalid phase name')
        require(name not in {phase['name'] for phase in phases}, f'{name}: duplicate phase')
        require(begin != end, f'{name}: identical boundaries')
        require(cpu in ('bpf', 'native'), f'{name}: CPU must be bpf or native')
        for boundary in (begin, end):
            function, _, event = boundary.rpartition('@')
            require(function and event in ('entry', 'return'),
                    f'{name}: expected function@entry or function@return')
            if boundary not in ids:
                point = f'p{len(points)}' + ('__return' if event == 'return' else '')
                ids[boundary] = point
                points[point] = dict(function=function, event=event)
        phases.append(dict(name=name, begin=ids[begin], end=ids[end],
                           cpu=cpu, within=within))
    lookup = {phase['name']: phase for phase in phases}
    native = [phase for phase in phases if phase['cpu'] == 'native']
    require(len(native) <= 1 and all(not phase['within'] for phase in native),
            'native CPU covers one complete pass')
    for phase in phases:
        parent, seen = phase['within'], {phase['name']}
        while parent:
            require(parent in lookup and parent not in seen,
                    f'{phase["name"]}: unknown or cyclic parent')
            seen.add(parent)
            parent = lookup[parent]['within']
        phase['depth'] = len(seen) - 1
    return dict(phases=phases, points=points)


def probe_specs(binary, protocol):
    rows = subprocess.check_output(['nm', '--defined-only', binary], text=True)
    symbols = [(parts[0], parts[2]) for row in rows.splitlines()
               if len(parts := row.split()) == 3 and parts[1] in ('t', 'T', 'w', 'W')
               and '.' not in parts[2]]
    names = subprocess.run(['c++filt'], input='\n'.join(symbol for _, symbol in symbols) + '\n',
                           text=True, capture_output=True, check=True).stdout.splitlines()
    resolved = {}
    for function in dict.fromkeys(point['function'] for point in protocol['points'].values()):
        matches = {}
        for (address, symbol), name in zip(symbols, names):
            name = name.replace('(anonymous namespace)::', '')
            if name.split('(', 1)[0] == function or name == function:
                # Constructor aliases at the same address need only one probe.
                matches.setdefault(address, symbol)
        require(len(matches) == 1,
                f'{binary}: {function}: expected one address, found {len(matches)}')
        resolved[function] = next(iter(matches.values()))
    segments = []
    for row in subprocess.check_output(['readelf', '-lW', binary], text=True).splitlines():
        fields = row.split()
        if fields and fields[0] == 'LOAD':
            segments.append(tuple(int(fields[i], 16) for i in (1, 2, 4)))
    addresses = {symbol: int(address, 16) for address, symbol in symbols}
    offsets = {}
    for name, point in protocol['points'].items():
        address = addresses[resolved[point['function']]]
        matches = [offset + address - base for offset, base, size in segments
                   if base <= address < base + size]
        require(len(matches) == 1, f'{binary}: function outside a unique ELF segment')
        offsets[name] = matches[0]
    return offsets


def phase_actions(protocol):
    actions = defaultdict(list)
    for phase in protocol['phases']:
        actions[phase['begin']].append(('begin', phase))
        actions[phase['end']].append(('end', phase))
    for entries in actions.values():
        # Shared boundaries: close children, then open parents before children.
        entries.sort(key=lambda item: (item[0] == 'begin',
                                       item[1]['depth'] if item[0] == 'begin' else
                                       -item[1]['depth']))
    return actions


def probe_manifest(protocol, binaries, compile_cpu):
    require(compile_cpu >= 0, 'invalid compile CPU')
    lines = [f'cpu\t{compile_cpu}']
    for binary in dict.fromkeys(binary.resolve() for binary in binaries):
        require('\t' not in str(binary) and '\n' not in str(binary), 'invalid binary path')
        offsets = probe_specs(str(binary), protocol)
        for index, (point, spec) in enumerate(protocol['points'].items()):
            lines.append(f"{index}\t{int(spec['event'] == 'return')}\t"
                         f"{offsets[point]:x}\t{binary}")
    return '\n'.join(lines) + '\n'


def build_collector(protocol, directory, compiler):
    directory.mkdir(parents=True, exist_ok=True)
    phases = protocol['phases']
    ids = {phase['name']: index for index, phase in enumerate(phases)}
    cpu_count = max(int(part.split('-')[-1]) for part in
                    Path('/sys/devices/system/cpu/possible').read_text().strip().split(',')) + 1
    header = [f'#define PHASE_COUNT {len(phases)}',
              f'#define CPU_COUNT {cpu_count}',
              '#define PHASE_NAMES {' + ', '.join(json.dumps(p['name']) for p in phases) + '}',
              '#define TIMED_PHASES {' + ', '.join(str(int(p['cpu'] == 'bpf')) for p in phases) + '}']
    cases = []
    for point, actions in phase_actions(protocol).items():
        cases.append(f"case {list(protocol['points']).index(point)}:")
        for action, phase in actions:
            index, parent = ids[phase['name']], ids.get(phase['within'], -1)
            suffix = ', total' if action == 'begin' else ''
            cases.append(f'  {action}_phase(s, t, p, {index}, {parent}{suffix});')
        cases.append('  break;')
    header.append('#define PHASE_ACTIONS \\\n' + ' \\\n'.join(cases))
    (directory / 'config.h').write_text('\n'.join(header) + '\n')

    # Match tracepoint layouts on the measurement kernel; task fields use CO-RE.
    layouts = []
    events = {'rss_event': ('kmem/rss_stat', {'curr', 'member', 'size'})}
    for name, (event, wanted) in events.items():
        text = (Path('/sys/kernel/tracing/events') / event / 'format').read_text()
        fields, offset = [], 0
        for field, location, size, signed in re.findall(
                r'field:([^;]+);\s*offset:(\d+);\s*size:(\d+);\s*signed:(\d+);', text):
            field = field.split()[-1]
            if field not in wanted:
                continue
            location, size = int(location), int(size)
            require(size in (4, 8) and location >= offset, f'{event}: unsupported field layout')
            if location > offset:
                fields.append(f'unsigned char pad{offset}[{location - offset}];')
            kind = ('int' if size == 4 else 'long long') if signed == '1' else (
                'unsigned int' if size == 4 else 'unsigned long long')
            fields.append(f'{kind} {field};')
            offset = location + size
            wanted = wanted - {field}
        require(not wanted, f'{event}: missing tracepoint fields {wanted}')
        layouts.append(f'struct {name} {{' + ' '.join(fields) + '};')
    (directory / 'layout.h').write_text('\n'.join(layouts) + '\n')
    source = Path(__file__).resolve().parents[1] / 'collector'
    arch = subprocess.check_output(['gcc', '-dumpmachine'], text=True).strip()
    flags = subprocess.check_output(['pkg-config', '--cflags', 'libbpf'], text=True).split()
    subprocess.run([compiler, '-D__TARGET_ARCH_x86', '-target', 'bpf', '-O2', '-g', '-Wall', '-Werror',
                    f'-I/usr/include/{arch}', '-I' + str(directory), *flags,
                    '-c', str(source / 'collector.bpf.c'),
                    '-o', str(directory / 'collector.bpf.o')], check=True)
    libraries = subprocess.check_output(['pkg-config', '--libs', 'libbpf'], text=True).split()
    subprocess.run(['gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-I' + str(directory),
                    *flags, str(source / 'collector.c'), '-o', str(directory / 'collector'),
                    *libraries], check=True)


def read_metrics(path, protocol):
    return parse_metrics((json.loads(line) for line in path.read_text().splitlines()), protocol, path)


def parse_metrics(events, protocol, path):
    recorded, status = {}, None
    sources = {phase['name']: phase['cpu'] for phase in protocol['phases']}
    for event in events:
        if 'status' in event:
            require(status is None, f'{path}: duplicate capture status')
            status = event['status']
            continue
        name = event['phase']
        require(name in sources and name not in recorded,
                f'{path}: unknown or duplicate phase {name}')
        cpu = event['cpu_ns']
        require((cpu is None) == (sources[name] != 'bpf'),
                f'{path}: {name}: CPU source differs from protocol')
        recorded[name] = dict(cpu_seconds=None if cpu is None else cpu / 1e9,
                              calls=event['calls'], peak_rss_mib=event['peak_rss_bytes'] / 2**20)
        if 'entry_rss_bytes' in event:
            recorded[name].update(entry_rss_mib=event['entry_rss_bytes'] / 2**20,
                                 peak_rss_increase_mib=event['peak_rss_increase_bytes'] / 2**20)
    require(status == [0, 0], f'{path}: capture errors or incomplete boundaries: {status}')
    require(set(recorded) == set(sources), f'{path}: missing phase results')
    for name, values in recorded.items():
        require((values['calls'] > 0) == (values['peak_rss_mib'] > 0),
                f'{path}: {name}: missing RSS or unexpected peak')
        require(values['calls'] or values['cpu_seconds'] in (None, 0),
                f'{path}: {name}: CPU without phase calls')
        if 'entry_rss_mib' in values:
            require(0 <= values['entry_rss_mib'] <= values['peak_rss_mib'] and
                    0 <= values['peak_rss_increase_mib'] <= values['peak_rss_mib'],
                    f'{path}: {name}: invalid RSS entry or increase')
    return recorded


class Collector:
    """Keep probes loaded; dump kernel aggregates after each pass invocation."""

    def __init__(self, probes, log, cpu):
        self.probes, self.log, self.cpu = probes, log, cpu
        self.tracer = self.errors = None

    def _next(self):
        line = self.tracer.stdout.readline()
        require(line, f'collector exited: {self.log}')
        return json.loads(line), line

    def __enter__(self):
        try:
            self.errors = self.log.open('w')
            directory = self.probes.parent
            self.tracer = subprocess.Popen(
                ['taskset', '-c', str(self.cpu), str(directory / 'collector'),
                 str(directory / 'collector.bpf.o'), str(self.probes)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors, text=True)
            event, _ = self._next()
            require(event == {'ready': True}, 'expected probe attachment')
            return self
        except BaseException as error:
            self.__exit__(type(error), None, None)
            raise

    def run(self, operation, metrics):
        result = operation()
        self.tracer.stdin.write('dump\n')
        self.tracer.stdin.flush()
        with metrics.open('w') as output:
            while True:
                event, line = self._next()
                output.write(line)
                if 'status' in event:
                    require(event['status'] == [0, 0],
                            f'{metrics}: capture errors or incomplete boundaries')
                    break
        return result

    def __exit__(self, error_type, *_):
        if self.tracer:
            self.tracer.stdin.close()
            self.tracer.wait()
            self.tracer.stdout.close()
        if self.errors:
            self.errors.close()
        if error_type is None and self.tracer:
            require(self.tracer.returncode == 0, f'collector failed: {self.log}')
