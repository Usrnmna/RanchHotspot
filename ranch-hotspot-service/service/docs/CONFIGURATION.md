# Settings and camera sources

Use this reference when changing a value. For a file-by-file reading order and function descriptions, see [Manual code review](MANUAL_REVIEW.md). The tables below describe the validators in this package; they do not establish that an address, device, camera, or carrier works.

## Which file should I edit?

| What you want to change | Edit here | How the change takes effect |
| --- | --- | --- |
| Wi-Fi name/password/channel, LAN address, carrier APN/authentication | Installed `/etc/ranch-hotspot/hotspot.json` | Validate, run `hotspot.py configure`, then restart `ranch-hotspot` |
| Camera address/path, receiver, snapshot interval, upload limit, control interface/port | Private `upload-gate.json` copied from the example | Validate, stop controller, copy to the installed config path, then start controller; commands below |
| Stream selection and duration for one session | Arguments to `send_magic.py` on the sending computer | Send a new command; it replaces the current session if accepted |
| Camera codec, resolution, frame rate, video bitrate | The camera's own settings | The relay copies the camera's video; it does not configure or encode it |
| Hardware support, retries, timeouts, or network policy embedded in Python/service files | See [advanced settings](MANUAL_REVIEW.md#advanced-settings-in-source-code) | Copy/reinstall changed files; reconfigure network profiles if their rendering changed |

`config/*.example.json` files are templates. Changing them does not update an installed Pi. The base installer preserves an existing `hotspot.json`; the gate installer replaces its installed JSON with the file passed to `--config`. Neither running service watches configuration files for live changes.

Keep the field names exactly as shown in the templates. JSON needs double quotes, lowercase `true`/`false`, no comments, and no trailing commas. Integers are unquoted whole numbers. Extra and missing fields are rejected. Save edited files as UTF-8 without a byte-order mark. Record your explanations in a separate private note instead of adding JSON comment fields.

## Hotspot settings

Template: [hotspot.example.json](../config/hotspot.example.json). Enforcement: `hotspot.validate()`; use: `hotspot.render()` and hardware checks.

| Field | Example/template value | Accepted value and effect |
| --- | --- | --- |
| `country` | `SET_COUNTRY` | Replace with two uppercase letters for the actual country, e.g. `US`. Sets the runtime radio regulatory domain; the driver must still allow the selected channel. Syntax validation alone cannot verify the country. |
| `wifi_mac` | `SET_USB_WIFI_MAC` | Replace with the permanent unicast MAC address, e.g. `00:11:22:33:44:55`. Discover with `ethtool -P`; it is not an interface name. The selected device must also pass the USB ID/AP-mode checks. |
| `ssid` | `RanchHotspot` | 1-32 UTF-8 bytes. Non-ASCII characters can use more than one byte each. |
| `wifi_password` | `SET_A_UNIQUE_PASSWORD` | Replace with 8-63 printable ASCII characters. Stored in the protected configuration and generated network profile. |
| `channel` | `6` | Integer 1-11; 2.4 GHz only. Hardware/regulatory checks happen during configure/start. |
| `lan_address` | `10.42.0.1/24` | Pi gateway host address in a private `/24`; cannot be its network or broadcast address. Must not overlap another interface's currently assigned subnet. |
| `apn` | `SET_CARRIER_APN` | Carrier-supplied APN, 1-100 letters/digits/dots/hyphens, starting with a letter/digit. Required even if the modem can discover an APN automatically. |
| `lte_username` | `""` | Carrier username, or empty string when not required. |
| `lte_password` | `""` | Carrier password, or empty string when not required. |
| `allow_roaming` | `false` | Boolean. `true` permits roaming; this is not a data quota or billing control. |

All text fields reject control characters and values starting with `SET_`. The example deliberately fails validation until the setup placeholders are replaced.

Changing `lan_address` requires updating the gate's `lan_network` and camera addresses as well. For example, gateway `10.43.0.1/24` corresponds to network `10.43.0.0/24`, with cameras such as `10.43.0.20`. These two configuration files are validated separately; the software does not cross-check them.

On the Pi, after editing the installed hotspot file:

```bash
sudo python3 /opt/ranch-hotspot/hotspot.py validate
sudo python3 /opt/ranch-hotspot/hotspot.py configure
sudo systemctl restart ranch-hotspot.service
```

`validate` only reads/checks JSON. `configure` changes the radio state and network profiles, stops the initializer, and disconnects its active profiles; NetworkManager can autoactivate the new profiles. Use a local console or separate management connection. If changing the subnet while the gate is installed, stop `ranch-upload-gate` first, update both configurations, and apply the gate file from the local console using the workflow below.

## Upload gate settings

Template: [upload-gate.example.json](../config/upload-gate.example.json). Enforcement: `upload_gate.validate()`.

| Field | Template value | Accepted value and effect |
| --- | --- | --- |
| `target` | `ranch-01` | 1-64 letters/digits/underscores/hyphens; must equal the sender's `--target`. |
| `wifi_interface` | `wlan1` | Actual hotspot data interface. Interface fields accept 1-15 letters/digits/underscores/hyphens. |
| `cellular_interface` | `wwan0` | Actual LTE data interface, distinct from Wi-Fi; neither may be `lo`. Signed commands must arrive here. A `/dev/cdc-wdm*` modem control path is not an interface name. |
| `lan_network` | `10.42.0.0/24` | Private IPv4 network in CIDR notation, with no host bits and a prefix no longer than `/30`. Match the hotspot's `/24` network in this project. |
| `management` | `[]` | Zero or more objects containing exactly `interface` and `network`. Allows Pi communication with that private network on a separate interface; does not allow forwarding. Example below. |
| `control_port` | `45991` | Integer 1024-65535, inbound UDP. Also set the sender's `--port` if changed. |
| `max_duration` | `900` | Integer 1-3600 seconds. Upper bound for each accepted start; fresh commands can start further sessions. |
| `clock_skew_seconds` | `30` | Integer 1-120 seconds, allowed past/future command age. Keep both machines' clocks accurate. |
| `upload_bytes_per_second` | `110000` | Integer 1000-115000 bytes/s. Firewall drops excess upload packets; this is not smooth bandwidth shaping. Default equals 880000 bits/s before additional link overhead. |
| `streams` | Two example entries | Object containing 1-32 named streams. Names use 1-64 letters/digits/underscores/hyphens. Only one stream runs at a time. |

An optional management entry looks like this (replace with your separate local interface/subnet):

```json
"management": [{"interface": "eth0", "network": "192.168.1.0/24"}]
```

The entry above is a field fragment, not a complete configuration. Its interface cannot be loopback, cellular, or the camera Wi-Fi interface. An empty list grants no additional management network. Gate activation restricts Pi internet traffic as well as camera forwarding; cellular DNS/NTP and package downloads are blocked by the generated rules.

## Camera and receiver fields

Each entry under `streams` uses the following common fields. Stream names such as `barn-video` are your labels, passed to the sender with `--stream`.

| Field | Meaning and rules |
| --- | --- |
| `kind` | Exactly `rtsp` for video or `stills` for individual JPEG snapshots. |
| `source_url` | Camera URL with a fixed usable IPv4 address inside `lan_network`. RTSP streams require `rtsp://`; stills require `http://` or `https://`. Use the camera's actual path. Hostnames, spaces and URL fragments are not accepted. |
| `receiver_url` | RTSP publish destination for video, HTTP(S) raw-JPEG POST endpoint for stills. Ports default to 554/80/443 for RTSP/HTTP/HTTPS. Set an explicit port when needed. |
| `receiver_ip` | Fixed IPv4 address outside the camera LAN. Cannot be multicast, unspecified, loopback or link-local. A private address can be used on a routed carrier/private network; the validator does not prove it is reachable. |

For RTSP, the hostname in `receiver_url` must be the exact `receiver_ip`. The receiver must accept RTSP publishing, not just playback. Source/receiver RTSP credentials may be placed in the URLs using percent-encoding as needed; video is copied over TCP with audio omitted. Configure the H.264 substream and bitrate on the camera itself. URLs passed to FFmpeg can be visible in local process arguments.

For stills, the receiver URL may have a DNS hostname: the connection uses `receiver_ip` while preserving HTTP Host and HTTPS certificate checks. Update the pinned IP when the receiver moves. URL-based receiver credentials are rejected; use `receiver_token`. The camera supports HTTP Basic credentials in its URL, but not digest authentication. HTTPS certificates must validate, including for camera URLs that use IP addresses.

Only stills entries require these additional fields; including them on RTSP entries fails validation:

| Field | Template value | Accepted value and effect |
| --- | --- | --- |
| `interval_seconds` | `30` | Integer 1-86400 seconds, minimum time between starting successive snapshot cycles. If fetch/upload takes longer, the next cycle begins immediately after completion. |
| `max_image_bytes` | `1048576` | Integer 1024-8388608 bytes (1 KiB-8 MiB); caps an individual image held in memory. Default is 1 MiB. |
| `receiver_token` | `""` | Printable ASCII bearer token, or empty string to omit authorization. Keep private. |

The stills receiver must accept a raw JPEG POST, not a multipart form, and return status 200-299. `X-Captured-At-Ms` is measured when the Pi starts posting, not a timestamp supplied by the camera. HTTP redirects and source/receiver errors end the worker; there is no retry queue or local recording.

To add a camera, copy one complete stream object of the required kind, give it a unique name, and replace its source/destination values. Keep 1-32 stream entries and correct commas between them. To remove a camera, remove its entire entry and its adjacent comma as needed. `203.0.113.10` and `uploads.example.com` in the template are documentation examples, not working receivers; the template passes structural validation with these addresses.

For an already installed gate, apply an edited private file from the copied service folder on the Pi. Run each step only if the preceding command succeeded:

```bash
python3 upload_gate.py validate --config upload-gate.json
sudo systemctl stop ranch-upload-gate.service
sudo install -m 600 upload-gate.json /etc/ranch-hotspot/upload-gate.json
sudo systemctl start ranch-upload-gate.service
sudo systemctl status ranch-upload-gate.service
```

Stopping the controller closes the current session but leaves the restrictive firewall installed. Starting it reads the new JSON and replaces the firewall with closed rules for that configuration; send a fresh command to start an upload. The key and replay database are unchanged. The boot lock reads the same installed JSON next boot. Use a local console when changing management or interface settings. A successful validation does not test camera URLs, certificates, receiver authentication, routing, or inbound cellular delivery.

For a first installation, follow [install and activate](UPLOAD_GATE.md#install-and-activate-on-the-pi), which uses `install_upload_gate.py`. The installer also downloads packages on every rerun; an active gate blocks public package servers, including via management Ethernet unless an APT proxy/mirror is reachable inside an allowed private subnet. Routine JSON changes use the commands above and need no package downloads.

## Sender arguments

Run on the controlling computer with `send_magic.py` and `upload_gate.py` in the same folder. These values select one operation; they do not edit Pi configuration.

| Argument | Default | Meaning |
| --- | --- | --- |
| `--host` | Required | Inbound-reachable cellular IPv4 address or DNS name of the Pi. |
| `--port` | `45991` | UDP destination port; sender accepts 1-65535, but it must match the gate's port. |
| `--target` | Required | Exact gate `target`. |
| `--key` | Required | Path to a protected copy of `upload-gate.key`, containing 64 hexadecimal characters. |
| `--stream` | Required | Exact configured stream name. |
| `--action` | `start` | `start` replaces any active stream; `stop` affects only the named active stream. |
| `--seconds` | `60` | Start duration, 1-3600 seconds and no greater than the Pi's `max_duration`. Ignored for stop, which sends duration zero. |

Example for the default target and stream (replace `CELLULAR_IPV4`):

```bash
python send_magic.py --host CELLULAR_IPV4 --target ranch-01 --key upload-gate.key --stream barn-video --seconds 60
```

A start window includes setup time. Sending prints no delivery acknowledgment. Run the sender again to create a fresh command after a lost packet; previously accepted commands cannot be replayed. Timestamps must increase across all senders/streams, so avoid issuing commands in the same millisecond. See [upload-gate operation](UPLOAD_GATE.md) for installation, clock setup and physical verification.
