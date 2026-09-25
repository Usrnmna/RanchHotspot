#!/usr/bin/env python3
"""Install and immediately activate the camera upload gate on the Ranch Pi.

After the base hotspot installation, run with sudo and --config pointing to your
edited copy of config/upload-gate.example.json. See docs/UPLOAD_GATE.md for the
networking prerequisites and operational checks.

This installer installs OS packages, creates the ranch-stream system account
when needed, copies configuration/programs/service units, and starts the upload
firewall and controller. Activation blocks general hotspot internet access.
Use temporary Ethernet for package downloads before that change. Packages come
from the Pi's existing APT repositories. Completed changes are not rolled back
if a later step fails.

Re-running replaces the installed JSON configuration with --config and preserves
an existing upload-gate.key. Normal source/receiver/limit edits belong in that
JSON file, not in this installer. For an already-installed gate, use the direct
configuration update in docs/CONFIGURATION.md; it needs no package downloads.
Re-running this installer behind an active gate requires an APT proxy/mirror
reachable inside an allowed private management subnet for package downloads.
"""
import argparse
import os
from pathlib import Path
import secrets
import subprocess
import sys

from hotspot import atomic_write, root
from upload_gate import load, no_flowtables, rules, worker_identity

HERE = Path(__file__).resolve().parent  # Bundled files are relative to this script.


def call(*args):
    """Run a command with inherited output; stop installation on a nonzero exit."""
    subprocess.run(args, check=True)


def main():
    """Validate settings/host, install files, then activate firewall and listener.

    Validation occurs before activation; it is not a dry run of this installer.
    The final service operations revoke current upload permissions before the
    listener is restarted. Existing authorized uploads are interrupted.
    """
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--config', type=Path, required=True, help='Edited upload-gate JSON; see docs/UPLOAD_GATE.md')
    args = p.parse_args()
    root()
    c = load(args.config)  # Enforce the same configuration rules as the running gate.
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
    # The relay runs as an unprivileged account; the controller retains firewall
    # privileges. Reuse the installed account so its UID remains consistent.
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
    # Unlike install.py's base config handling, this explicitly replaces the
    # upload-gate configuration on each install using the supplied edited file.
    atomic_write(cfg / 'upload-gate.json', args.config.read_text(encoding='utf-8'))
    key = cfg / 'upload-gate.key'
    # A new key is 32 random bytes stored as 64 hexadecimal characters. Keeping
    # an existing key avoids invalidating the private copies on command senders.
    if not key.exists():
        atomic_write(key, secrets.token_hex(32) + '\n')
    key.chmod(0o600)
    # Program destinations and service ExecStart paths must remain in agreement
    # if the installation layout is ever changed.
    for name in ('upload_gate.py', 'stream_worker.py', 'send_magic.py'):
        atomic_write(Path('/opt/ranch-hotspot') / name, (HERE / name).read_text(encoding='utf-8'), 0o755)
    for name in ('ranch-upload-lock.service', 'ranch-upload-gate.service'):
        atomic_write(Path('/etc/systemd/system') / name,
                     (HERE / 'systemd' / name).read_text(encoding='utf-8'), 0o644)
    atomic_write(Path('/etc/systemd/system/NetworkManager.service.d/ranch-upload-gate.conf'),
                 (HERE / 'systemd/NetworkManager-upload-gate.conf').read_text(encoding='utf-8'), 0o644)
    # The lock unit closes uploads first; stopping it intentionally does not
    # remove the firewall. Starting the gate then accepts authenticated commands.
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
