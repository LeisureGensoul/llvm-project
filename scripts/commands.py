#!/usr/bin/env python3
"""Prepare native dependencies and compiler commands."""
import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess

from checkpoint import write_json
from phases import require


def run(argv, cwd, log):
    with log.open('a') as output:
        output.write(shlex.join(argv) + '\n')
        output.flush()
        result = subprocess.run(argv, cwd=cwd, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f'{shlex.join(argv)}\n{log.read_text()[-4000:]}')


def ninja(build, *argv, tool='ninja'):
    return subprocess.check_output([str(tool), '-C', str(build), *argv], text=True)


def dependencies(text):
    result, active = [], False
    for line in text.splitlines():
        if line.startswith('  input:'):
            active = True
        elif line.startswith('  outputs:'):
            active = False
        elif active and line.startswith('    '):
            result.append(line.strip().lstrip('| '))
    return list(dict.fromkeys(result))


def compiler_command(command):
    argv = shlex.split(command)
    begin = next(i for i, token in enumerate(argv) if Path(token).name in ('clang', 'clang++'))
    return argv[begin:]


def suite_plan(build, target, binary, mode):
    commands = ninja(build, '-t', 'commands', target).splitlines()
    objects = set()
    link = None
    for command in commands:
        for part in command.split(' && '):
            if not re.search(r'(?:^|/)clang(?:\+\+)?\s', part):
                continue
            argv = compiler_command(part)
            if '-o' not in argv:
                continue
            output = argv[argv.index('-o') + 1]
            if '-c' in argv:
                objects.add(output)
            elif output == binary:
                link = argv
    scope = Path(binary).parent
    entries = [entry for entry in json.loads((build / 'compile_commands.json').read_text())
               if entry['output'] in objects and scope in Path(entry['output']).parents
               and Path(entry['file']).suffix.lower() != '.s']
    objects = {entry['output'] for entry in entries}
    require(entries, f'{target}: missing C/C++ compilation commands')
    if mode == 'NoLTO':
        needed = dependencies(ninja(build, '-t', 'query', *sorted(objects)))
    else:
        require(link, f'{target}: missing link command')
        query = ninja(build, '-t', 'query', binary)
        needed = dependencies(query)
        if any(token.startswith('@') for token in link):
            rule = next(line.split(': ', 1)[1] for line in query.splitlines()
                        if line.startswith('  input:'))
            expanded = next(entry['command'] for entry in json.loads(
                ninja(build, '-t', 'compdb', '-x', rule)) if entry['output'] == binary)
            link = compiler_command(next(part for part in expanded.split(' && ')
                                        if re.search(r'(?:^|/)clang(?:\+\+)?\s', part)))
    return entries, link, needed


def commands(config, mode, build, target, binary, log):
    if config['workload'] in ('ctmark', 'spec2017'):
        entries, link, needed = suite_plan(build, target, binary, mode)
        if needed:
            run(['ninja', '-C', str(build), f"-j{config['jobs']}", *needed], build, log)
        return entries, link, build
    directory = Path(config['inputs']) / 'config' / mode
    plan = json.loads((directory / ('compile.json' if mode == 'NoLTO' else 'link.json')).read_text())
    if mode == 'NoLTO':
        selected = set(plan['objects'])
        entries = [entry for entry in json.loads((build / 'compile_commands.json').read_text())
                   if entry['output'] in selected and Path(entry['file']).suffix.lower() != '.s']
        return entries, None, build
    return [], plan['argv'], build


def prepare_inputs(config, mode, program, build, target, binary):
    directory = Path(config['inputs']) / mode / program
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / 'manifest.json'
    if manifest.exists():
        saved = json.loads(manifest.read_text())
        require(saved['recipe'] == config['recipe'],
                f'{directory}: input changed; use a new INPUTS directory')
        return
    entries, link, cwd = commands(config, mode, build, target, binary,
                                  directory / 'prepare.log')
    if mode == 'NoLTO':
        native = [dict(cwd=entry['directory'], argv=compiler_command(
            entry.get('command') or shlex.join(entry['arguments']))) for entry in entries]
    else:
        native = [dict(cwd=str(cwd), argv=link)]
    write_json(manifest, dict(recipe=config['recipe'], commands=native))
    print(f'{mode} {program}: {len(native)} native units', flush=True)


def configure(args):
    binary = args.input_bin
    tools = args.tools
    config = args.results / 'config' / args.mode
    config.mkdir(parents=True, exist_ok=True)
    compile_flags = args.flags
    flags = shlex.quote(f'--ld-path={binary / "ld.lld"}') + ' -Wl,--build-id=none'
    if args.mode == 'NoLTO':
        compile_flags += ' -fno-lto'
        compile_flags += ''.join(f' -mllvm {shlex.quote(o)}' for o in args.llvm_option)
        flags += ' -fno-lto'
    else:
        compile_flags += ' -flto=full'
        flags += (',--lto-O2,--lto-CGO2'
                  + ''.join(f',--mllvm={o}' for o in args.llvm_option))
    if args.mode == 'NoLTO':
        query = args.build / '.cmake/api/v1/query/codemodel-v2'
        query.parent.mkdir(parents=True, exist_ok=True)
        query.touch()
    command = [str(tools / 'cmake'), '-S', str(args.source), '-B', str(args.build),
               '-G', 'Ninja', f'-DCMAKE_MAKE_PROGRAM={tools / "ninja"}',
               f'-DCMAKE_C_COMPILER={binary / "clang"}',
               f'-DCMAKE_CXX_COMPILER={binary / "clang++"}',
               f'-DCMAKE_AR={binary / "llvm-ar"}', f'-DCMAKE_RANLIB={binary / "llvm-ranlib"}',
               '-DCMAKE_BUILD_TYPE=Release',
               '-DCMAKE_C_FLAGS=', '-DCMAKE_CXX_FLAGS=',
               f'-DCMAKE_C_FLAGS_RELEASE={compile_flags}',
               f'-DCMAKE_CXX_FLAGS_RELEASE={compile_flags}',
               '-DCMAKE_C_COMPILER_LAUNCHER=', '-DCMAKE_CXX_COMPILER_LAUNCHER=',
               f'-DCMAKE_EXE_LINKER_FLAGS={flags}',
               '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON']
    if args.mode == 'NoLTO':
        command.append('-DCMAKE_ADD_CUSTOM_COMMAND_DEPENDS_EXPLICIT_ONLY=ON')
    command += ['-DLLVM_ENABLE_ASSERTIONS=OFF',
               '-DLLVM_ENABLE_LTO=' + ('Full' if args.mode == 'FullLTO' else 'OFF'),
               f'-DLLVM_ENABLE_PROJECTS={args.project}',
               f'-DLLVM_TARGETS_TO_BUILD={args.llvm_targets}',
               '-DLLVM_BUILD_LLVM_DYLIB=OFF', '-DLLVM_LINK_LLVM_DYLIB=OFF',
               '-DLLVM_INCLUDE_TESTS=OFF',
               '-DLLVM_INCLUDE_BENCHMARKS=OFF', '-DLLVM_INCLUDE_EXAMPLES=OFF',
               f'-DLLVM_FORCE_VC_REVISION={args.source_commit}',
               f'-DLLVM_TABLEGEN={binary / "llvm-tblgen"}']
    if args.project == 'clang':
        command += ['-DCLANG_LINK_CLANG_DYLIB=OFF',
                    f'-DCLANG_TABLEGEN={binary / "clang-tblgen"}']
    command += args.cmake_option
    with (config / 'configure.log').open('w') as output:
        subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, check=True)
    (config / 'configure-command.txt').write_text(shlex.join(command) + '\n')


def cmake_targets(args):
    reply = args.build / '.cmake/api/v1/reply'
    index = json.loads(max(reply.glob('index-*.json')).read_text())
    model = json.loads((reply / index['reply']['codemodel-v2']['jsonFile']).read_text())
    targets = {t['id']: t for t in model['configurations'][0]['targets']}
    return {key: json.loads((reply / target['jsonFile']).read_text())
            for key, target in targets.items()}


def compile_plan(args):
    targets = cmake_targets(args)
    target = next(key for key, info in targets.items() if info['name'] == args.project)
    closure = set()

    def visit(key):
        if key in closure:
            return
        closure.add(key)
        if key != target and targets[key]['type'] in ('EXECUTABLE', 'UTILITY'):
            return
        for dependency in targets[key].get('dependencies', []):
            visit(dependency['id'])

    visit(target)
    generators = {key for key in closure
                  if targets[key]['type'] == 'EXECUTABLE' and key != target}
    directories = {(args.build / info['paths']['build'] / f'CMakeFiles/{info["name"]}.dir').resolve()
                   for key, info in targets.items() if key in closure - generators}
    objects, precompiled_headers = [], []
    for entry in json.loads((args.build / 'compile_commands.json').read_text()):
        output = Path(entry['output'])
        path = (args.build / output).resolve()
        if path.suffix in ('.pch', '.gch') or not any(parent in directories for parent in path.parents):
            continue
        objects.append(str(output))
        argv = entry.get('arguments') or shlex.split(entry['command'])
        for index, token in enumerate(argv):
            if token == '-include-pch':
                header = next(arg for arg in argv[index + 1:] if arg != '-Xclang')
                path = Path(header)
                precompiled_headers.append(str(path.relative_to(args.build) if path.is_absolute() else path))
    require(objects, f'{args.project}: missing compilation objects')
    dependencies = [info['name'] for key, info in targets.items()
                    if key in closure and (info['type'] == 'UTILITY' or key in generators)]
    dependencies.extend(dict.fromkeys(precompiled_headers))
    outputs = {line.rsplit(': ', 1)[0] for line in
               ninja(args.build, '-t', 'targets', 'all', tool=args.tools / 'ninja').splitlines()}
    for key in closure - generators:
        for source in targets[key].get('sources', []):
            path = Path(source['path'])
            if source.get('isGenerated') and path.suffix not in ('.rule', '.o', '.a', '.so'):
                name = str(path.relative_to(args.build) if path.is_absolute() and path.is_relative_to(args.build) else path)
                if name in outputs:
                    dependencies.append(name)
    dependencies = list(dict.fromkeys(dependencies))
    return dict(objects=objects, dependencies=dependencies)


def ninja_prerequisites(args, objects):
    needed = []
    for begin in range(0, len(objects), 2048):
        text = ninja(args.build, '-t', 'query', *objects[begin:begin + 2048],
                     tool=args.tools / 'ninja')
        active = False
        for line in text.splitlines():
            if line.startswith('  input:'):
                active = True
            elif line.startswith('  outputs:'):
                active = False
            elif active and line.startswith('    '):
                name = line.strip().lstrip('| ')
                path = Path(name)
                if path.suffix in ('.o', '.a', '.so'):
                    continue
                if not line.startswith('    || ') and (path.suffix in ('.pch', '.gch') or not (args.build / path).exists()):
                    needed.append(name)
    return list(dict.fromkeys(needed))


def prepare_project(args):
    configure(args)
    binary = args.input_bin
    config = args.results / 'config' / args.mode
    if args.mode == 'NoLTO':
        plan = compile_plan(args)
        plan['dependencies'] = list(dict.fromkeys(
            plan['dependencies'] + ninja_prerequisites(args, plan['objects'])))
        with (config / 'prepare.log').open('w') as output:
            if plan['dependencies']:
                subprocess.run([str(args.tools / 'cmake'), '--build', str(args.build),
                                f'-j{args.jobs}', '--target', *plan['dependencies']],
                               stdout=output, stderr=subprocess.STDOUT, check=True)
        (config / 'compile.json').write_text(json.dumps(plan, indent=2) + '\n')
        return
    builder = args.tools / 'ninja'
    targets = [line.rsplit(': ', 1) for line in ninja(args.build, '-t', 'targets', 'all', tool=builder).splitlines()]
    matches = [(target, rule) for target, rule in targets
               if rule.startswith(f'CXX_EXECUTABLE_LINKER__{args.project}_')]
    require(len(matches) == 1, f'{args.project}: expected one executable link target')
    target, rule = matches[0]
    edges = ninja(args.build, '-t', 'query', target, tool=builder)
    needed = dependencies(edges)
    require(needed, f'{args.project}: missing link dependencies')
    with (config / 'prepare.log').open('w') as output:
        subprocess.run([str(builder), '-C', str(args.build), f'-j{args.jobs}',
                        *needed], stdout=output, stderr=subprocess.STDOUT, check=True)
    entries = json.loads(ninja(args.build, '-t', 'compdb', '-x', rule, tool=builder))
    entry = next(item for item in entries if item['output'] == target)
    parts = entry['command'].split(' && ')
    links = []
    for part in parts:
        argv = shlex.split(part)
        if argv and argv[0] == str(binary / 'clang++'):
            links.append(argv)
    require(len(links) == 1, f'{args.project}: expected one compiler link command')
    argv = links[0]
    argv = [re.sub(r'\\+\$', '$', arg) for arg in argv]
    require('-flto=full' in argv, f'{args.project}: FullLTO link flag missing')
    (config / 'link.json').write_text(json.dumps(
        dict(argv=argv), indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    project = actions.add_parser('project')
    project.add_argument('--build', type=Path, required=True)
    project.add_argument('--tools', type=Path, required=True)
    project.add_argument('--input-bin', type=Path, required=True)
    project.add_argument('--source', type=Path, required=True)
    project.add_argument('--source-commit', required=True)
    project.add_argument('--project', choices=('clang', 'lld'), required=True)
    project.add_argument('--llvm-targets', default='X86')
    project.add_argument('--flags', required=True)
    project.add_argument('--jobs', type=int, default=8)
    project.add_argument('--llvm-option', action='append', default=[])
    project.add_argument('--cmake-option', action='append', default=[])
    project.add_argument('--mode', choices=('NoLTO', 'FullLTO'), required=True)
    project.add_argument('--results', type=Path, required=True)
    inputs = actions.add_parser('inputs')
    inputs.add_argument('config', type=Path)
    inputs.add_argument('mode', choices=('NoLTO', 'FullLTO'))
    inputs.add_argument('program')
    inputs.add_argument('build', type=Path)
    inputs.add_argument('target')
    inputs.add_argument('binary')
    args = parser.parse_args()
    if args.action == 'project':
        prepare_project(args)
    else:
        prepare_inputs(json.loads(args.config.read_text()), args.mode, args.program,
                       args.build, args.target, args.binary)
