#!/usr/bin/env python3
"""Authenticated, fail-closed cellular upload controller for the Ranch Pi."""
import argparse
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit

CONFIG = Path('/etc/ranch-hotspot/upload-gate.json')
KEY = Path('/etc/ranch-hotspot/upload-gate.key')
STATE = Path('/var/lib/ranch-upload-gate/commands.sqlite3')
TABLE = 'ranch_upload_gate'
MAX_PACKET = 2048
FIELDS = {'v', 'target', 'stream', 'action', 'duration', 'issued_ms', 'nonce', 'signature'}


def integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name}: expected integer {low}..{high}')


def text(value, pattern, name):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f'{name}: invalid value')


def endpoint(url, schemes):
    if not isinstance(url, str) or any(ord(c) < 33 for c in url):
        raise ValueError('URLs must be text without spaces/control characters')
    u = urlsplit(url)
    if u.scheme not in schemes or not u.hostname or u.fragment:
        raise ValueError('Unsupported URL scheme, host or fragment')
    port = u.port or {'rtsp': 554, 'http': 80, 'https': 443}[u.scheme]
    integer(port, 1, 65535, 'URL port')
    return u, port


def validate(c):
    expected = {'target', 'wifi_interface', 'cellular_interface', 'lan_network',
                'management', 'control_port', 'max_duration', 'clock_skew_seconds',
                'upload_bytes_per_second', 'streams'}
    if not isinstance(c, dict) or set(c) != expected:
        raise ValueError('Fields must match upload-gate.example.json')
    text(c['target'], r'[A-Za-z0-9_-]{1,64}', 'target')
    for field in ('wifi_interface', 'cellular_interface'):
        text(c[field], r'[A-Za-z0-9_-]{1,15}', field)
    if c['wifi_interface'] == c['cellular_interface'] or 'lo' in (c['wifi_interface'], c['cellular_interface']):
        raise ValueError('Use distinct Wi-Fi and cellular interfaces, neither loopback')
    lan = ipaddress.IPv4Network(c['lan_network'])
    private = [ipaddress.IPv4Network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
    if not any(lan.subnet_of(n) for n in private) or lan.prefixlen > 30:
        raise ValueError('lan_network must be a usable private IPv4 subnet')
    if not isinstance(c['management'], list):
        raise ValueError('management must be a list')
    for m in c['management']:
        if not isinstance(m, dict) or set(m) != {'interface', 'network'}:
            raise ValueError('management entries require interface and network')
        text(m['interface'], r'[A-Za-z0-9_-]{1,15}', 'management interface')
        if m['interface'] in ('lo', c['cellular_interface'], c['wifi_interface']):
            raise ValueError('Management must use a separate local interface')
        net = ipaddress.IPv4Network(m['network'])
        if not any(net.subnet_of(n) for n in private):
            raise ValueError('Management must use a private IPv4 subnet')
    integer(c['control_port'], 1024, 65535, 'control_port')
    integer(c['max_duration'], 1, 3600, 'max_duration')
    integer(c['clock_skew_seconds'], 1, 120, 'clock_skew_seconds')
    integer(c['upload_bytes_per_second'], 1000, 115000, 'upload_bytes_per_second')
    if not isinstance(c['streams'], dict) or not 1 <= len(c['streams']) <= 32:
        raise ValueError('Configure 1..32 streams')
    for name, s in c['streams'].items():
        text(name, r'[A-Za-z0-9_-]{1,64}', 'stream name')
        common = {'kind', 'source_url', 'receiver_url', 'receiver_ip'}
        if not isinstance(s, dict) or s.get('kind') not in ('rtsp', 'stills'):
            raise ValueError('Stream kind must be rtsp or stills')
        extra = {'interval_seconds', 'max_image_bytes', 'receiver_token'} if s['kind'] == 'stills' else set()
        if set(s) != common | extra:
            raise ValueError(f'{name}: unexpected or missing stream fields')
        source, _ = endpoint(s['source_url'], ('rtsp',) if s['kind'] == 'rtsp' else ('http', 'https'))
        source_ip = ipaddress.IPv4Address(source.hostname)
        if source_ip not in lan or source_ip in (lan.network_address, lan.broadcast_address):
            raise ValueError('Camera URL must use a fixed usable IP on the hotspot LAN')
        receiver, _ = endpoint(s['receiver_url'], ('rtsp',) if s['kind'] == 'rtsp' else ('http', 'https'))
        receiver_ip = ipaddress.IPv4Address(s['receiver_ip'])
        if receiver_ip.is_multicast or receiver_ip.is_unspecified or receiver_ip.is_loopback or receiver_ip.is_link_local or receiver_ip in lan:
            raise ValueError('receiver_ip must be a unicast address outside the camera LAN')
        if s['kind'] == 'rtsp' and receiver.hostname != str(receiver_ip):
            raise ValueError('RTSP receiver URL must use receiver_ip, avoiding cellular DNS')
        if s['kind'] == 'stills':
            if receiver.username or receiver.password:
                raise ValueError('Use receiver_token for snapshot destination authorization')
            integer(s['interval_seconds'], 1, 86400, 'interval_seconds')
            integer(s['max_image_bytes'], 1024, 8 * 1024 * 1024, 'max_image_bytes')
            if not isinstance(s['receiver_token'], str) or any(ord(x) < 32 or ord(x) > 126 for x in s['receiver_token']):
                raise ValueError('receiver_token must be printable ASCII')
    return c


def load(path=CONFIG):
    return validate(json.loads(path.read_text(encoding='utf-8')))


def read_key(path=KEY):
    key = bytes.fromhex(path.read_text(encoding='ascii').strip())
    if len(key) != 32:
        raise ValueError('Control key must contain exactly 64 hexadecimal characters')
    return key


def canonical(body):
    return json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def sign(body, key):
    return {**body, 'signature': hmac.new(key, canonical(body), hashlib.sha256).hexdigest()}


def unique_object(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise ValueError('Duplicate JSON field')
        result[k] = v
    return result


def authenticate(packet, key, c, now_ms):
    if len(packet) > MAX_PACKET:
        raise ValueError('Oversized packet')
    msg = json.loads(packet, object_pairs_hook=unique_object)
    if not isinstance(msg, dict) or set(msg) != FIELDS:
        raise ValueError('Invalid command fields')
    signature = msg.pop('signature')
    text(signature, r'[0-9a-f]{64}', 'signature')
    if not hmac.compare_digest(signature, hmac.new(key, canonical(msg), hashlib.sha256).hexdigest()):
        raise ValueError('Invalid signature')
    integer(msg['v'], 1, 1, 'v')
    if msg['target'] != c['target'] or not isinstance(msg['stream'], str) or msg['stream'] not in c['streams']:
        raise ValueError('Wrong target or unknown stream')
    if msg['action'] not in ('start', 'stop'):
        raise ValueError('Invalid action')
    integer(msg['duration'], 1 if msg['action'] == 'start' else 0,
            c['max_duration'] if msg['action'] == 'start' else 0, 'duration')
    integer(msg['issued_ms'], 1, 2**53 - 1, 'issued_ms')
    if abs(now_ms - msg['issued_ms']) > c['clock_skew_seconds'] * 1000:
        raise ValueError('Expired or future command')
    text(msg['nonce'], r'[0-9a-f]{32}', 'nonce')
    return msg


class ReplayStore:
    """Commit before granting permission; keep replay history across restarts."""
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS commands (nonce TEXT PRIMARY KEY, issued INTEGER NOT NULL)')
        self.db.execute('CREATE INDEX IF NOT EXISTS issued_index ON commands(issued)')
        self.db.commit()

    def consume(self, msg):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            newest = self.db.execute('SELECT MAX(issued) FROM commands').fetchone()[0]
            if newest is not None and msg['issued_ms'] <= newest:
                raise ValueError('Replayed or out-of-order command')
            try:
                self.db.execute('INSERT INTO commands VALUES (?, ?)', (msg['nonce'], msg['issued_ms']))
            except sqlite3.IntegrityError as e:
                raise ValueError('Replayed nonce') from e


def nft_run(script):
    p = subprocess.run(['nft', '-f', '-'], input=script, text=True, capture_output=True, timeout=10)
    if p.returncode:
        raise RuntimeError('nftables operation failed; no permission granted (check configuration locally)')


def rules(c, uid):
    """Atomic table replacement starts with no leases; never flush other tables."""
    integer(uid, 1, 2**31 - 1, 'worker UID')
    wifi, cell = c['wifi_interface'], c['cellular_interface']
    local = [(wifi, c['lan_network'])] + [(m['interface'], m['network']) for m in c['management']]
    local_output = '\n'.join(f'        oifname "{iface}" ip daddr {net} accept' for iface, net in local)
    local_input = '\n'.join(f'        iifname "{iface}" ip saddr {net} accept' for iface, net in local)
    return f'''add table inet {TABLE}
delete table inet {TABLE}
table inet {TABLE} {{
    set lease {{ type ipv4_addr . inet_service; flags timeout; }}
    chain forward {{ type filter hook forward priority -10; policy drop; }}
    chain input {{
        type filter hook input priority -10; policy drop;
        iifname "lo" accept
        udp dport {c['control_port']} iifname != "{cell}" drop
        iifname "{cell}" udp dport {c['control_port']} limit rate 20/second burst 40 packets accept
{local_input}
        iifname "{wifi}" udp sport 68 udp dport 67 accept
        iifname "{cell}" udp sport 67 udp dport 68 accept
        ct state established,related accept
    }}
    chain upload {{
        limit rate over {c['upload_bytes_per_second']} bytes/second burst 16000 bytes counter drop
        meta skuid {uid} ip daddr . tcp dport @lease counter accept
        counter drop
    }}
    chain output {{
        type filter hook output priority -10; policy drop;
        oifname "lo" accept
{local_output}
        oifname "{wifi}" udp sport 67 udp dport 68 accept
        oifname "{cell}" ip daddr 255.255.255.255 udp sport 68 udp dport 67 accept
        oifname "{cell}" jump upload
    }}
}}
'''


def no_flowtables():
    p = subprocess.run(['nft', '-j', 'list', 'ruleset'], capture_output=True, text=True, check=True, timeout=10)
    if any('flowtable' in item for item in json.loads(p.stdout)['nftables']):
        raise RuntimeError('Remove forwarding/flowtable offload before enabling the upload gate')


class Firewall:
    def close(self):
        nft_run(f'flush set inet {TABLE} lease\n')

    def grant(self, stream, duration):
        _, port = endpoint(stream['receiver_url'], ('rtsp', 'http', 'https'))
        nft_run(f'flush set inet {TABLE} lease\n'
                f'add element inet {TABLE} lease {{ {stream["receiver_ip"]} . {port} timeout {duration}s }}\n')


def launch_worker(stream, uid, gid):
    # Secrets go over a private pipe, not command-line arguments or logs.
    p = subprocess.Popen([sys.executable, str(Path(__file__).with_name('stream_worker.py'))],
                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         user=uid, group=gid, extra_groups=[], start_new_session=True, text=True)
    try:
        p.stdin.write(json.dumps(stream))
        p.stdin.close()
    except Exception:
        os.killpg(p.pid, signal.SIGKILL)
        p.wait()
        raise
    return p


def kill_worker(p):
    # Kill its process group, including FFmpeg. Revocation precedes this call.
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    p.wait(timeout=5)


class Controller:
    def __init__(self, c, firewall, store, launcher, killer=kill_worker, clock=time.monotonic):
        self.c, self.firewall, self.store = c, firewall, store
        self.launcher, self.killer, self.clock = launcher, killer, clock
        self.active = None

    def stop(self):
        try:
            self.firewall.close()
        finally:
            if self.active:
                self.killer(self.active[1])
                self.active = None

    def handle(self, msg):
        self.store.consume(msg)
        if msg['action'] == 'stop':
            if self.active and self.active[0] == msg['stream']:
                self.stop()
            return
        self.stop()  # Only one upload at a time; new signed starts replace it.
        deadline = self.clock() + msg['duration']
        stream = {**self.c['streams'][msg['stream']],
                  'pace_bytes_per_second': int(self.c['upload_bytes_per_second'] * 0.75)}
        try:
            self.firewall.grant(stream, msg['duration'])
            p = self.launcher(stream)
            self.active = (msg['stream'], p, deadline)
        except Exception:
            self.firewall.close()
            raise

    def tick(self):
        if self.active and (self.clock() >= self.active[2] or self.active[1].poll() is not None):
            print(f'Closed {self.active[0]}: deadline reached or relay ended.', flush=True)
            self.stop()


def worker_identity():
    import pwd
    entry = pwd.getpwnam('ranch-stream')
    if entry.pw_uid == 0:
        raise RuntimeError('Worker must be an unprivileged system account')
    return entry.pw_uid, entry.pw_gid


def serve(c, key, state):
    uid, gid = worker_identity()
    no_flowtables()
    nft_run(rules(c, uid))
    store = ReplayStore(state)
    ctl = Controller(c, Firewall(), store, lambda s: launch_worker(s, uid, gid))
    def shutdown(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, shutdown)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, c['cellular_interface'].encode() + b'\0')
            sock.bind(('0.0.0.0', c['control_port']))
            sock.settimeout(0.2)
            print('Upload gate ready; all streams closed.', flush=True)
            while True:
                ctl.tick()
                try:
                    packet, _ = sock.recvfrom(MAX_PACKET + 1)
                except socket.timeout:
                    continue
                try:
                    msg = authenticate(packet, key, c, time.time_ns() // 1_000_000)
                    ctl.handle(msg)
                    print(f"Accepted {msg['action']} for {msg['stream']} ({msg['duration']} seconds).", flush=True)
                except (ValueError, UnicodeError, TypeError, RecursionError):
                    # No response, no untrusted input/URLs/secrets in logs.
                    continue
    finally:
        ctl.stop()
        store.db.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=('validate', 'render', 'lock', 'serve'))
    p.add_argument('--config', type=Path, default=CONFIG)
    p.add_argument('--key', type=Path, default=KEY)
    p.add_argument('--state', type=Path, default=STATE)
    args = p.parse_args()
    c = load(args.config)
    if args.command == 'validate':
        print('Configuration valid; camera/receiver reachability not checked.')
        return
    if args.command == 'render':
        print(rules(c, worker_identity()[0]), end='')
        return
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise RuntimeError('Run on the Pi as root')
    if args.command == 'lock':
        nft_run(rules(c, worker_identity()[0]))
        no_flowtables()
    else:
        serve(c, read_key(args.key), args.state)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError, RuntimeError, KeyError, sqlite3.Error, subprocess.SubprocessError):
        print('Upload gate failed; inspect setup locally. Existing kernel leases still expire.', file=sys.stderr)
        sys.exit(1)
