#!/usr/bin/env python3
"""Build the final commit and run the profile's selected correctness tests."""
import argparse
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

from phases import read_list


def check(args):
    PROJECT, SOURCE, BUILD, TOOLS, RESULTS = (args.project, args.source, args.build,
                                            args.tools, args.results)
    targets, jobs, build_targets = args.targets, str(args.jobs), args.build_targets
    unit_tests, lit_patterns = [], []
    for row in read_list(args.tests.read_text()):
        if row[0] == 'unit' and len(row) == 3:
            unit_tests.append(row[1:])
        elif row[0] == 'lit' and len(row) == 2:
            lit_patterns.append(row[1])
        else:
            sys.exit(f'tests.list: invalid entry {row}')
    if not unit_tests and not lit_patterns:
        print('correctness: no tests selected', flush=True)
        return

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


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('project', 'source', 'build', 'tools', 'results', 'tests'):
        parser.add_argument(name, type=Path)
    parser.add_argument('targets')
    parser.add_argument('jobs', type=int)
    parser.add_argument('build_targets', nargs='+')
    check(parser.parse_args())
