#!/usr/bin/env python3
"""Root-only Linux packet checks in disposable network namespaces; no real modem."""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).parents[1]))
import upload_gate as g


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=15, **kwargs)


def main():
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise SystemExit('Run with sudo on Linux. This test does not run on Windows.')
    if not all(shutil.which(name) for name in ('ip', 'nft')):
        raise SystemExit('Install iproute2 and nftables first.')
    suffix = secrets.token_hex(4)
    router, receiver, camera = [f'rug-{suffix}-{x}' for x in ('r', 's', 'c')]
    namespaces, processes = [], []
    def ns(name, *args, **kwargs):
        return run('ip', 'netns', 'exec', name, *args, **kwargs)
    probe = '''import os,socket,sys
os.setgroups([]); os.setgid(int(sys.argv[2])); os.setuid(int(sys.argv[2]))
try:
 s=socket.create_connection(('10.201.0.2',8554),timeout=1)
 s.sendall(b'probe'); s.close()
except OSError: sys.exit(2)
'''
    try:
        for name in (router, receiver, camera):
            run('ip', 'netns', 'add', name)
            namespaces.append(name)
            ns(name, 'ip', 'link', 'set', 'lo', 'up')
        for a, b, remote, addr_r, addr_b in (
                ('cell0', 'peer0', receiver, '10.201.0.1/24', '10.201.0.2/24'),
                ('wifi0', 'peer1', camera, '10.42.0.1/24', '10.42.0.20/24')):
            ns(router, 'ip', 'link', 'add', a, 'type', 'veth', 'peer', 'name', b)
            ns(router, 'ip', 'link', 'set', b, 'netns', remote)
            ns(router, 'ip', 'addr', 'add', addr_r, 'dev', a)
            ns(router, 'ip', 'link', 'set', a, 'up')
            ns(remote, 'ip', 'addr', 'add', addr_b, 'dev', b)
            ns(remote, 'ip', 'link', 'set', b, 'up')
        ns(camera, 'ip', 'route', 'add', 'default', 'via', '10.42.0.1')
        ns(receiver, 'ip', 'route', 'add', '10.42.0.0/24', 'via', '10.201.0.1')
        ns(router, 'sysctl', '-q', '-w', 'net.ipv4.ip_forward=1')
        c = g.load(Path(__file__).parents[1] / 'config/upload-gate.example.json')
        c['wifi_interface'], c['cellular_interface'] = 'wifi0', 'cell0'
        uid = 65534
        ns(router, 'nft', '-f', '-', input=g.rules(c, uid))
        # Replacing an existing table must also succeed atomically.
        ns(router, 'nft', '-f', '-', input=g.rules(c, uid))
        with tempfile.TemporaryDirectory(prefix='ranch-gate-test-') as d:
            log = Path(d) / 'received.jsonl'
            ready = Path(d) / 'ready'
            server = '''import socket,sys,time,pathlib
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.bind(('0.0.0.0',8554)); s.listen(); pathlib.Path(sys.argv[2]).touch()
while True:
 c,_=s.accept()
 with c:
  while True:
   data=c.recv(4096)
   if not data: break
   with open(sys.argv[1],'a') as f: f.write(str(time.monotonic())+'\\n')
'''
            processes.append(subprocess.Popen(['ip', 'netns', 'exec', receiver, sys.executable,
                                               '-c', server, str(log), str(ready)]))
            for _ in range(50):
                if ready.exists():
                    break
                time.sleep(0.1)
            if not ready.exists():
                raise RuntimeError('Test receiver did not start')
            def denied(name, as_uid):
                p = subprocess.run(['ip', 'netns', 'exec', name, sys.executable,
                                    '-c', probe, 'unused', str(as_uid)], timeout=5)
                if p.returncode != 2:
                    raise AssertionError('Expected connection to be denied')
            denied(router, uid)
            print('PASS: relay upload blocked without a lease')
            ns(router, 'nft', '-f', '-', input=f'add element inet {g.TABLE} lease {{ 10.201.0.2 . 8554 timeout 8s }}\n')
            denied(router, 0)
            denied(camera, uid)
            print('PASS: root output and direct Wi-Fi forwarding blocked even with a lease')
            ns(router, 'nft', '-f', '-', input=f'flush set inet {g.TABLE} lease\n'
               f'add element inet {g.TABLE} lease {{ 10.201.0.2 . 8554 timeout 2s }}\n')
            start = time.monotonic()
            sender = '''import os,socket,time
os.setgroups([]); os.setgid(65534); os.setuid(65534)
s=socket.create_connection(('10.201.0.2',8554),timeout=1)
end=time.monotonic()+4
while time.monotonic()<end:
 s.sendall(b'frame'); time.sleep(.1)
s.close()
'''
            ns(router, sys.executable, '-c', sender)
            arrivals = [float(line) for line in log.read_text().splitlines()]
            if len(arrivals) < 5 or not 0.5 < arrivals[-1] - start < 2.4:
                raise AssertionError('Existing TCP stream did not pass then expire as expected')
            denied(router, uid)
            print('PASS: authorized packets arrive; established TCP stops at kernel expiry without a controller')
    finally:
        for p in processes:
            p.kill()
            p.wait(timeout=5)
        for name in reversed(namespaces):
            run('ip', 'netns', 'delete', name)


if __name__ == '__main__':
    main()
