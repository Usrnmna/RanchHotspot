# Physical-device acceptance

Record the OS version, kernel, USB IDs, driver, modem firmware and carrier/APN used. Keep IMEI, SIM identifiers and passwords out of shared logs.

- [ ] Both USB devices enumerate reliably; Wi-Fi uses the intended USB driver and permanent MAC.
- [ ] ModemManager identifies EM060K, sees the SIM, reports unlocked state and registers on the carrier.
- [ ] Configure accepts AP mode and the chosen regulatory channel on the USB radio.
- [ ] Both `ranch-hotspot-ap` and `ranch-hotspot-lte` appear in `nmcli connection show --active`.
- [ ] With temporary internet uplinks removed, `ip -4 route get 1.1.1.1` selects the cellular data interface.
- [ ] A phone/laptop with its own cellular data disabled joins the SSID using the configured password and receives an address in the configured /24, plus the Pi gateway/DNS address.
- [ ] The client resolves a hostname and loads an HTTPS page over LTE. A Pi-only connection test does not prove NAT/DHCP/DNS for clients.
- [ ] Reboot the Pi with Ethernet removed. Wait for enumeration/registration retries; verify client access again.
- [ ] Disconnect/reconnect the USB modem, then the Wi-Fi dongle, separately. Verify NetworkManager recovers each connection and clients regain internet access. Hardware hangs may require a powered reset.
- [ ] Leave a client transferring traffic long enough to check power stability, temperature and intended coverage. Record results; none are pre-certified by this package.

## Troubleshooting

```bash
sudo python3 /opt/ranch-hotspot/hotspot.py status
sudo journalctl -b -u ranch-hotspot -u NetworkManager -u ModemManager --no-pager
sudo journalctl -b -k --no-pager
```

| Symptom | Check next |
| --- | --- |
| USB adapter absent | Power, cable, supported modem adapter and hardware seating |
| Wi-Fi ID differs | Exact BrosTrend product/revision; framework targets AX9L `0e8d:7961` |
| Missing Wi-Fi interface | `lsusb -t`, MediaTek firmware errors, installed kernel's `mt7921u` support |
| AP unsupported or channel restricted | Exact PHY modes, `iw reg get`, actual country, firmware and channel; do not bypass `no IR` restrictions |
| Modem absent in ModemManager | USB driver binding and modem firmware support; no competing modem dialer |
| SIM locked | Correct manual SIM unlock procedure; repeated PIN guesses can lock the SIM |
| Registered but LTE fails | Carrier APN/authentication, plan activation, permitted roaming, IPv4 data support and signal |
| LTE active but no internet | Carrier data service, actual default route, DNS; profile activation alone is not proof of internet |
| Client connects but gets no address | NetworkManager shared-mode logs, dnsmasq-base, conflicting DHCP/DNS services |
| Client has address but no internet | LTE route, existing firewall/VPN policy, sharing rules and subnet overlap |
| Changes do not apply | Run configure then restart; direct edits to generated profiles are unsupported |
| Unit is active (exited) during LTE outage | Normal for a completed initializer; inspect live NetworkManager/ModemManager state |

The installer includes QMI/MBIM diagnostic tools, but status relies on ModemManager so it does not contend for a live control port. Generated profiles contain secrets; do not share them or invoke `nmcli --show-secrets` in a support log.

## Validation performed during development

Python compilation and ten offline tests cover invalid setup values, protected profile rendering, correct route/sharing settings, modem identity and lock rejection, subnet overlap, configure-to-start state, changed settings, and avoiding unnecessary reconnects. External commands are mocked. No Linux service startup, package install, actual NetworkManager keyfile acceptance, hardware connection or throughput test was performed on the development Windows host.
