#!/usr/bin/env python3
"""Small, dependency-free NetworkManager/ModemManager initialization tool."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

CONFIG = Path('/etc/ranch-hotspot/hotspot.json')
PROFILES = Path('/etc/NetworkManager/system-connections')
APPLIED = Path('/etc/ranch-hotspot/applied.sha256')
AP_UUID = '6d1a0ccd-0293-4fc2-94cf-6effceea601a'
LTE_UUID = 'c4dd601e-1232-468d-aad4-e9e8b25ec706'
FIELDS = {'country', 'wifi_mac', 'ssid', 'wifi_password', 'channel',
          'lan_address', 'apn', 'lte_username', 'lte_password', 'allow_roaming'}


def run(*args, check=True, timeout=150):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout,
                            env={**os.environ, 'LC_ALL': 'C'})
    if check and result.returncode:
        # Commands never contain credentials. Avoid echoing tool output into logs.
        raise RuntimeError(f'{args[0]} failed (exit {result.returncode}); '
                           'use status and the NetworkManager journal for details')
    return result.stdout.strip()


def root():
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise RuntimeError('Run this command with sudo on the Raspberry Pi.')


def validate(c):
    if not isinstance(c, dict) or set(c) != FIELDS:
        raise ValueError('Configuration fields must match hotspot.example.json exactly.')
    for k in FIELDS - {'channel', 'allow_roaming'}:
        v = c[k]
        if not isinstance(v, str) or any(ord(x) < 32 or ord(x) == 127 for x in v):
            raise ValueError(f'{k}: expected text without control characters')
        if v.startswith('SET_'):
            raise ValueError(f'{k}: replace the setup placeholder')
    if not re.fullmatch(r'[A-Z]{2}', c['country']):
        raise ValueError('country: use the actual two-letter country code')
    if not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', c['wifi_mac']):
        raise ValueError('wifi_mac: use the USB radio permanent MAC address')
    first = int(c['wifi_mac'][:2], 16)
    if first & 1 or c['wifi_mac'].lower() == '00:00:00:00:00:00':
        raise ValueError('wifi_mac: must be a unicast hardware address')
    if not 1 <= len(c['ssid'].encode('utf-8')) <= 32:
        raise ValueError('ssid: must be 1-32 UTF-8 bytes')
    if not 8 <= len(c['wifi_password']) <= 63 or not c['wifi_password'].isascii():
        raise ValueError('wifi_password: use 8-63 printable ASCII characters')
    if type(c['channel']) is not int or c['channel'] not in range(1, 12):
        raise ValueError('channel: this 2.4 GHz framework accepts channels 1-11')
    if type(c['allow_roaming']) is not bool:
        raise ValueError('allow_roaming must be true or false')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,99}', c['apn']):
        raise ValueError('apn: enter the carrier APN')
    lan = ipaddress.IPv4Interface(c['lan_address'])
    private = [ipaddress.IPv4Network(n) for n in
               ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
    if (lan.network.prefixlen != 24 or lan.ip in
            (lan.network.network_address, lan.network.broadcast_address) or
            not any(lan.network.subnet_of(n) for n in private)):
        raise ValueError('lan_address: use a private /24 with a usable host address')
    return c


def load(path):
    return validate(json.loads(path.read_text(encoding='utf-8')))


def keyvalue(s):
    # GLib scalar string escaping. Semicolons are literal in scalar properties.
    return s.replace('\\', '\\\\').replace(' ', '\\s')


def render(c, equipment_id):
    validate(c)
    if not re.fullmatch(r'[0-9]{14,16}', equipment_id):
        raise ValueError('Modem did not report a usable IMEI; no LTE profile written')
    ap = f'''[connection]
id=ranch-hotspot-ap
uuid={AP_UUID}
type=wifi
autoconnect=true
autoconnect-priority=100
autoconnect-retries=0

[wifi]
mode=ap
ssid={''.join(str(b) + ';' for b in c['ssid'].encode('utf-8'))}
mac-address={c['wifi_mac']}
cloned-mac-address=permanent
band=bg
channel={c['channel']}
powersave=2

[wifi-security]
key-mgmt=wpa-psk
proto=rsn;
pairwise=ccmp;
group=ccmp;
psk={keyvalue(c['wifi_password'])}
psk-flags=0

[ipv4]
method=shared
address1={c['lan_address']}
never-default=true

[ipv6]
method=disabled
'''
    lte = f'''[connection]
id=ranch-hotspot-lte
uuid={LTE_UUID}
type=gsm
autoconnect=true
autoconnect-priority=100
autoconnect-retries=0
metered=1

[gsm]
device-id={equipment_id}
apn={keyvalue(c['apn'])}
auto-config=false
home-only={str(not c['allow_roaming']).lower()}
'''
    if c['lte_username']:
        lte += f"username={keyvalue(c['lte_username'])}\n"
    if c['lte_password']:
        lte += f"password={keyvalue(c['lte_password'])}\npassword-flags=0\n"
    lte += '''
[ipv4]
method=auto
route-metric=50
may-fail=false

[ipv6]
method=disabled
'''
    return {'ranch-hotspot-ap.nmconnection': ap,
            'ranch-hotspot-lte.nmconnection': lte}


def atomic_write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            f.write(content)
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def usb_identity(device):
    for p in [device.resolve(), *device.resolve().parents]:
        if (p / 'idVendor').exists() and (p / 'idProduct').exists():
            return ((p / 'idVendor').read_text().strip().lower(),
                    (p / 'idProduct').read_text().strip().lower())
    return None


def wifi_device(c):
    matches = []
    for p in Path('/sys/class/net').iterdir():
        if not (p / 'phy80211').exists():
            continue
        # ethtool reports the permanent address even when NM randomizes scan MACs.
        permanent = run('ethtool', '-P', p.name, check=False)
        if c['wifi_mac'].lower() in permanent.lower():
            matches.append(p)
    if len(matches) != 1:
        raise RuntimeError('Configured USB Wi-Fi MAC not found uniquely; run status.')
    p = matches[0]
    if usb_identity(p / 'device') != ('0e8d', '7961'):
        raise RuntimeError('Selected radio is not the expected AX9L USB ID 0e8d:7961. '
                           'Verify the exact BrosTrend model/revision.')
    phy = (p / 'phy80211').resolve().name
    info = run('iw', 'phy', phy, 'info')
    modes = info.split('Supported interface modes:', 1)
    if len(modes) != 2 or not re.search(r'^\s*\* AP\s*$', modes[1], re.M):
        raise RuntimeError('USB radio/driver does not advertise AP mode.')
    freq = 2407 + c['channel'] * 5
    line = next((s for s in info.splitlines() if f'{freq} MHz ' in s), '')
    if not line or any(flag in line.lower() for flag in ('disabled', 'no ir', 'no-ir')):
        raise RuntimeError('Selected channel cannot initiate an AP under current '
                           'regulatory/firmware restrictions; inspect iw phy output.')
    return p.name


def modem():
    listing = run('mmcli', '-L')
    paths = re.findall(r'/org/freedesktop/ModemManager1/Modem/\d+', listing)
    if len(paths) != 1:
        raise RuntimeError('Expected exactly one ModemManager modem; run status.')
    data = json.loads(run('mmcli', '-m', paths[0], '--output-json'))
    g = data['modem']['generic']
    if 'EM060K' not in str(g.get('model', '')).upper():
        raise RuntimeError('Detected modem is not an EM060K; verify hardware/driver.')
    lock = g.get('unlock-required', 'unknown')
    if lock not in ('none', '--'):
        raise RuntimeError('SIM/modem is locked or lock state unknown. Resolve manually; '
                           'the service never retries SIM PINs.')
    return g['equipment-identifier']


def no_subnet_conflict(c, wifi):
    lan = ipaddress.IPv4Interface(c['lan_address']).network
    for item in json.loads(run('ip', '-j', '-4', 'address', 'show')):
        if item['ifname'] == wifi:
            continue
        for addr in item.get('addr_info', []):
            other = ipaddress.IPv4Interface(f"{addr['local']}/{addr['prefixlen']}").network
            if lan.overlaps(other):
                raise RuntimeError('Hotspot LAN overlaps another interface; change lan_address.')


def check_hardware(c):
    run('iw', 'reg', 'set', c['country'])
    run('nmcli', 'radio', 'wifi', 'on')
    run('nmcli', 'radio', 'wwan', 'on')
    wifi = wifi_device(c)
    no_subnet_conflict(c, wifi)
    return wifi, modem()


def configure(c):
    wifi, equipment_id = check_hardware(c)
    profiles = render(c, equipment_id)
    # Stop the retrying initializer before changing its profiles. Disconnect only ours.
    run('systemctl', 'stop', 'ranch-hotspot.service')
    for name, content in profiles.items():
        atomic_write(PROFILES / name, content)
        run('nmcli', 'connection', 'load', str(PROFILES / name))
    # Reload first, then disconnect any running old instance. Autoactivation now
    # uses the new settings even if it races with the later service start.
    active = run('nmcli', '-t', '-f', 'UUID', 'connection', 'show', '--active').splitlines()
    for uid in (AP_UUID, LTE_UUID):
        if uid in active:
            run('nmcli', 'connection', 'down', 'uuid', uid)
    atomic_write(APPLIED, fingerprint(c, equipment_id))
    print(f'Profiles loaded for {wifi} and EM060K. Reapply with: '
          'sudo systemctl restart ranch-hotspot')


def fingerprint(c, equipment_id):
    return hashlib.sha256(json.dumps([c, equipment_id], sort_keys=True).encode()).hexdigest()


def start(c):
    wifi, equipment_id = check_hardware(c)
    # Require configure again after replacing the modem or changing setup values.
    if not APPLIED.exists() or APPLIED.read_text() != fingerprint(c, equipment_id):
        raise RuntimeError('Configuration changed or not applied; run configure.')
    for uid in (AP_UUID, LTE_UUID):
        run('nmcli', 'connection', 'show', 'uuid', uid)
    active = run('nmcli', '-t', '-f', 'UUID', 'connection', 'show', '--active').splitlines()
    if AP_UUID not in active:
        run('nmcli', '--wait', '60', 'connection', 'up', 'uuid', AP_UUID, 'ifname', wifi)
    if LTE_UUID not in active:
        run('nmcli', '--wait', '120', 'connection', 'up', 'uuid', LTE_UUID)
    no_subnet_conflict(c, wifi)
    print('Wi-Fi AP and LTE profiles active. Verify internet access from a Wi-Fi client.')


def status():
    commands = [
        ('Kernel', ('uname', '-r')),
        ('USB devices and drivers', ('lsusb', '-t')),
        ('USB identities', ('lsusb',)),
        ('Network devices', ('nmcli', 'device', 'status')),
        ('Active connections', ('nmcli', '-f', 'NAME,UUID,TYPE,DEVICE',
                                'connection', 'show', '--active')),
        ('Modem discovery', ('mmcli', '-L')),
        ('Regulatory domain', ('iw', 'reg', 'get')),
        ('Radio blocks', ('rfkill', 'list')),
        ('IPv4 routes', ('ip', '-4', 'route')),
        ('Service', ('systemctl', '--no-pager', 'status', 'ranch-hotspot')),
    ]
    for label, args in commands:
        print('\n' + label + ':')
        try:
            print(run(*args, check=False, timeout=15) or '(no output)')
        except (OSError, subprocess.TimeoutExpired) as e:
            print(type(e).__name__)
    for p in Path('/sys/class/net').iterdir():
        if (p / 'phy80211').exists():
            print(p.name + ': ' + run('ethtool', '-P', p.name, check=False))
    print('\nStatus is diagnostic only; it does not prove internet connectivity.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['validate', 'configure', 'start', 'status'])
    parser.add_argument('--config', type=Path, default=CONFIG)
    args = parser.parse_args()
    try:
        if args.command != 'validate':
            root()
        if args.command == 'status':
            status()
            return
        c = load(args.config)
        if args.command == 'validate':
            print('Configuration valid (hardware has not been checked).')
        elif args.command == 'configure':
            configure(c)
        else:
            start(c)
    except (ValueError, OSError, RuntimeError, KeyError, subprocess.TimeoutExpired) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
