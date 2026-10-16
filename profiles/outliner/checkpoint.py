"""Build an immutable checkpoint host and private Outliner implementations."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess


def fork_support(body):
    body = body.replace('static ThreadPoolExecutor *getDefaultExecutor() {',
                        'static ThreadPoolExecutor *BenchExecutor = nullptr;\n\n'
                        'static ThreadPoolExecutor *getDefaultExecutor() {')
    body = body.replace('static ThreadPoolExecutor Exec(strategy);\n  return &Exec;',
                        'static ThreadPoolExecutor Exec(strategy);\n'
                        '  static const bool Registered = (BenchExecutor = &Exec, true);\n'
                        '  (void)Registered;\n  return &Exec;')
    return body + '''
extern "C" void llvm_bench_prepare_fork() {
#if LLVM_ENABLE_THREADS
  if (BenchExecutor)
    BenchExecutor->stop();
#endif
  llvm::parallel::strategy.ThreadsRequested = 1;
}
'''


def build(config):
    bench = Path(config['bench'])
    base = Path(config['frontend']).parent
    project = Path(config['project'])
    replaced = {'llvm/lib/CodeGen/MachineOutliner.cpp',
                'llvm/lib/Support/SuffixTree.cpp', 'llvm/lib/Support/SuffixTreeNode.cpp',
                'llvm/include/llvm/Support/SuffixTree.h', 'llvm/include/llvm/Support/SuffixTreeNode.h'}
    for commit in set(config['commits'].values()):
        changed = subprocess.check_output(['git', '-C', str(project), 'diff', '--name-only',
            config['input_commit'], commit, '--', 'llvm/lib', 'llvm/include',
            'clang/lib', 'clang/include', 'lld'], text=True).splitlines()
        outside = sorted(set(changed) - replaced)
        if outside:
            raise ValueError('checkpoint adapter does not load these changes: ' + ', '.join(outside))
    entries = json.loads((base / 'compile_commands.json').read_text())
    runtime = bench / 'checkpoint/runtime.h'
    prelude = '#include "' + str(runtime) + '"\n' + '''
struct BenchOutlinerConfig {
  unsigned Mode;
  bool EnableLinkOnceODROutlining;
  unsigned OutlinerReruns;
  unsigned OutlinerBenefitThreshold;
  bool OutlinerLeafDescendants;
  bool DisableGlobalOutlining;
  bool AppendContentHashToOutlinedName;
};
'''
    fingerprint = hashlib.sha256(runtime.read_bytes() +
                                 Path(__file__).read_bytes()).hexdigest()[:16]
    work = Path(config['toolroot']) / 'checkpoint' / config['input_commit'] / fingerprint
    work.mkdir(parents=True, exist_ok=True)

    def original(commit, path):
        return subprocess.check_output(['git', '-C', str(project), 'show',
                                        commit + ':llvm/' + path], text=True)

    def run(args, log):
        with log.open('w') as output:
            subprocess.run(list(map(str, args)), cwd=base, stdout=output,
                           stderr=subprocess.STDOUT, check=True)

    def compile_source(folder, name, kind, body, private=False):
        source, output = folder / (name + '.cpp'), folder / (name + '.cpp.o')
        source.write_text(body)
        entry = next(x for x in entries if x['file'].endswith(kind))
        args = entry.get('arguments') or shlex.split(entry['command'])
        args = list(args)
        args[args.index('-o') + 1] = str(output)
        args[-1] = str(source)
        args[1:1] = ['-fPIC', '-I' + str(folder / 'include')]
        if private:
            args[1:1] = ['-fvisibility=hidden', '-DLLVM_BENCH_PLUGIN']
        run(args, folder / (name + '.log'))
        return output

    host = work / 'host'
    if not (host / 'ready.json').exists():
        host.mkdir(exist_ok=True)
        body = original(config['input_commit'], 'lib/CodeGen/MachineOutliner.cpp')
        body = prelude + body
        body = body.replace('bool runOnModule(Module &M) override;',
                            'bool runOnModule(Module &M) override;\n  bool runNative(Module &M);')
        body = body.replace('bool MachineOutliner::runOnModule(Module &M) {',
                            'bool MachineOutliner::runNative(Module &M) {')
        body += '''
bool MachineOutliner::runOnModule(Module &M) {
  BenchOutlinerConfig Config{static_cast<unsigned>(RunOutlinerMode),
    EnableLinkOnceODROutlining, OutlinerReruns, OutlinerBenefitThreshold,
    OutlinerLeafDescendants, DisableGlobalOutlining,
    AppendContentHashToOutlinedName};
  return benchRunPass(this, M, Config, [&] { return runNative(M); });
}
'''
        obj = compile_source(host, 'MachineOutliner', '/lib/CodeGen/MachineOutliner.cpp', body)
        parallel = compile_source(host, 'Parallel', '/lib/Support/Parallel.cpp',
                                  fork_support(original(config['input_commit'], 'lib/Support/Parallel.cpp')))
        archives = {}
        for name, object_file in [('LLVMCodeGen', obj), ('LLVMSupport', parallel)]:
            source = 'lib/lib' + name + '.a'
            archive = host / Path(source).name
            shutil.copyfile(base / source, archive)
            run(['ar', 'r', archive, object_file], host / (name + '-archive.log'))
            archives[source] = str(archive)
        text = subprocess.check_output([str(Path(config['toolroot']) / 'bin/ninja'), '-C', str(base), '-t', 'commands',
                                        'clang', 'lld'], text=True)
        links = {}
        for line in text.splitlines():
            for part in line.split(' && '):
                argv = shlex.split(part)
                if '-o' not in argv or not any(x.endswith('libLLVMCodeGen.a') for x in argv):
                    continue
                target = argv[argv.index('-o') + 1]
                if target.startswith('bin/clang-') and target != 'bin/clang-tblgen':
                    links['clang'] = argv
                elif target == 'bin/lld':
                    links['lld'] = argv
        (host / 'bin').mkdir(exist_ok=True)
        for tool in ('clang', 'lld'):
            args = links[tool]
            args[args.index('-o') + 1] = str(host / 'bin' / tool)
            args = [archives.get(x, x) for x in args]
            args.insert(1, '-Wl,--export-dynamic')
            run(args, host / (tool + '.log'))
        for alias, tool in [('clang++', 'clang'), ('ld.lld', 'lld')]:
            (host / 'bin' / alias).symlink_to(tool)
        (host / 'ready.json').write_text(json.dumps(dict(compiler=config['input_commit'])))

    def private_tree(text):
        return re.sub(r'\bSuffixTree([A-Za-z0-9_]*)\b', r'BenchSuffixTree\1', text)

    def plugin(version):
        commit = config['commits'][version]
        folder = work / commit
        output = folder / 'outliner.so'
        if output.exists():
            return str(output)
        include = folder / 'include/llvm/Support'
        include.mkdir(parents=True, exist_ok=True)
        for name in ('SuffixTree.h', 'SuffixTreeNode.h'):
            (include / ('Bench' + name)).write_text(private_tree(
                original(commit, 'include/llvm/Support/' + name)))
        body = private_tree(original(commit, 'lib/CodeGen/MachineOutliner.cpp'))
        body = prelude + body
        options = []
        def option(match):
            kind, name = match.group(1), match.group(2)
            options.append(name)
            return 'static ' + kind + ' ' + name + ';'
        body = re.sub(r'static cl::opt<(bool|unsigned)>\s+(\w+)\(.*?\);', option, body, flags=re.S)
        body = body.replace('bool MachineOutliner::runOnModule(',
                            '__attribute__((noinline, noclone)) bool MachineOutliner::runOnModule(')
        body = body.replace('bool MachineOutliner::outline(',
                            '__attribute__((noinline, noclone)) bool MachineOutliner::outline(')
        body = body.replace('void MachineOutliner::populateMapper(',
                            '__attribute__((noinline, noclone)) void MachineOutliner::populateMapper(')
        start = body.index('ModulePass *llvm::createMachineOutlinerPass(')
        end = body.index('void MachineOutliner::emitNotOutliningCheaperRemark(', start)
        body = body[:start] + body[end:]
        body += '\nextern "C" __attribute__((visibility("default")))\n'
        body += 'bool llvm_bench_run_pass(llvm::Pass *Host, llvm::Module *M, const BenchOutlinerConfig *Config, double *CPU) {\n'
        body += ''.join('  ' + name + ' = Config->' + name + ';\n' for name in options)
        body += '''  MachineOutliner Instance;
  Instance.RunOutlinerMode = static_cast<RunOutliner>(Config->Mode);
  Instance.setResolver(new AnalysisResolver(*Host->getResolver()));
  double Start = benchCPU();
  bool Changed = Instance.runOnModule(*M);
  *CPU = benchCPU() - Start;
  return Changed;
}
'''
        objects = [compile_source(folder, 'MachineOutliner',
                                   '/lib/CodeGen/MachineOutliner.cpp', body, True)]
        for name in ('SuffixTree', 'SuffixTreeNode'):
            objects.append(compile_source(folder, name, '/lib/Support/' + name + '.cpp',
                private_tree(original(commit, 'lib/Support/' + name + '.cpp')), True))
        run(['g++', '-shared', '-Wl,-Bsymbolic', *objects, '-o', output], folder / 'link.log')
        return str(output)

    representatives = {commit: version for version, commit in config['commits'].items()}
    with ThreadPoolExecutor(max_workers=2) as pool:
        built = dict(zip(representatives, pool.map(plugin, representatives.values())))
    libraries = {version: built[commit] for version, commit in config['commits'].items()}
    return dict(host=str(host / 'bin'), plugins=libraries,
                aliases={'llvm::SuffixTree::SuffixTree': 'llvm::BenchSuffixTree::BenchSuffixTree'})
