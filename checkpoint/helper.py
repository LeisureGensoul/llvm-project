#!/usr/bin/env python3
"""Serve CRIU requests from a host-authorized unfiltered process."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import socket
import struct
import subprocess


def limit_caps(mask):
    class Header(ctypes.Structure):
        _fields_ = [('version', ctypes.c_uint32), ('pid', ctypes.c_int)]

    class Data(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint32) for name in ('effective', 'permitted', 'inheritable')]

    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines()
                  if ':' in line)
    if os.geteuid() or int(status['Seccomp']):
        raise SystemExit('CRIU helper requires a host-authorized unfiltered root launch')
    libc = ctypes.CDLL(None, use_errno=True)
    bound = int(status['CapBnd'], 16)
    for bit in range(int(Path('/proc/sys/kernel/cap_last_cap').read_text()) + 1):
        if bound & (1 << bit) and not mask & (1 << bit):
            if libc.prctl(24, bit, 0, 0, 0):
                raise OSError(ctypes.get_errno(), 'cannot restrict capability bounds')
    caps = (Data * 2)(Data(mask & 0xffffffff, mask & 0xffffffff, 0),
                      Data(mask >> 32, mask >> 32, 0))
    if libc.capset(ctypes.byref(Header(0x20080522, 0)), caps) or libc.prctl(38, 1, 0, 0, 0):
        raise OSError(ctypes.get_errno(), 'cannot restrict helper privileges')


def serve(args):
    endpoint, root = args.socket.resolve(), args.root.resolve()
    limit_caps(int(args.caps, 16))
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(endpoint))
    os.chmod(endpoint, 0o600)
    server.listen(4)
    if os.fork():
        server.close()
        return
    os.setsid()
    with open(os.devnull, 'rb') as input_stream, open(endpoint.with_suffix('.log'), 'a') as output:
        os.dup2(input_stream.fileno(), 0)
        os.dup2(output.fileno(), 1)
        os.dup2(output.fileno(), 2)
    try:
        while True:
            peer, _ = server.accept()
            with peer:
                shutdown = False
                try:
                    pid, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid or os.readlink(f'/proc/{pid}/ns/net') != os.readlink('/proc/self/ns/net') or \
                            Path(f'/proc/{pid}/cgroup').read_text() != Path('/proc/self/cgroup').read_text():
                        raise ValueError('CRIU request must come from the same root environment')
                    request = json.loads(peer.makefile('rb').readline(65536))
                    argv = request['argv']
                    if argv == ['info']:
                        status = dict(line.split(':', 1)
                                      for line in Path('/proc/self/status').read_text().splitlines()
                                      if ':' in line)
                        reply = dict(returncode=0, root=str(root), criu=args.criu, pid=os.getpid(),
                                     seccomp=int(status['Seccomp']))
                    elif argv == ['shutdown']:
                        reply, shutdown = dict(returncode=0), True
                    else:
                        cwd = Path(request['cwd']).resolve()
                        if not cwd.is_relative_to(root) or not argv or argv[0] not in ('check', 'dump', 'restore'):
                            raise ValueError('CRIU request outside the configured checkpoint scope')
                        command = ['nsenter', '-t', str(pid), '-p', '-m', '--', 'sh', '-c',
                                   'cd "$1" && shift && exec "$@"', 'sh', str(cwd), args.criu, *argv]
                        result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                                capture_output=True, timeout=120)
                        reply = dict(returncode=result.returncode,
                                     stdout=result.stdout.decode(errors='replace'),
                                     stderr=result.stderr.decode(errors='replace'))
                except Exception as error:
                    reply = dict(returncode=1, stderr=str(error))
                peer.sendall((json.dumps(reply) + '\n').encode())
                if shutdown:
                    break
    finally:
        server.close()
        endpoint.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--criu', required=True)
    parser.add_argument('--caps', required=True)
    serve(parser.parse_args())
