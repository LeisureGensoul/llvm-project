"""Save, restore and compress pre-pass process checkpoints."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from threading import Lock
import time

from phases import require

launch_lock = Lock()


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def choice(case, action, library='', output='', cpu=0):
    message = f'{action}\n{library}\n{output}\n{cpu}\n'
    temporary = case / 'choice.tmp'
    temporary.write_text(message)
    temporary.replace(case / 'choice')


def helper(config, argv, case, log):
    start = time.monotonic()
    with log.open('w') as stream:
        subprocess.run([*config['criu'], *map(str, argv)], cwd=case,
                       stdout=stream, stderr=subprocess.STDOUT, check=True)
    return time.monotonic() - start


def native_command(config, command, case, mode):
    argv = list(command['argv'])
    argv[0] = str(Path(config['frontend']) / Path(argv[0]).name)
    output = case / ('native.o' if mode == 'NoLTO' else 'native.bin')
    argv[argv.index('-o') + 1] = str(output)
    if mode == 'FullLTO':
        argv.append('-Wl,--lto-partitions=1')
    plan = subprocess.run([*argv, '-###'], cwd=command['cwd'], capture_output=True,
                          text=True, check=True)
    commands = [shlex.split(line) for line in plan.stderr.splitlines() if line.startswith(' "')]
    raw = next(a for a in commands if '-cc1' in a or a[0].endswith('/ld.lld'))
    raw[0] = str(Path(config['checkpoint']['host']) /
                 ('clang' if mode == 'NoLTO' else 'ld.lld'))
    return raw


def restore_image(config, case):
    (case / 'choice').unlink(missing_ok=True)
    (case / 'done').unlink(missing_ok=True)
    for source in (case / 'files').rglob('*'):
        if source.is_file():
            shutil.copy2(source, case / source.relative_to(case / 'files'))
    return helper(config, ['restore', '--unprivileged', '--shell-job', '--restore-detached',
                           '-D', case / 'images', '-o', 'restore.log'],
                  case, case / 'restore-command.log')


def unpack_unit(case):
    for archive in (case / 'images').glob('pages-*.img.zst'):
        image = archive.with_suffix('')
        if not image.exists():
            subprocess.run(['zstd', '-dq', str(archive), '-o', str(image)], check=True)


def save_temporary_files(case, pids):
    files = case / 'files'
    files.mkdir()
    for pid in pids:
        for descriptor in Path(f'/proc/{pid}/fd').iterdir():
            try:
                target = Path(os.readlink(descriptor))
            except FileNotFoundError:
                continue
            if target.is_relative_to(case) and target.name.endswith('.tmp') and target.is_file():
                destination = files / target.relative_to(case)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, destination)


def prepare_unit(config, command, case, mode, slot=0, cancelled=None):
    expected = dict(host=config['checkpoint']['host'], command=command, mode=mode,
                    recipe=config['recipe'])
    manifest = case / 'checkpoint.json'
    if mode == 'FullLTO' and manifest.exists():
        saved = json.loads(manifest.read_text())
        require(saved['input'] == expected,
                f'{case}: input changed; use a new CHECKPOINTS directory')
        unpack_unit(case)
        return saved
    if case.exists():
        shutil.rmtree(case)
    case.mkdir(parents=True, exist_ok=True)
    raw = native_command(config, command, case, mode)
    env = dict(os.environ, LLVM_BENCH_CHECKPOINT=str(case))
    started = time.monotonic()
    # Keep compiler PIDs below the controller range, including parallel prefixes.
    with launch_lock, (case / 'compile.log').open('w') as log:
        Path('/proc/sys/kernel/ns_last_pid').write_text(str(9999 + slot * 1024))
        process = subprocess.Popen(raw, cwd=command['cwd'], env=env,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
    try:
        while not (case / 'ready').exists():
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError('checkpoint preparation cancelled')
            require(process.poll() is None, f'{case}: compilation failed before checkpoint')
            time.sleep(.01)
        pid = int((case / 'ready').read_text())
        preparation = time.monotonic() - started
        trim = json.loads((case / 'trim.json').read_text())
        require(len(list(Path(f'/proc/{pid}/task').iterdir())) == 1,
                f'{case}: checkpoint must contain one compiler thread')
        seconds = 0.0
        if mode == 'FullLTO':
            images = case / 'images'
            images.mkdir()
            save_temporary_files(case, [pid])
            seconds = helper(config, ['dump', '--unprivileged', '--shell-job',
                                      '-t', pid, '-D', images, '-o', 'dump.log'],
                             case, case / 'dump-command.log')
            process.wait()
        saved = dict(input=expected, pid=pid, preparation_s=preparation, save_s=seconds,
                     trim=trim,
                     image_bytes=sum(p.stat().st_size for p in images.glob('*.img'))
                     if mode == 'FullLTO' else 0)
        write_json(manifest, saved)
        return saved
    finally:
        if process.poll() is None and not (mode == 'NoLTO' and manifest.exists()):
            os.killpg(process.pid, 9)
            process.wait()


def measure_fork(config, case, version, directory):
    (case / 'done').unlink(missing_ok=True)
    choice(case, 'measure', config['checkpoint']['plugins'][version],
           str(directory), config['cpu'])
    while not (case / 'done').exists():
        time.sleep(.001)
    status = int((case / 'done').read_text())
    require(os.waitstatus_to_exitcode(status) == 0, f'{case}: forked pass failed')
    return json.loads((directory / 'native.json').read_text())


def compact_unit(case):
    started = time.monotonic()
    for image in (case / 'images').glob('pages-*.img'):
        archive = image.with_suffix(image.suffix + '.zst')
        if not archive.exists():
            temporary = archive.with_suffix('.tmp')
            subprocess.run(['zstd', '-q', '-1', '-T2', str(image), '-o', str(temporary)], check=True)
            temporary.replace(archive)
        image.unlink()
    return dict(compact_s=time.monotonic() - started,
                stored_image_bytes=sum(p.stat().st_size for p in (case / 'images').iterdir()))


def batch_worker(case):
    job = json.loads((case / 'batch.json').read_text())
    config, commands = job['config'], job['commands']
    def prepare(item):
        slot, command = item
        return prepare_unit(config, command, case / 'units' / f'{slot:05d}',
                            'NoLTO', slot=slot)
    with ThreadPoolExecutor(max_workers=config['jobs']) as pool:
        units = list(pool.map(prepare, enumerate(commands)))
    write_json(case / 'ready.json', dict(pid=os.getpid(), units=units))
    while not (case / 'choice').exists():
        time.sleep(.01)
    require((case / 'choice').read_text().splitlines()[0] == 'finish',
            f'{case}: unknown batch action')
    for slot, unit in enumerate(units):
        choice(case / 'units' / f'{slot:05d}', 'finish')
        _, status = os.waitpid(unit['pid'], 0)
        require(os.waitstatus_to_exitcode(status) == 0, f'{case}: parent failed')


def start_batch(config, commands, case):
    case.mkdir(parents=True)
    write_json(case / 'batch.json', dict(config=config, commands=commands))
    started = time.monotonic()
    Path('/proc/sys/kernel/ns_last_pid').write_text('8999')
    with (case / 'prepare.log').open('w') as log:
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(case)],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
    while not (case / 'ready.json').exists():
        require(process.poll() is None, f'{case}: batch preparation failed')
        time.sleep(.01)
    saved = json.loads((case / 'ready.json').read_text())
    saved['preparation_s'] = time.monotonic() - started
    return saved


def save_batch(config, case, saved):
    (case / 'images').mkdir()
    save_temporary_files(case, [unit['pid'] for unit in saved['units']])
    saved['save_s'] = helper(config, ['dump', '--unprivileged', '--shell-job',
                                    '-t', saved['pid'], '-D', case / 'images', '-o', 'dump.log'],
                             case, case / 'dump-command.log')
    os.waitpid(saved['pid'], 0)
    for unit in saved['units']:
        os.waitpid(unit['pid'], 0)
    saved['image_bytes'] = sum(p.stat().st_size for p in (case / 'images').glob('*.img'))
    write_json(case / 'checkpoint.json', saved)
    return saved


def finish_checkpoint(case):
    pid = json.loads((case / 'checkpoint.json').read_text())['pid']
    choice(case, 'finish')
    _, status = os.waitpid(pid, 0)
    require(os.waitstatus_to_exitcode(status) == 0, f'{case}: checkpoint parent failed')


def prepare_nolto_batch(config, commands, case):
    expected = dict(host=config['checkpoint']['host'], commands=commands, recipe=config['recipe'])
    started = time.monotonic()
    manifest = case / 'checkpoint.json'
    reused = manifest.exists()
    if reused:
        saved = json.loads(manifest.read_text())
        require(saved['input'] == expected, f'{case}: input changed; use a new CHECKPOINTS directory')
        unpack_unit(case)
    else:
        if case.exists():
            shutil.rmtree(case)
        saved = start_batch(config, commands, case)
        saved['input'] = expected
        save_batch(config, case, saved)
    seconds = restore_image(config, case)
    return saved, reused, time.monotonic() - started - seconds, seconds


if __name__ == '__main__':
    batch_worker(Path(sys.argv[1]))
