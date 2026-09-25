#!/usr/bin/env python3
"""Configure and start the Ranch Wi-Fi access point and LTE connection.

Normal setup values belong in /etc/ranch-hotspot/hotspot.json (or --config).
Use config/hotspot.example.json for the required keys and validate() for the
accepted values. There are no implicit defaults for missing JSON fields.

Read in this order: main() selects a command; load()/validate() check settings;
check_hardware() identifies the devices; render() builds NetworkManager
profiles; configure()/start() apply or activate them. status() reports local
diagnostics without proving client internet access.

Only validate is intended for offline use. configure and start change the
Pi's radio state and may establish a chargeable cellular connection. This
initializer does not enforce the separate upload-gate traffic policy.
"""
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

# Installed paths. Keep these aligned with install.py and the systemd unit.
# --config changes only the JSON input path, not these output locations.
CONFIG = Path('/etc/ranch-hotspot/hotspot.json')
PROFILES = Path('/etc/NetworkManager/system-connections')
APPLIED = Path('/etc/ranch-hotspot/applied.sha256')

# Stable identities for the two profiles this program owns. These are not
# hardware IDs or credentials. Changing them can leave old profiles behind.
AP_UUID = '6d1a0ccd-0293-4fc2-94cf-6effceea601a'
LTE_UUID = 'c4dd601e-1232-468d-aad4-e9e8b25ec706'

# Exact JSON schema: add a field here only together with validation, rendering
# (if applicable), the example configuration, documentation and tests.
FIELDS = {'country', 'wifi_mac', 'ssid', 'wifi_password', 'channel',
          'lan_address', 'apn', 'lte_username', 'lte_password', 'allow_roaming'}


def run(*args, check=True, timeout=150):
    """Run an argument list without a shell; return stripped standard output.

    timeout is in seconds. check=False tolerates a nonzero exit status, but
    missing commands and timeouts still raise. Keep credentials out of args;
    tool output is deliberately omitted from errors to avoid logging secrets.
    """
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout,
                            env={**os.environ, 'LC_ALL': 'C'})
    if check and result.returncode:
        # Commands never contain credentials. Avoid echoing tool output into logs.
        raise RuntimeError(f'{args[0]} failed (exit {result.returncode}); '
                           'use status and the NetworkManager journal for details')
    return result.stdout.strip()


def root():
    """Reject commands requiring Linux administrator access on other hosts."""
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise RuntimeError('Run this command with sudo on the Raspberry Pi.')


# Configuration parsing: pure checks, with no hardware or network changes.


def validate(c):
    """Return configuration c unchanged, or raise for missing/invalid values.

    c is the complete JSON settings dictionary. channel is a 2.4 GHz channel
    number; lan_address is the Pi's private gateway IPv4 address with /24.
    SSID size is measured in UTF-8 bytes, password size in ASCII characters.
    Empty LTE username/password are allowed for carriers without credentials.
    This checks value syntax, not country/APN correctness or hardware support.
    """
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
    """Read a UTF-8 JSON file at a pathlib.Path and validate every setting."""
    return validate(json.loads(path.read_text(encoding='utf-8')))


# Profile generation: render() returns text; configure() performs the writes.


def keyvalue(s):
    """Escape validated scalar text for a NetworkManager keyfile property."""
    # GLib scalar string escaping. Semicolons are literal in scalar properties.
    return s.replace('\\', '\\\\').replace(' ', '\\s')


def render(c, equipment_id):
    """Return {filename: profile_text} for validated settings and modem IMEI.

    No files or connections are changed here. Returned text contains secrets.
    Hardware binding uses the configured Wi-Fi MAC and discovered modem IMEI.

    Advanced policy lives in the templates below: WPA2/CCMP, 2.4 GHz, shared
    IPv4, disabled IPv6, unlimited autoconnect retries, and LTE route metric 50.
    These are source-code choices, not extra JSON keys. Re-run configure after
    changing a template; the applied-settings hash does not track Python code.
    """
    validate(c)
    if not re.fullmatch(r'[0-9]{14,16}', equipment_id):
        raise ValueError('Modem did not report a usable IMEI; no LTE profile written')
    # Decimal UTF-8 bytes preserve the exact SSID, including spaces and symbols.
    # IPv4 "shared" delegates DHCP/DNS/NAT to NetworkManager; it is not a gate.
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
    # A lower route metric is preferred over higher-metric competing routes.
    # Metered marks the connection; it does not limit SIM data consumption.
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
    """Replace a text file via a same-directory temporary file (LF newlines).

    Creates parent directories as needed and applies mode before replacement.
    The default 0o600 permits only the owner to read/write profile secrets.
    Each file is replaced individually; a pair of profile writes is not one
    transaction. Temporary files are removed even if writing fails.
    """
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


# Hardware inspection. These functions use Linux sysfs and installed OS tools.


def usb_identity(device):
    """Walk a sysfs device's parents; return (USB vendor ID, product ID) or None."""
    for p in [device.resolve(), *device.resolve().parents]:
        if (p / 'idVendor').exists() and (p / 'idProduct').exists():
            return ((p / 'idVendor').read_text().strip().lower(),
                    (p / 'idProduct').read_text().strip().lower())
    return None


def wifi_device(c):
    """Return the interface name for the configured, AP-capable USB radio.

    Matches permanent MAC even if scanning uses a randomized address, then
    requires USB ID 0e8d:7961 and an allowed configured channel. Supporting a
    different radio requires reviewing the hardware check here as well as the
    driver/firmware installation; changing wifi_mac alone is insufficient.
    """
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
    # Channels 1-11 use a 2407 MHz base plus 5 MHz per channel.
    freq = 2407 + c['channel'] * 5
    line = next((s for s in info.splitlines() if f'{freq} MHz ' in s), '')
    if not line or any(flag in line.lower() for flag in ('disabled', 'no ir', 'no-ir')):
        raise RuntimeError('Selected channel cannot initiate an AP under current '
                           'regulatory/firmware restrictions; inspect iw phy output.')
    return p.name


def modem():
    """Return the sole unlocked EM060K modem's reported equipment ID (IMEI).

    Raises for missing/ambiguous devices, a different model, or a locked SIM.
    It never sends a PIN or changes modem settings. render() checks IMEI syntax.
    """
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
    """Reject LAN overlap with currently assigned IPv4 subnets except wifi.

    wifi is the selected AP interface name, whose existing address may be from
    this program. This does not inspect inactive profiles or all routed/VPN
    destinations; it is a check against other interfaces' current addresses.
    """
    lan = ipaddress.IPv4Interface(c['lan_address']).network
    for item in json.loads(run('ip', '-j', '-4', 'address', 'show')):
        if item['ifname'] == wifi:
            continue
        for addr in item.get('addr_info', []):
            other = ipaddress.IPv4Interface(f"{addr['local']}/{addr['prefixlen']}").network
            if lan.overlaps(other):
                raise RuntimeError('Hotspot LAN overlaps another interface; change lan_address.')


def check_hardware(c):
    """Set runtime country, enable both radios, and return (Wi-Fi name, IMEI).

    Despite the name, this changes system state before inspecting hardware.
    It also checks the channel and current interface subnet overlap. It does
    not prove SIM registration, client connectivity or the upload-gate policy.
    """
    run('iw', 'reg', 'set', c['country'])
    run('nmcli', 'radio', 'wifi', 'on')
    run('nmcli', 'radio', 'wwan', 'on')
    wifi = wifi_device(c)
    no_subnet_conflict(c, wifi)
    return wifi, modem()


# Lifecycle actions. NetworkManager owns ongoing connection/reconnection work.


def configure(c):
    """Write/load both profiles and record the applied configuration + IMEI.

    Stops the initializer and disconnects active instances of our two UUIDs;
    unrelated profiles are preserved. Loading autoconnect profiles may activate
    them immediately and use cellular data. Restart the service afterward to
    run start() with the new settings. Failures are reported without rollback.
    """
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
    """Hash settings and modem identity to require configure after either changes.

    This is a change marker, not a profile integrity check. It intentionally
    ignores profile-file edits by NetworkManager and Python/template changes.
    """
    return hashlib.sha256(json.dumps([c, equipment_id], sort_keys=True).encode()).hexdigest()


def start(c):
    """Activate already-configured profiles if inactive; leave active ones up.

    Hardware checks enable radios first. The saved fingerprint must match the
    current JSON and IMEI. Wi-Fi activation waits up to 60 seconds and LTE up
    to 120 seconds; an LTE failure can leave the AP active. This is a one-time
    initializer, not a continuous connectivity or firewall health monitor.
    """
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
    print('Wi-Fi AP and LTE profiles active. Verify the configured traffic policy from a Wi-Fi client.')


def status():
    """Print local diagnostics without changing profiles or testing internet.

    Each listed diagnostic command has a 15-second timeout. The final Wi-Fi
    permanent-MAC lookup uses run()'s default timeout. No configuration file is
    loaded, so this command can help before the setup placeholders are filled.
    """
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
    """Dispatch the CLI command and report expected failures with exit code 1.

    validate needs only a readable config; all other commands require Linux
    root. --config selects an alternate input file, including for offline
    validation, but does not redirect generated profiles or the applied hash.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        'command', choices=['validate', 'configure', 'start', 'status'],
        help='validate: check JSON only; configure: write/load profiles; '
             'start: activate applied profiles; status: print local diagnostics')
    parser.add_argument(
        '--config', type=Path, default=CONFIG,
        help='input JSON path (default: %(default)s); ignored by status; '
             'does not change profile or applied-hash output paths')
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
