# Ranch LTE hotspot service framework

An initialization framework for a Raspberry Pi 4 Model B (2 GB), BrosTrend AXE3000 USB radio, and Quectel EM060K-GL on a USB modem adapter with a SIM slot. The programs are implemented; normal setup requires configuration values and hardware verification, not additional application coding.

**Validation:** Python checks and mocked integration tests pass on the development computer. Raspberry Pi OS installation, actual driver binding, SIM registration, AP operation, and client internet access have not been tested on the physical assembly. An unsupported hardware revision or modem firmware may require vendor support beyond initialization.

## Included files

| File | Purpose |
| --- | --- |
| `install.py` | Installs OS packages, copies program and service, preserves existing configuration |
| `hotspot.py` | Validates configuration, checks devices, writes network profiles, activates connections, reports status |
| `config/hotspot.example.json` | Configuration template; credentials are supplied locally on the Pi |
| `systemd/ranch-hotspot.service` | Boot initialization with a 30-second retry after failure |
| `tests/test_hotspot.py` | Offline configuration, profile, identity, subnet and lifecycle tests |
| `docs/HARDWARE.md` | Wiring, adapter compatibility, drivers and antenna checks |
| `docs/ACCEPTANCE.md` | Physical-device acceptance and troubleshooting steps |

## Operating design

```text
LTE antennas -> EM060K-GL + SIM -> USB -> ModemManager
                                              |
                                       NetworkManager
                                 LTE address/default route
                                 IPv4 sharing/NAT/DNS/DHCP
                                              |
                             USB BrosTrend Wi-Fi access point
                                              |
                                       phones/computers
```

NetworkManager owns both connections, routing and its private DHCP/DNS process. ModemManager manages modem registration and data sessions through the USB protocol exposed by the modem. No second modem dialer, standalone hostapd, standalone dnsmasq daemon, custom packet forwarder, or direct serial-port polling competes with those services. NetworkManager's `ipv4.method=shared` supplies forwarding and sharing. See [NetworkManager IPv4 settings](https://www.networkmanager.dev/docs/api/latest/settings-ipv4.html).

The starting profile uses WPA2-Personal with AES/CCMP, 2.4 GHz channel 6, and a private `10.42.0.1/24` LAN. It disables IPv6 on these two profiles to keep initial routing simple. LTE is marked metered, uses route metric 50, and prohibits roaming unless enabled in configuration. This does not impose a data cap. The radio is bound by permanent MAC and the modem by its discovered IMEI. The Pi's built-in Wi-Fi is not selected.

**Uplink behavior:** sharing follows the Pi's routing table. LTE is preferred over ordinary higher-metric routes, but another Ethernet, Wi-Fi or VPN route can carry traffic. Disconnect temporary internet uplinks for LTE-only operation and acceptance testing. There is no LTE-only firewall or kill switch in this framework. Wi-Fi clients share a trusted LAN and can reach services exposed by the Pi; this is not a guest-isolation design.

## First initialization

Use a fresh **64-bit Raspberry Pi OS Lite Bookworm or Trixie** installation on the Pi 4, with an updated distribution kernel (6.6 or newer is the intended baseline). Keep a local console or Ethernet administration path. Do not initialize over the USB radio that will become the hotspot.

1. Read `docs/HARDWARE.md` and assemble with power disconnected. Use an activated data SIM with its SIM PIN disabled beforehand using a phone or the carrier's supported procedure. This service never guesses or automatically submits a SIM PIN.
2. Give the Pi temporary Ethernet internet access to download OS packages and firmware. Update the OS normally and reboot after a kernel update. Copy this entire `service` folder to the Pi, for example to `/home/<your-user>/ranch-hotspot-service`.
3. In that folder on the **Pi**, run:

   ```bash
   sudo python3 install.py
   sudo python3 /opt/ranch-hotspot/hotspot.py status
   ```

   Installation enables NetworkManager and ModemManager. It does not enable the hotspot initializer on a first install. Reboot if firmware or kernel changes require it. The installer stops for active standalone hostapd, dnsmasq or dhcpcd services; resolve that existing networking setup or use a fresh OS image.
4. Edit the installed configuration:

   ```bash
   sudo nano /etc/ranch-hotspot/hotspot.json
   ```

   | Setting | Value to enter |
   | --- | --- |
   | `country` | Your actual ISO two-letter country code, such as `US` where applicable |
   | `wifi_mac` | Permanent address from `ethtool -P <USB-interface>`; status lists radio addresses |
   | `ssid` | Desired network name, at most 32 UTF-8 bytes |
   | `wifi_password` | Unique password, 8-63 printable ASCII characters; 16+ recommended |
   | `channel` | Locally permitted 2.4 GHz channel from 1 through 11 |
   | `lan_address` | Private gateway address with `/24`; change if it overlaps another network |
   | `apn` | Carrier's exact data APN |
   | `lte_username`, `lte_password` | Leave empty unless required by the carrier |
   | `allow_roaming` | Keep `false` unless the SIM needs and permits roaming |

   The example intentionally has unconfigured placeholders. JSON strings require escaping a literal backslash as `\\` and a double quote as `\"`. The tool validates values without printing passwords. No Python packages from pip are needed.
5. Apply and activate:

   ```bash
   sudo python3 /opt/ranch-hotspot/hotspot.py validate
   sudo python3 /opt/ranch-hotspot/hotspot.py configure
   sudo systemctl enable --now ranch-hotspot.service
   sudo python3 /opt/ranch-hotspot/hotspot.py status
   ```

   Configure checks the selected USB ID, AP support, permitted channel, LAN overlap, modem model and SIM lock state. It creates two root-only profiles and loads them into NetworkManager. Loading an autoconnect profile may immediately activate it and begin using SIM data. Boot initialization retries if enumeration or registration is delayed. NetworkManager handles later reconnection with unlimited autoconnect retries. A modem firmware hang that requires a physical reset is outside this initialization framework.
6. Disconnect Ethernet/other internet uplinks, join the new Wi-Fi from a client with its mobile data disabled, and complete `docs/ACCEPTANCE.md`. Reboot and repeat before considering the assembly operational.

The AP can remain available if LTE activation fails after the Wi-Fi profile has started. A completed systemd initializer remains `active (exited)`; this means initialization succeeded, not that cellular internet is continuously healthy.

## Changes, shutdown and removal

To change settings, use the local console or Ethernet, edit `/etc/ranch-hotspot/hotspot.json`, then run:

```bash
sudo python3 /opt/ranch-hotspot/hotspot.py configure
sudo systemctl restart ranch-hotspot.service
```

Configure stops the initializer, reloads the profiles, and disconnects any running instances so activation uses the new credentials and settings. It briefly interrupts Wi-Fi/LTE. It preserves unrelated connection profiles. Use the JSON configuration as the source of truth; edits made directly to generated profiles are not tracked. Reconfigure after replacing the modem or changing the Wi-Fi MAC.

To turn off this hotspot persistently, disable the initializer **and** NetworkManager autoconnect for both profiles:

```bash
sudo systemctl disable --now ranch-hotspot.service
sudo nmcli connection modify uuid 6d1a0ccd-0293-4fc2-94cf-6effceea601a connection.autoconnect no
sudo nmcli connection modify uuid c4dd601e-1232-468d-aad4-e9e8b25ec706 connection.autoconnect no
sudo nmcli connection down uuid 6d1a0ccd-0293-4fc2-94cf-6effceea601a
sudo nmcli connection down uuid c4dd601e-1232-468d-aad4-e9e8b25ec706
```

An already-disconnected profile may report that it is not active. To re-enable, run configure and `systemctl enable --now ranch-hotspot.service` again. Stopping only the systemd unit does not disconnect NetworkManager-owned connections.

For removal, run the shutdown commands, delete only these two UUIDs using `sudo nmcli connection delete uuid <UUID>`, then remove `/opt/ranch-hotspot/hotspot.py`, `/etc/systemd/system/ranch-hotspot.service`, and the two private files `/etc/ranch-hotspot/hotspot.json` and `/etc/ranch-hotspot/applied.sha256`. Run `sudo systemctl daemon-reload`. Keep shared OS packages and NetworkManager unless separately retiring the host. The service sets the runtime regulatory domain and turns Wi-Fi/WWAN radios on; restore your preferred country/radio settings if repurposing the Pi.

## Installed locations and dependencies

Application: `/opt/ranch-hotspot/hotspot.py`; configuration: `/etc/ranch-hotspot/hotspot.json` (directory mode 700, file mode 600); generated profiles: `/etc/NetworkManager/system-connections/ranch-hotspot-{ap,lte}.nmconnection` (mode 600); unit: `/etc/systemd/system/ranch-hotspot.service`. Profile files contain credentials and must not be shared. The applied-state hash detects changes to setup values and modem identity without storing another plaintext copy of credentials.

OS dependencies are installed by `install.py`: Python 3, NetworkManager, ModemManager, wpa_supplicant, dnsmasq-base, nftables, wireless firmware/regulatory database, iw, ethtool, rfkill, USB tools, IP tools, and QMI/MBIM diagnostic utilities. Firmware comes from `firmware-mediatek` where available, otherwise `firmware-misc-nonfree`. OS repositories must offer the applicable firmware package. Kernel drivers and firmware remain OS/vendor components rather than bundled binaries.

Run offline checks from this folder with `python3 -m unittest discover -s tests -v`. These tests do not substitute for Linux or hardware validation. Network profile serialization follows [NetworkManager's keyfile format](https://www.networkmanager.dev/docs/api/latest/nm-settings-keyfile.html); SSIDs are encoded as supported decimal byte lists to preserve their exact bytes.
