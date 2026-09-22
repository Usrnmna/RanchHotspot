# Hardware and driver bring-up

| Component | Connection and check |
| --- | --- |
| Pi 4 Model B, 2 GB | 64-bit Raspberry Pi OS Lite; microSD; stable power; console or Ethernet for administration |
| BrosTrend AXE3000 | USB to Pi; expected AX9L USB identity `0e8d:7961`; check actual label and `lsusb` |
| Quectel EM060K-GL | M.2 B-key socket on a cellular-modem USB adapter, with active SIM |
| USB 3.0 modem adapter | Must carry modem USB data, SIM signals, supply regulation and required modem power/control pins; an M.2 storage enclosure is not equivalent |
| Two RF pigtails | Modem MAIN and diversity/GNSS ports to the N-type antennas, after confirming exact miniature connector fit |
| Two 9 dBi 698-2700 MHz antennas | Cellular path only; these do not extend the USB Wi-Fi radio's coverage |

Power off before inserting the module, SIM or RF connectors. Support the modem mechanically. Check the adapter's supported module list, SIM-slot orientation and power instructions. Use short, good USB cables; use appropriate external USB power if the combined radios exceed available power. Repeated USB disconnects under load should be investigated before changing software.

**Pigtail fit is not yet confirmed.** Quectel's hardware guide specifies 2 mm by 2 mm antenna receptacles and mating dimensions. A cable sold simply as “U.FL” must not be assumed to fit; verify the exact mating series (including whether MHF4 is required) with the module/cable supplier and replace incompatible leads. Do not force connectors. For this module the LTE connections are `ANT_MAIN` and `ANT_DRx/GNSS`. The antennas' stated 698-2700 MHz coverage excludes LTE B71 and higher-frequency bands such as B42/B43/B48; do not expect those antennas to cover every supported modem band. See [Quectel hardware design, sections 5.1 and 5.4](https://quectel.com/content/uploads/2024/02/Quectel_EM060K_SeriesEM120K-GL_Hardware_Design_V1.1.pdf).

Keep RG178 pigtails short to limit RF loss. Confirm that the antennas have mating male N-type ends. Protect outdoor connectors from water and strain. Mount and space antennas according to their supplier's instructions and test signal at the intended site.

## Wi-Fi driver path

BrosTrend maps AXE3000/AX9L to USB `0e8d:7961` and lists in-kernel support from Linux 5.18. The Linux module name is `mt7921u` (the vendor support table calls the adapter driver `mt7921au`). Use the distribution's kernel and MediaTek firmware. This framework deliberately verifies the USB ID rather than silently choosing the Pi's built-in radio. [BrosTrend USB identities](https://linux.brostrend.com/advanced/usb_modeswitch/), [supported kernels](https://linux.brostrend.com/supported-distributions/), [Linux driver build definition](https://github.com/torvalds/linux/blob/master/drivers/net/wireless/mediatek/mt76/mt7921/Makefile).

On the Pi:

```bash
lsusb
lsusb -t
iw dev
iw list
nmcli device status
```

Find the USB radio's interface from these outputs; run `sudo ethtool -P <interface>` for its permanent address and `sudo ethtool -i <interface>` for its driver. Copy that permanent address to the configuration. `iw phy <phy-name> info` must show AP mode and permit initiation on the chosen channel. Marketing support for Wi-Fi 6E or client mode does not establish usable AP support. The framework uses 2.4 GHz only; it does not configure 5/6 GHz or DFS.

If the ID differs or the driver is missing, first verify the physical revision and use its supported kernel/firmware. Do not substitute guessed IDs or copy unrelated Realtek driver installers. The framework reports the mismatch; vendor changes to hardware cannot be resolved through an APN or SSID setting.

## LTE driver path

The kernel discovers the modem's USB composition. Typical paths use `option` for AT serial ports and `cdc_mbim` or `qmi_wwan` with `cdc_wdm` for the control/data interfaces. ModemManager selects the exposed supported protocol; NetworkManager requests the bearer. The exact port count and protocol depend on the modem firmware. The [Quectel EM060K specification](https://quectel.com/content/uploads/2024/03/Quectel_EM060K_Series_LTE-A_Module_Specification_V1.4-2.pdf) lists QMI/MBIM support.

```bash
sudo mmcli -L
sudo mmcli -m <modem-index>
sudo journalctl -b -u ModemManager --no-pager
sudo journalctl -b -k --no-pager
```

Use the index returned by `mmcli -L`; never assume index 0 or a fixed `/dev/ttyUSB` number. Check model, SIM presence, lock status and registration. Resolve locked PIN/PUK states manually with the carrier's correct information. If USB enumerates but no modem appears, inspect drivers, firmware and ModemManager support. Do not run Quectel-CM, raw QMI/MBIM session commands or AT initialization concurrently with ModemManager. The framework does not flash firmware, change USB composition, lock LTE bands or manipulate adapter-specific GPIO/power pins.
