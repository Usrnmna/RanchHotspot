#!/usr/bin/env python3
"""Install and activate the camera upload gate on the dedicated Ranch Pi."""
import argparse
import os
from pathlib import Path
import secrets
import subprocess
import sys

from hotspot import atomic_write, root
from upload_gate import load, no_flowtables, rules, worker_identity

HERE = Path(__file__).resolve().parent


def call(*args):
    subprocess.run(args, check=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, required=True, help='Edited upload-gate JSON; see docs/UPLOAD_GATE.md')
    args = p.parse_args()
    root()
    c = load(args.config)
    model = Path('/proc/device-tree/model')
    if not model.exists() or 'Raspberry Pi 4 Model B' not in model.read_text():
        raise RuntimeError('This installer targets the dedicated Ranch Raspberry Pi 4')
    for iface in (c['wifi_interface'], c['cellular_interface']):
        if not (Path('/sys/class/net') / iface).exists():
            raise RuntimeError(f'Interface {iface} is absent; use the actual Linux data-interface names')
    # Install over temporary Ethernet before closing internet access.
    call('apt-get', 'update')
    call('apt-get', 'install', '-y', 'python3', 'nftables', 'ffmpeg')
    no_flowtables()
    try:
        worker_identity()
    except KeyError:
        call('useradd', '--system', '--user-group', '--no-create-home',
             '--home-dir', '/nonexistent', '--shell', '/usr/sbin/nologin', 'ranch-stream')
    uid, _ = worker_identity()
    # Ask the actual Linux nftables parser before changing networking.
    subprocess.run(['nft', '--check', '-f', '-'], input=rules(c, uid), text=True, check=True)
    cfg = Path('/etc/ranch-hotspot')
    cfg.mkdir(parents=True, exist_ok=True, mode=0o700)
    cfg.chmod(0o700)
    atomic_write(cfg / 'upload-gate.json', args.config.read_text(encoding='utf-8'))
    key = cfg / 'upload-gate.key'
    if not key.exists():
        atomic_write(key, secrets.token_hex(32) + '\n')
    key.chmod(0o600)
    for name in ('upload_gate.py', 'stream_worker.py', 'send_magic.py'):
        atomic_write(Path('/opt/ranch-hotspot') / name, (HERE / name).read_text(encoding='utf-8'), 0o755)
    for name in ('ranch-upload-lock.service', 'ranch-upload-gate.service'):
        atomic_write(Path('/etc/systemd/system') / name,
                     (HERE / 'systemd' / name).read_text(encoding='utf-8'), 0o644)
    atomic_write(Path('/etc/systemd/system/NetworkManager.service.d/ranch-upload-gate.conf'),
                 (HERE / 'systemd/NetworkManager-upload-gate.conf').read_text(encoding='utf-8'), 0o644)
    call('systemctl', 'daemon-reload')
    call('systemctl', 'enable', 'ranch-upload-lock.service', 'ranch-upload-gate.service')
    call('systemctl', 'stop', 'ranch-upload-gate.service')
    call('systemctl', 'restart', 'ranch-upload-lock.service')
    call('systemctl', 'start', 'ranch-upload-gate.service')
    print('Upload gate installed and activated. General hotspot internet access is now blocked.\n'
          'Securely copy /etc/ranch-hotspot/upload-gate.key to the command sender.\n'
          'Verify cellular command delivery and timed cutoff using docs/UPLOAD_GATE.md.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as e:
        print(f'Installation stopped: {e}', file=sys.stderr)
        sys.exit(1)
