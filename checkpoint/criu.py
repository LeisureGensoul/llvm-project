#!/usr/bin/env python3
"""Manage the CRIU helper and forward checkpoint requests."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time


def request(endpoint, argv):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(str(endpoint))
        connection.sendall((json.dumps(dict(argv=argv, cwd=os.getcwd())) + '\n').encode())
        return json.loads(connection.makefile('rb').readline())


def start(endpoint, argv):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--criu', required=True)
    parser.add_argument('--launch', nargs=argparse.REMAINDER, default=[])
    args = parser.parse_args(argv)
    root = args.root.resolve()
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines()
                  if ':' in line)
    if endpoint.exists():
        try:
            reply = request(endpoint, ['info'])
        except ConnectionRefusedError:
            endpoint.unlink()
        else:
            if reply['returncode'] or reply['root'] != str(root) or reply['criu'] != args.criu:
                raise SystemExit('CRIU helper settings differ; stop the existing helper first')
            return
    if not args.launch and int(status['Seccomp']):
        raise SystemExit('CRIU_LAUNCH: set the host unfiltered launch command in config.sh')
    endpoint.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    helper = Path(__file__).with_name('helper.py')
    command = [*args.launch, sys.executable, str(helper), '--socket', str(endpoint),
               '--root', str(root), '--criu', args.criu, '--caps', status['CapEff'].strip()]
    log = endpoint.with_suffix('.log')
    with log.open('w') as output:
        subprocess.run(command, stdin=subprocess.DEVNULL, stdout=output,
                       stderr=subprocess.STDOUT, check=True)
    reply = request(endpoint, ['info'])
    if reply['returncode'] or reply['root'] != str(root) or reply['criu'] != args.criu:
        raise SystemExit(f'CRIU helper failed to start: {log}')


def stop(endpoint):
    reply = request(endpoint, ['shutdown'])
    if reply['returncode']:
        raise SystemExit(reply['returncode'])
    deadline = time.monotonic() + 5
    while endpoint.exists():
        if time.monotonic() >= deadline:
            raise SystemExit('CRIU helper did not stop')
        time.sleep(.01)

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--socket', required=True)
parser.add_argument('argv', nargs=argparse.REMAINDER)
args = parser.parse_args()
endpoint = Path(args.socket).resolve()
action, *argv = args.argv
if action == 'start':
    start(endpoint, argv)
elif action == 'stop':
    stop(endpoint)
else:
    reply = request(endpoint, args.argv)
    sys.stdout.write(reply.get('stdout', ''))
    sys.stderr.write(reply.get('stderr', ''))
    raise SystemExit(reply['returncode'])
