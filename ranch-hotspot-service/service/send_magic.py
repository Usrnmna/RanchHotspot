#!/usr/bin/env python3
"""Send one signed start/stop command to the Pi using Python's standard library.

Run this on the authorized sender with a private copy of upload-gate.key; no
camera stream is transmitted by this program. Supply --host, --port, --target,
and --stream to match the Pi's reachability and upload-gate.json configuration.
For usage and per-command settings, run: python3 send_magic.py --help

One UDP packet is sent with no acknowledgment or automatic retry. The sender and
Pi clocks must agree within the Pi's clock_skew_seconds setting. --seconds is
the requested upload lifetime, bounded by the Pi's configured max_duration.
"""
import argparse
from pathlib import Path
import secrets
import socket
import time

from upload_gate import canonical, integer, read_key, sign


def main():
    """Parse command options, validate duration/port, sign, and send one packet.

    Reading the private key and sending UDP are the side effects. The final
    message confirms a local send only; the Pi may reject or never receive it.
    """
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--host', required=True, help='Reachable cellular IPv4 address or DNS name of the Pi')
    p.add_argument('--port', type=int, default=45991,
                   help='Pi control_port (UDP; default: 45991)')
    p.add_argument('--target', required=True, help='Configured router target ID')
    p.add_argument('--key', type=Path, required=True, help='Private copy of upload-gate.key')
    p.add_argument('--stream', required=True, help='Exact key in the Pi configuration streams object')
    p.add_argument('--action', choices=('start', 'stop'), default='start',
                   help='Requested action (default: start); stop applies to the named active stream')
    p.add_argument('--seconds', type=int, default=60,
                   help='Start duration in seconds (default: 60; ignored for stop); must fit Pi max_duration')
    args = p.parse_args()
    # The wire protocol requires zero duration for stop, regardless of --seconds.
    duration = args.seconds if args.action == 'start' else 0
    integer(args.port, 1, 65535, 'port')
    integer(duration, 1 if args.action == 'start' else 0, 3600, 'duration')
    # issued_ms is Unix time in milliseconds; a fresh 16-byte nonce identifies
    # this command so the Pi can reject replays. Keep wire fields in sync with
    # upload_gate.authenticate() when reviewing/changing the protocol.
    body = dict(v=1, target=args.target, stream=args.stream, action=args.action,
                duration=duration, issued_ms=time.time_ns() // 1_000_000, nonce=secrets.token_hex(16))
    packet = canonical(sign(body, read_key(args.key)))
    # IPv4 only. The host must reach the Pi's cellular UDP listener; this does
    # not create carrier port forwarding or bypass carrier NAT.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(packet, (args.host, args.port))
    print('Signed command sent. Delivery is not acknowledged; check the Pi journal or receiving server.')


if __name__ == '__main__':
    main()
