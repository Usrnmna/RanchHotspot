#!/usr/bin/env python3
"""Install the base hotspot files on a dedicated Raspberry Pi 4 Model B.

Run with sudo on Raspberry Pi OS Bookworm/Trixie 64-bit. This changes installed
OS packages, /etc and /opt files, and NetworkManager/ModemManager services; it is
an installer, not a read-only configuration check. Package downloads use the
Pi's existing APT repository configuration and require working internet access.

Hotspot parameters belong in /etc/ranch-hotspot/hotspot.json, initially copied
from config/hotspot.example.json. The installer preserves that file if present,
replaces the installed Python/service files, and leaves hotspot activation for
the manual configure/enable steps in README.md. Failures stop installation with
no automatic rollback of completed steps.
"""
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent  # Resolve bundled files beside this script, not the shell's directory.
sys.path.insert(0, str(HERE))
from hotspot import atomic_write, root


def call(*args):
    """Run a command with inherited output; raise if its exit status is nonzero."""
    subprocess.run(args, check=True)


def main():
    """Check the supported host, install dependencies/files, and start networking.

    Existing hostapd/dnsmasq/dhcpcd services cause a stop before package changes.
    NetworkManager and ModemManager are enabled and started; ranch-hotspot.service
    is installed but is not enabled or started by this function.
    """
    root()
    # Compatibility checks are intentional guardrails, not normal user settings.
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
    # Base networking/modem dependencies. Repository URLs are managed by the OS.
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
    # Configuration can contain passwords; restrict the directory to root.
    cfg = Path('/etc/ranch-hotspot')
    cfg.mkdir(mode=0o700, parents=True, exist_ok=True)
    cfg.chmod(0o700)
    # Re-running installation retains the administrator's current JSON settings.
    if not (cfg / 'hotspot.json').exists():
        atomic_write(cfg / 'hotspot.json',
                     (HERE / 'config/hotspot.example.json').read_text())
    atomic_write(Path('/opt/ranch-hotspot/hotspot.py'),
                 (HERE / 'hotspot.py').read_text(), 0o755)
    atomic_write(Path('/etc/systemd/system/ranch-hotspot.service'),
                 (HERE / 'systemd/ranch-hotspot.service').read_text(), 0o644)
    # Reload unit definitions after writing them; activation of the hotspot itself
    # remains a separate step once the actual interfaces/SSID/password are set.
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
