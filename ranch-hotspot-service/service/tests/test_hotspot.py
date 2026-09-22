import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('hotspot', Path(__file__).parents[1] / 'hotspot.py')
h = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(h)


def config():
    return dict(country='US', wifi_mac='00:11:22:33:44:55', ssid='Ranch Test',
                wifi_password='test password;\\x', channel=6, lan_address='10.42.0.1/24',
                apn='test.apn', lte_username='', lte_password='', allow_roaming=False)


class FrameworkTests(unittest.TestCase):
    def test_template_requires_initialization(self):
        with self.assertRaises(ValueError):
            h.load(Path(__file__).parents[1] / 'config/hotspot.example.json')

    def test_input_validation(self):
        for key, value in [('country', 'USA'), ('wifi_mac', 'wlan1'),
                           ('wifi_mac', 'ff:ff:ff:ff:ff:ff'), ('ssid', '🐎' * 9),
                           ('wifi_password', 'short'), ('channel', True), ('channel', 36),
                           ('apn', 'x\n[evil]'), ('allow_roaming', 'false'),
                           ('lan_address', '8.8.8.1/24'), ('lan_address', '10.42.0.0/24')]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                h.validate({**config(), key: value})

    def test_profile_pair_routes_and_secrets(self):
        p = h.render(config(), '123456789012345')
        ap, lte = p['ranch-hotspot-ap.nmconnection'], p['ranch-hotspot-lte.nmconnection']
        self.assertIn('method=shared', ap)
        self.assertIn('ssid=82;97;110;99;104;32;84;101;115;116;', ap)
        self.assertIn('psk=test\\spassword;\\\\x', ap)
        self.assertIn('key-mgmt=wpa-psk', ap)
        self.assertIn('device-id=123456789012345', lte)
        self.assertIn('home-only=true', lte)
        self.assertIn('route-metric=50', lte)
        self.assertNotIn('interface-name=', ap + lte)

    def test_optional_auth_and_roaming(self):
        c = {**config(), 'lte_username': 'user name', 'lte_password': 'p;\\', 'allow_roaming': True}
        lte = h.render(c, '123456789012345')['ranch-hotspot-lte.nmconnection']
        self.assertIn('home-only=false', lte)
        self.assertIn('username=user\\sname', lte)
        self.assertIn('password=p;\\\\', lte)

    def test_identity_is_required(self):
        with self.assertRaises(ValueError):
            h.render(config(), '--')

    def test_modem_identity_and_lock(self):
        g = {'model': 'EM060K-GL', 'unlock-required': 'none', 'equipment-identifier': '123456789012345'}
        with patch.object(h, 'run', side_effect=['/org/freedesktop/ModemManager1/Modem/4',
                                               json.dumps({'modem': {'generic': g}})]):
            self.assertEqual(h.modem(), g['equipment-identifier'])
        for change in [{'unlock-required': 'sim-pin'}, {'model': 'different modem'}]:
            with patch.object(h, 'run', side_effect=['/org/freedesktop/ModemManager1/Modem/4',
                        json.dumps({'modem': {'generic': {**g, **change}}})]):
                with self.assertRaises(RuntimeError):
                    h.modem()

    def test_multiple_modems_refused(self):
        with patch.object(h, 'run', return_value='/org/freedesktop/ModemManager1/Modem/0\n/org/freedesktop/ModemManager1/Modem/1'):
            with self.assertRaises(RuntimeError):
                h.modem()

    def test_subnet_overlap(self):
        addresses = [{'ifname': 'eth0', 'addr_info': [{'local': '10.42.0.20', 'prefixlen': 24}]}]
        with patch.object(h, 'run', return_value=json.dumps(addresses)):
            with self.assertRaises(RuntimeError):
                h.no_subnet_conflict(config(), 'wlan1')
            h.no_subnet_conflict(config(), 'eth0')

    def test_atomic_write_and_configure_start_contract(self):
        with tempfile.TemporaryDirectory() as d:
            profiles, applied = Path(d) / 'profiles', Path(d) / 'applied'
            with patch.object(h, 'PROFILES', profiles), patch.object(h, 'APPLIED', applied), \
                 patch.object(h, 'check_hardware', return_value=('wlan7', '123456789012345')), \
                 patch.object(h, 'no_subnet_conflict'), patch.object(h, 'run', return_value='') as run:
                h.configure(config())
                self.assertEqual(len(list(profiles.glob('*.nmconnection'))), 2)
                h.start(config())
                self.assertTrue(any('ifname' in call.args and 'wlan7' in call.args for call in run.call_args_list))
                with self.assertRaises(RuntimeError):
                    h.start({**config(), 'ssid': 'Changed'})
                # NM owns the keyfiles and may add timestamps; that must not break boot.
                with (profiles / 'ranch-hotspot-ap.nmconnection').open('a') as f:
                    f.write('\n# changed by NM\n')
                h.start(config())

    def test_already_active_profiles_not_restarted(self):
        with tempfile.TemporaryDirectory() as d:
            applied = Path(d) / 'applied'
            applied.write_text(h.fingerprint(config(), '123456789012345'))
            with patch.object(h, 'APPLIED', applied), \
                 patch.object(h, 'check_hardware', return_value=('wlan7', '123456789012345')), \
                 patch.object(h, 'no_subnet_conflict'), \
                 patch.object(h, 'run', return_value=h.AP_UUID + '\n' + h.LTE_UUID) as run:
                h.start(config())
                self.assertFalse(any('up' in call.args for call in run.call_args_list))


if __name__ == '__main__':
    unittest.main()
