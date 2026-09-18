#!/usr/bin/env python3
"""Install on a dedicated Raspberry Pi OS Bookworm/Trixie 64-bit host."""
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from hotspot import atomic_write, root


def call(*args):
    subprocess.run(args, check=True)


def main():
    root()
    model = Path('/proc/device-tree/model')
    if not model.exists() or 'Raspberry Pi 4 Model B' not in model.read_text():
        raise RuntimeError('This installer targets Raspberry Pi 4 Model B.')
    if os.uname().machine != 'aarch64':
        raise RuntimeError('Use 64-bit Raspberry Pi OS Lite.')
    release = Path('/etc/os-release').read_text()
    if not any(f'VERSION_CODENAME={v}' in release for v in ('bookworm', 'trixie')):
        raise RuntimeError('Use Raspberry Pi OS Bookworm or Trixie.')
    for service in ('hostapd', 'dnsmasq', 'dhcpcd'):
        if subprocess.run(['systemctl', 'is-active', '--quiet', service]).returncode == 0:
            raise RuntimeError(f'{service} is active. Resolve the existing network setup '
                               'before using this NetworkManager framework.')
    call('apt-get', 'update')
    call('apt-get', 'install', '-y', 'python3', 'network-manager', 'modemmanager',
         'wpasupplicant', 'dnsmasq-base', 'nftables', 'iw', 'rfkill', 'usbutils',
         'ethtool', 'iproute2', 'wireless-regdb', 'usb-modeswitch',
         'libqmi-utils', 'libmbim-utils')
    # MediaTek firmware moved out of firmware-misc-nonfree in newer distributions.
    result = subprocess.run(['apt-cache', 'show', 'firmware-mediatek'],
                            capture_output=True, text=True)
    firmware = 'firmware-mediatek' if 'Package: firmware-mediatek' in result.stdout else 'firmware-misc-nonfree'
    call('apt-get', 'install', '-y', firmware)
    cfg = Path('/etc/ranch-hotspot')
    cfg.mkdir(mode=0o700, parents=True, exist_ok=True)
    cfg.chmod(0o700)
    if not (cfg / 'hotspot.json').exists():
        atomic_write(cfg / 'hotspot.json',
                     (HERE / 'config/hotspot.example.json').read_text())
    atomic_write(Path('/opt/ranch-hotspot/hotspot.py'),
                 (HERE / 'hotspot.py').read_text(), 0o755)
    atomic_write(Path('/etc/systemd/system/ranch-hotspot.service'),
                 (HERE / 'systemd/ranch-hotspot.service').read_text(), 0o644)
    call('systemctl', 'daemon-reload')
    call('systemctl', 'enable', '--now', 'NetworkManager', 'ModemManager')
    print('\nInstalled. Hotspot service is NOT enabled by this installer.\n'
          'Next: reboot if kernel/firmware changed, run status, edit '
          '/etc/ranch-hotspot/hotspot.json, configure, then enable the service. '
          'See README.md.')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as e:
        print(f'Installation stopped: {e}', file=sys.stderr)
        sys.exit(1)
