#!/usr/bin/env python3
"""Relay one camera stream during a controller-authorized upload session.

Read this file after upload_gate.py: that controller validates the stream, opens
the timed firewall permission, and starts this worker as the ranch-stream user.
The selected stream arrives as JSON on standard input; do not run this program
as a standalone way to grant internet access.

Adjust camera and receiver URLs, JPEG size, and snapshot interval in
/etc/ranch-hotspot/upload-gate.json (template: config/upload-gate.example.json).
The controller adds pace_bytes_per_second from upload_bytes_per_second; it is
not a field to add to the JSON file. RTSP bitrate/resolution are camera settings:
this worker copies compressed video and does not resize or re-encode it.
"""
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
    """Return FFmpeg arguments for the validated RTSP stream dictionary ``s``.

    Copy only the first video track, omit audio, and use TCP at both ends.
    This function builds arguments; run() actually starts the child process.
    URLs become FFmpeg process arguments, so embedded credentials may be visible
    to users with sufficient local process-inspection access.
    """
    # Camera must provide H.264 below the configured uplink budget. No Pi encoding.
    # FFmpeg's RTSP input timeout below is in microseconds: 5,000,000 = 5 seconds.
    return ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-rtsp_transport', 'tcp', '-timeout', '5000000', '-i', s['source_url'],
            '-map', '0:v:0', '-an', '-c:v', 'copy', '-f', 'rtsp',
            '-rtsp_transport', 'tcp', s['receiver_url']]


def connection(url, fixed_ip=None):
    """Return (HTTP connection, parsed URL, request path) without connecting yet.

    fixed_ip selects the destination address while retaining the URL hostname
    for HTTPS certificate verification/SNI and HTTP Host. The 10-second timeout
    applies to socket operations, not a deadline for the entire upload session.
    """
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
    """Fetch one snapshot into memory; return JPEG bytes or raise on failure.

    s uses source_url and max_image_bytes from the selected stream configuration.
    URL-encoded camera credentials are sent as HTTP Basic authentication when
    present. No redirects are followed and no image is written to disk.
    """
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
        # Read one extra byte to distinguish an exact-size image from an overflow.
        image = r.read(s['max_image_bytes'] + 1)
        # Check JPEG boundary markers only; this is not a full image decode.
        if len(image) > s['max_image_bytes'] or not image.startswith(b'\xff\xd8') or not image.endswith(b'\xff\xd9'):
            raise RuntimeError('Invalid or oversized JPEG')
        return image
    finally:
        conn.close()


def post_jpeg(s, image):
    """POST JPEG bytes to the configured receiver, then require a 2xx response.

    receiver_ip fixes the destination without cellular DNS; receiver_token is an
    optional Bearer token. pace_bytes_per_second controls JPEG body pacing only;
    the controller/firewall manages the allowed destination and session lifetime.
    """
    conn, _, path = connection(s['receiver_url'], s['receiver_ip'])
    try:
        conn.putrequest('POST', path)
        conn.putheader('Content-Type', 'image/jpeg')
        conn.putheader('Content-Length', str(len(image)))
        # This is upload-start Unix time in milliseconds, not a camera timestamp.
        conn.putheader('X-Captured-At-Ms', str(time.time_ns() // 1_000_000))
        if s['receiver_token']:
            conn.putheader('Authorization', 'Bearer ' + s['receiver_token'])
        conn.endheaders()
        # Send at most 8 KiB at a time; bytes / (bytes per second) gives seconds.
        # Network/TLS overhead is additional to this application-level pacing.
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
    """Run one RTSP child or repeat snapshots until failure/controller shutdown.

    s is the controller-validated stream dictionary, including its derived pace.
    There is no local retry: an error exits the worker and the controller closes
    permission. Snapshot interval_seconds is the minimum start-to-start interval;
    if fetching/uploading takes longer, the next cycle starts immediately.
    """
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
        # The controller closes the private pipe after writing one JSON object.
        run(json.load(sys.stdin))
    except Exception:
        # Do not leak camera passwords, receiver tokens or URLs through the journal.
        sys.exit(1)
