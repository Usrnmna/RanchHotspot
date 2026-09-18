# Ranch LTE Hotspot

A Raspberry Pi–based LTE hotspot intended to provide Wi-Fi around a rural horse ranch or barn. The LTE modem connects to a cellular network through two external antennas, and the Pi shares that connection with nearby devices over Wi-Fi.

> **Project status:** The [service framework](service/README.md) now includes an installer, initialization program, configuration template, boot service and offline tests. The complete assembly, carrier connection, and Wi-Fi access point have not yet been tested together.

## How it fits together

```text
Cell tower
   │
   ├── LTE antenna ── N-type/U.FL cable ──┐
   └── LTE antenna ── N-type/U.FL cable ──┤
                                         Quectel EM060K-GL
                                                │ M.2 B-key
                                         USB 3.0 adapter + SIM
                                                │ USB
                                         Raspberry Pi 4 Model B
                                                │ USB
                                         BrosTrend AXE3000 Wi-Fi
                                                ))) Ranch devices
```

The LTE antennas serve the **cellular modem**. The BrosTrend dongle serves **local Wi-Fi**; the LTE antennas do not extend Wi-Fi coverage.

## Hardware

| Qty | Component | Role |
| ---: | --- | --- |
| 1 | Raspberry Pi 4 Model B, 2 GB | Runs the hotspot and routes traffic |
| 1 | BrosTrend AXE3000 USB Wi-Fi dongle | Intended Wi-Fi access point radio |
| 1 | Quectel EM060K-GL M.2 B-key LTE module | Cellular internet connection |
| 1 | NGFF/M.2 B-key to USB 3.0 adapter with SIM slot | Connects the modem and SIM to the Pi |
| 2 | U.FL to female N-type RG178 adapter cables | Connect the modem's antenna ports to the external antennas |
| 2 | 9 dBi LTE antennas, 16.2 in tall, N-type, 698–2700 MHz | Cellular main and diversity/MIMO antennas |

### Also needed for a working installation

- An activated data SIM and a carrier plan compatible with the **EM060K-GL**, its supported bands, and the ranch's local coverage. Obtain the carrier's APN if it is not supplied automatically.
- A microSD card with Raspberry Pi OS and a reliable Raspberry Pi 4 power supply. The modem and Wi-Fi adapter add USB power demand; check the adapter's power requirements and use a suitable powered USB hub if the Pi cannot power both reliably.
- A protected enclosure, appropriate antenna mounts, weather-rated cable routing, and surge/lightning protection suited to the installation. Keep electronics dry and place antennas where cellular signal is usable.
- A phone or computer for initial setup, preferably with temporary Ethernet access for updates and troubleshooting.

## Assembly

1. Power everything off. Insert the SIM into the adapter in the orientation marked on its slot, and secure the EM060K-GL in the M.2 B-key socket.
2. Verify the pigtails' exact miniature connector series against the modem before attaching them; a generic U.FL label does not establish compatibility. See [hardware checks](service/docs/HARDWARE.md). Attach correctly mating leads to **ANT_MAIN** and **ANT_DRx/GNSS** without forcing them.
3. Attach each lead's female N-type end to an LTE antenna. Mount the antennas securely, route cables away from pinch points and water entry, and avoid tight bends.
4. Connect the modem adapter and BrosTrend dongle to USB ports on the Pi. Connect power only after the antenna leads are attached.

Check the modem's hardware guide before final mounting, especially its antenna-port labels and any placement or grounding requirements. The antenna's advertised 9 dBi gain and frequency range do not guarantee coverage at the site.

## Software bring-up

1. Install a current Raspberry Pi OS release, set the Wi-Fi country, create an admin account, and update the system. Raspberry Pi OS uses NetworkManager on current releases.
2. Confirm that both USB devices appear with `lsusb`. Identify the Wi-Fi interface with `nmcli device status` and check whether the modem appears in `mmcli -L` after installing or enabling ModemManager as needed.
3. Establish the LTE data connection first. In NetworkManager, create a mobile-broadband connection for the SIM and enter the carrier APN if required. Confirm that the Pi can reach the internet **through LTE** before enabling internet sharing.
4. Check whether the BrosTrend interface supports access point mode on the installed kernel and firmware (`iw list` shows supported interface modes). Configure a password-protected Wi-Fi hotspot on that interface using NetworkManager's hotspot feature. If the USB adapter cannot operate in access point mode, the Pi's built-in Wi-Fi is a possible alternative for initial testing.
5. Connect a phone or laptop to the hotspot. Verify that it receives an IP address, can open an internet site, and continues to work after a Pi reboot.

NetworkManager can provide the local address, DHCP, and connection sharing for a hotspot. Interface names, modem protocol, APN, and Wi-Fi band depend on the actual devices and carrier; record the working values before turning this into an unattended installation.

## Ranch deployment notes

- Test cellular signal and throughput at the intended mounting location before drilling or running permanent cable. LTE performance varies with tower load, terrain, building materials, antenna orientation, and cable loss.
- Start with 2.4 GHz Wi-Fi when coverage across a barn matters more than peak speed. The USB adapter's supported access point bands and local regulatory settings need to be checked on the installed system.
- A single Wi-Fi radio may not cover multiple buildings, stalls, or paddocks. Add properly placed access points or a wired link if measurements show gaps.
- Keep the Pi and modem in a ventilated, protected location away from animals, dust, moisture, and direct heat. Secure cables against chewing and accidental pulls.
- Change default credentials, use a strong Wi-Fi password, keep the OS updated, and monitor data-plan usage.

## Verification checklist

- [ ] SIM activates and modem registers on the intended carrier
- [ ] LTE connection survives a reboot
- [ ] Intended Wi-Fi radio supports access point mode
- [ ] Clients get an address and internet access through LTE
- [ ] Signal and coverage are acceptable at the barn and required outdoor areas
- [ ] Power, enclosure, antenna mounting, and cable protection are suitable for continuous use

## References

- [Quectel EM060K series product page and hardware documentation](https://www.quectel.com/product/lte-a-em060k-series/)
- [Raspberry Pi wireless configuration documentation](https://www.raspberrypi.com/documentation/computers/configuration.html)
- [BrosTrend Linux support and installation guidance](https://linux.brostrend.com/)

Start with the [service setup guide](service/README.md) for the implemented framework. The package has passed offline checks; physical-device acceptance remains necessary.
