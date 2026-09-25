"""Gate/worker contracts with fake commands, replay files and loopback HTTP.

Run from the service folder: python -m unittest discover -s tests -v
No camera, receiver, cellular service or Linux firewall is used. SnapshotTests
opens a local HTTP server; the remaining system/network effects are mocked.
"""
import copy
import http.server
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1]))
import upload_gate as g
import stream_worker as w

# Public fixture key and fixed Unix milliseconds; never use this key on a Pi.
KEY = bytes(range(32))
NOW = 1800000000000


def config():
    """Load a fresh example config; example endpoints are never contacted here."""
    return g.load(Path(__file__).parents[1] / 'config/upload-gate.example.json')


def command(**changes):
    """Build a synthetic unsigned command, with optional field overrides."""
    return dict(v=1, target='ranch-01', stream='barn-video', action='start',
                duration=60, issued_ms=NOW, nonce='ab' * 16, **{}) | changes


def packet(**changes):
    """Encode/sign a fixture command using the fixed test key."""
    return g.canonical(g.sign(command(**changes), KEY))


class AuthenticationTests(unittest.TestCase):
    """Signature/schema checks and durable rejection of reused/older commands."""
    def test_valid_signed_command(self):
        self.assertEqual(g.authenticate(packet(), KEY, config(), NOW), command())

    def test_reject_tampering_wrong_key_and_schema(self):
        with self.assertRaises(ValueError):
            g.authenticate(packet().replace(b'"duration":60', b'"duration":90'), KEY, config(), NOW)
        with self.assertRaises(ValueError):
            g.authenticate(packet(), b'x' * 32, config(), NOW)
        for change in ({'duration': 0}, {'duration': 901}, {'duration': True},
                       {'stream': '../x'}, {'stream': []}, {'target': 'another-ranch'},
                       {'action': 'exec'}, {'action': 'stop', 'duration': 1},
                       {'issued_ms': NOW - 30001}, {'issued_ms': NOW + 30001},
                       {'issued_ms': True}, {'nonce': 'x'}, {'v': True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                g.authenticate(packet(**change), KEY, config(), NOW)

    def test_malformed_duplicate_oversized_packets(self):
        for raw in (b'{}', b'[]', b'null', b'\xff', b'x' * 2049, b'{"v":1,"v":1}', b'{'):
            with self.subTest(raw=raw[:20]), self.assertRaises((ValueError, UnicodeError)):
                g.authenticate(raw, KEY, config(), NOW)

    def test_stop_command(self):
        self.assertEqual(g.authenticate(packet(action='stop', duration=0), KEY, config(), NOW)['action'], 'stop')

    def test_replay_and_reordering_persist_across_restarts(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'replays.sqlite'
            store = g.ReplayStore(path)
            store.consume(command())
            store.db.close()
            store = g.ReplayStore(path)
            for msg in (command(), command(nonce='cd' * 16, issued_ms=NOW - 1),
                        command(nonce='cd' * 16), command(issued_ms=NOW + 1)):
                with self.assertRaises(ValueError):
                    store.consume(msg)
            store.consume(command(nonce='cd' * 16, issued_ms=NOW + 1))
            store.db.close()


class ConfigurationTests(unittest.TestCase):
    """Setting limits, generated firewall text and FFmpeg argument expectations."""
    def test_reject_injection_and_wide_permissions(self):
        for field, value in [('wifi_interface', 'wlan1" accept'), ('cellular_interface', 'lo'),
                             ('lan_network', '0.0.0.0/0'), ('max_duration', 99999),
                             ('control_port', True), ('upload_bytes_per_second', 125000)]:
            c = config()
            c[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                g.validate(c)
        for field, value in [('source_url', 'rtsp://8.8.8.8/live'),
                             ('receiver_ip', '1.2.3.4; accept'),
                             ('receiver_url', 'rtsp://arbitrary.example/live')]:
            c = config()
            c['streams']['barn-video'][field] = value
            with self.assertRaises(ValueError):
                g.validate(c)

    def test_firewall_has_no_established_output_bypass_or_forwarding(self):
        r = g.rules(config(), 999)
        self.assertIn('chain forward { type filter hook forward priority -10; policy drop; }', r)
        self.assertIn('flags timeout;', r)
        self.assertIn('meta skuid 999 ip daddr . tcp dport @lease', r)
        self.assertNotIn('ct state', r.split('chain output')[1])
        self.assertNotIn('flush ruleset', r)
        self.assertNotIn('elements =', r)

    def test_kernel_lease_has_duration_and_fixed_receiver(self):
        with patch.object(g, 'nft_run') as nft:
            g.Firewall().grant(config()['streams']['barn-video'], 60)
        self.assertIn('203.0.113.10 . 8554 timeout 60s', nft.call_args.args[0])

    def test_rtsp_uses_tcp_both_ways_and_no_audio(self):
        args = w.video_command(config()['streams']['barn-video'])
        self.assertEqual(args.count('-rtsp_transport'), 2)
        self.assertEqual(args.count('tcp'), 2)
        self.assertIn('-an', args)
        self.assertIn('copy', args)


class LifecycleTests(unittest.TestCase):
    """Exercise a controller with injected clock/process/firewall dependencies."""
    def setUp(self):
        self.events = []
        self.now = 10
        self.process = Mock()
        self.process.poll.return_value = None
        self.fw = Mock()
        self.fw.close.side_effect = lambda: self.events.append('close')
        self.fw.grant.side_effect = lambda *args: self.events.append('grant')
        self.store = Mock()
        self.store.consume.side_effect = lambda msg: self.events.append('consume')
        self.launch = Mock(side_effect=lambda s: self.events.append('launch') or self.process)
        self.kill = Mock(side_effect=lambda p: self.events.append('kill'))
        self.ctl = g.Controller(config(), self.fw, self.store, self.launch, self.kill, lambda: self.now)

    def test_deadline_closes_firewall_before_killing(self):
        self.ctl.handle(command(duration=5))
        self.assertEqual(self.events, ['consume', 'close', 'grant', 'launch'])
        self.now = 14.99
        self.ctl.tick()
        self.assertIsNotNone(self.ctl.active)
        self.now = 15
        self.ctl.tick()
        self.assertEqual(self.events[-2:], ['close', 'kill'])
        self.assertIsNone(self.ctl.active)

    def test_switch_stops_old_before_grant_and_paces_stills(self):
        self.ctl.handle(command())
        self.events.clear()
        self.ctl.handle(command(stream='gate-stills'))
        self.assertEqual(self.events, ['consume', 'close', 'kill', 'grant', 'launch'])
        self.assertEqual(self.launch.call_args.args[0]['pace_bytes_per_second'], 82500)

    def test_failed_launch_closes_lease(self):
        self.launch.side_effect = OSError('failed')
        with self.assertRaises(OSError):
            self.ctl.handle(command())
        self.assertEqual(self.events[-1], 'close')
        self.assertIsNone(self.ctl.active)

    def test_worker_exit_revokes_immediately(self):
        self.ctl.handle(command())
        self.process.poll.return_value = 1
        self.ctl.tick()
        self.assertIsNone(self.ctl.active)

    def test_replay_does_not_extend_running_upload(self):
        self.ctl.handle(command())
        active = self.ctl.active
        self.events.clear()
        self.store.consume.side_effect = ValueError('replay')
        with self.assertRaises(ValueError):
            self.ctl.handle(command())
        self.assertEqual(self.ctl.active, active)
        self.assertEqual(self.events, [])

    def test_stop_only_matches_requested_stream(self):
        self.ctl.handle(command())
        self.ctl.handle(command(action='stop', duration=0, stream='gate-stills'))
        self.assertIsNotNone(self.ctl.active)
        self.ctl.handle(command(action='stop', duration=0))
        self.assertIsNone(self.ctl.active)


class SnapshotTests(unittest.TestCase):
    """Local JPEG HTTP transfer plus failure cases; no remote endpoints needed."""
    def test_local_http_fetch_and_pinned_post(self):
        jpeg = b'\xff\xd8test-image\xff\xd9'
        received = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(jpeg)
            def do_POST(self):
                received.append((self.headers, self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(201)
                self.end_headers()
            def log_message(self, *args):
                pass
        with http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_port
            s = dict(source_url=f'http://127.0.0.1:{port}/snapshot',
                     receiver_url=f'http://no-dns.invalid:{port}/upload', receiver_ip='127.0.0.1',
                     max_image_bytes=1024, receiver_token='test-token', pace_bytes_per_second=100000)
            try:
                image = w.fetch_jpeg(s)
                w.post_jpeg(s, image)
                self.assertEqual(received[0][1], jpeg)
                self.assertEqual(received[0][0]['Authorization'], 'Bearer test-token')
                self.assertEqual(received[0][0]['Host'], f'no-dns.invalid:{port}')
            finally:
                server.shutdown()
                thread.join()

    def test_oversized_image_and_redirect_rejected(self):
        s = config()['streams']['gate-stills']
        for status, data in [(302, b''), (200, b'x' * (s['max_image_bytes'] + 1)), (200, b'not jpeg')]:
            conn = Mock()
            conn.getresponse.return_value.status = status
            conn.getresponse.return_value.read.return_value = data
            with patch.object(w, 'connection', return_value=(conn, g.endpoint(s['source_url'], ('http',))[0], '/')):
                with self.assertRaises(RuntimeError):
                    w.fetch_jpeg(s)
            conn.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
