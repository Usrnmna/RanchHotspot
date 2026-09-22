#!/usr/bin/env python3
"""Unprivileged camera relay. Started only by upload_gate.py via a private pipe."""
import base64
import http.client
import json
import socket
import ssl
import subprocess
import sys
import time
from urllib.parse import unquote

from upload_gate import endpoint


def video_command(s):
    # Camera must provide H.264 below the configured uplink budget. No Pi encoding.
    return ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-rtsp_transport', 'tcp', '-timeout', '5000000', '-i', s['source_url'],
            '-map', '0:v:0', '-an', '-c:v', 'copy', '-f', 'rtsp',
            '-rtsp_transport', 'tcp', s['receiver_url']]


def connection(url, fixed_ip=None):
    u, port = endpoint(url, ('http', 'https'))
    cls = http.client.HTTPSConnection if u.scheme == 'https' else http.client.HTTPConnection
    kwargs = {'context': ssl.create_default_context()} if u.scheme == 'https' else {}
    conn = cls(u.hostname, port, timeout=10, **kwargs)
    # Preserve Host/SNI and certificate checks, but never ask cellular DNS.
    if fixed_ip:
        conn._create_connection = lambda address, timeout, source_address=None: socket.create_connection(
            (fixed_ip, port), timeout, source_address)
    path = u.path or '/'
    if u.query:
        path += '?' + u.query
    return conn, u, path


def fetch_jpeg(s):
    conn, u, path = connection(s['source_url'])
    headers = {'Accept': 'image/jpeg'}
    if u.username is not None:
        auth = f'{unquote(u.username)}:{unquote(u.password or "")}'.encode()
        headers['Authorization'] = 'Basic ' + base64.b64encode(auth).decode('ascii')
    try:
        conn.request('GET', path, headers=headers)
        r = conn.getresponse()
        if r.status != 200:
            raise RuntimeError('Camera did not return a JPEG (redirects are not followed)')
        image = r.read(s['max_image_bytes'] + 1)
        if len(image) > s['max_image_bytes'] or not image.startswith(b'\xff\xd8') or not image.endswith(b'\xff\xd9'):
            raise RuntimeError('Invalid or oversized JPEG')
        return image
    finally:
        conn.close()


def post_jpeg(s, image):
    conn, _, path = connection(s['receiver_url'], s['receiver_ip'])
    try:
        conn.putrequest('POST', path)
        conn.putheader('Content-Type', 'image/jpeg')
        conn.putheader('Content-Length', str(len(image)))
        conn.putheader('X-Captured-At-Ms', str(time.time_ns() // 1_000_000))
        if s['receiver_token']:
            conn.putheader('Authorization', 'Bearer ' + s['receiver_token'])
        conn.endheaders()
        for offset in range(0, len(image), 8192):
            chunk = image[offset:offset + 8192]
            conn.send(chunk)
            time.sleep(len(chunk) / s['pace_bytes_per_second'])
        r = conn.getresponse()
        if not 200 <= r.status < 300:
            raise RuntimeError('Snapshot receiver rejected POST (redirects are not followed)')
    finally:
        conn.close()


def run(s):
    if s['kind'] == 'rtsp':
        result = subprocess.run(video_command(s), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            raise RuntimeError('RTSP relay ended unsuccessfully')
        return
    while True:
        start = time.monotonic()
        post_jpeg(s, fetch_jpeg(s))
        time.sleep(max(0, start + s['interval_seconds'] - time.monotonic()))


if __name__ == '__main__':
    try:
        run(json.load(sys.stdin))
    except Exception:
        # Do not leak camera passwords, receiver tokens or URLs through the journal.
        sys.exit(1)
