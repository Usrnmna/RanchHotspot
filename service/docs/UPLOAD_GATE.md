# Timed camera uploads over cellular

For individual field types, units and editing workflows, use [Settings and camera sources](CONFIGURATION.md). To trace functions and find advanced values embedded in code, use [Manual code review](MANUAL_REVIEW.md).

This extension runs on the **Raspberry Pi 4 that routes through the Quectel EM060K-GL**. It does not replace or flash modem firmware. The Pi pulls a configured local camera feed only after receiving an authenticated UDP command through its cellular data interface.

## Behavior

1. Start with no camera uploads. Direct Wi-Fi-device forwarding is blocked for IPv4 and IPv6.
2. Receive a signed command naming a configured stream and a duration in seconds.
3. Pull that camera on the Wi-Fi LAN and open one temporary cellular permission for the relay account, receiver IPv4 address and TCP port.
4. Stop and revoke permission at expiry, on a signed stop, on a relay failure, or when another stream is selected. Only **one stream is active at a time**.

The kernel permission expires independently of Python. Existing TCP connections do not bypass expiry. Normal shutdown revokes permission immediately; an abrupt controller crash leaves at most the remainder of the already-authorized window. Systemd kills relay processes when the controller exits and restarts with all permissions closed. Packets already queued in the modem/network can arrive after the cutoff; no software firewall can recall transmitted data.

An early boot lock is a required prerequisite for NetworkManager, including its automatic profile activation. If the lock fails, NetworkManager cannot start normally. The lock's stop operation deliberately leaves the firewall present. The controller resets all permissions on every start and does not restore old uploads.

## Cellular reachability and idle traffic

The receiving address must be reachable **inbound over cellular IPv4**, and the carrier must permit UDP port `45991` (or the configured port). A public/static-address APN or a carrier-routed private network can provide this. Ordinary SIM service behind carrier-grade NAT generally cannot receive this unsolicited packet. A home-router port-forward does not fix carrier NAT. This package does not silently create a tunnel or poll a cloud service. SMS and outbound broker/tunnel control are not implemented; they require a different control transport if your APN cannot deliver inbound packets.

There are no application-level acknowledgments or keepalives. Kernel output filtering blocks other Pi internet uploads too, including forwarded DNS, NTP, software updates and cloud agents. IPv4 cellular DHCP broadcasts are allowed for carriers that need them. Cellular registration/signaling, inbound traffic, and carrier accounting are outside this application gate, so this is not a guarantee of zero billed idle data.

Use local console access during setup. Optional `management` entries permit Pi communication with a specific private subnet on a separate interface, for example `[{"interface":"eth0","network":"192.168.1.0/24"}]`. They do not permit client forwarding or arbitrary internet access. The Wi-Fi LAN remains available for DHCP and camera access. DNS-based camera/receiver discovery is unnecessary: sources use fixed LAN IPv4 addresses, and receiver connections are pinned to configured IPv4 addresses.

## Configure cameras and the receiver

Copy `config/upload-gate.example.json` to a private editable file. All included camera paths and receiver addresses are examples; `203.0.113.10` and `uploads.example.com` are not working destinations. Discover the **Linux data-interface names** with `nmcli device status` and `ip -4 route`; the modem's `/dev/cdc-wdm*` control node is not its data interface. Interface changes fail closed and require reconfiguration.

| Setting | Meaning |
| --- | --- |
| `target` | Unique router name, also supplied by the command sender |
| `wifi_interface`, `cellular_interface` | Actual Wi-Fi AP and cellular data interfaces |
| `lan_network` | Existing hotspot subnet, normally `10.42.0.0/24` |
| `management` | Optional separate local administration interfaces/subnets; empty by default |
| `control_port` | Inbound cellular UDP port, default 45991 |
| `max_duration` | Maximum per-command window, default 900 seconds; hard maximum 3600 |
| `clock_skew_seconds` | Accepted sender/receiver clock difference, default 30 seconds |
| `upload_bytes_per_second` | Aggregate IP upload policing, default 110000 bytes/s (880 kbit/s), maximum 115000 |
| `streams` | Map of user-selected names to fixed camera/receiver definitions |

Configure fixed camera addresses, either on the cameras or through properly reserved DHCP leases. Ensure `lan_network` agrees with the hotspot profile. Use a dedicated Pi without alternative bridges, tunnels, containers, hardware forwarding or nftables flowtable offload. The installer refuses existing nftables flowtables. Do not enable offload or another manager that removes/replaces these rules afterward. Only the trusted relay worker should run as the `ranch-stream` account.

### Live RTSP video

`barn-video` pulls `source_url` using RTSP with RTP interleaved over TCP. FFmpeg publishes it to `receiver_url`, also with interleaved TCP. The receiver must be an **RTSP server that accepts publishing/ANNOUNCE**, such as a configured MediaMTX server; an arbitrary video viewer or web upload URL will not work. Configure a publish path and authentication on that server. The RTSP receiver URL must contain the exact `receiver_ip`, for example `rtsp://user:password@YOUR_SERVER_IP:8554/barn`.

Set the camera's H.264 substream to approximately **600–750 kbit/s**, leaving headroom below the default 880 kbit/s IP policer. The worker copies video without transcoding and omits audio. It neither changes camera encoding settings nor guarantees compatibility with every camera codec. The policer drops excess packets rather than smoothly shaping them, so an oversized stream can stall or lose quality. A 16000-byte token-bucket burst is permitted; cellular/link-layer overhead is additional.

Camera RTSP authentication can be supplied in `source_url` as supported by FFmpeg. URLs require percent-encoding special characters in credentials. This implementation uses plain RTSP/TCP for video, which does not encrypt media or credentials on the transport; the HMAC protects control commands, not video. FFmpeg URLs can appear in local process arguments, so keep local Pi accounts trusted.

### Time-lapse stills

`gate-stills` performs one HTTP(S) GET from the camera and one HTTP(S) POST to the receiver each `interval_seconds`. The camera endpoint must return an individual JPEG, not an HTML page or MJPEG stream. Camera HTTP Basic authentication in the URL is supported; digest-only snapshot endpoints are not.

The remote service must accept:

- `POST` to the configured URL with a raw JPEG body and `Content-Type: image/jpeg`.
- Optional `Authorization: Bearer <receiver_token>` and `X-Captured-At-Ms` headers.
- A success response with HTTP status 200–299.

This is not a multipart form upload or a particular cloud-provider API. Use an existing compatible ingestion endpoint or implement one on your chosen server. HTTPS validates the normal certificate and hostname while connecting to `receiver_ip`, so it does not require Pi cellular DNS. Set that IP to the server's actual address and update it when the server moves. HTTPS camera certificates must also validate normally; verification is never disabled.

Images are limited to `max_image_bytes` and kept only in memory. POST bodies are paced below the firewall rate. There is no disk queue or catch-up upload after expiry. Redirects are rejected; a source/receiver error ends the relay and closes its permission. If an image takes longer than the requested interval, the next starts after completion. The last image can be incomplete if the authorization expires during its POST; the receiver should commit only complete bodies.

## Install and activate on the Pi

First bring up the existing hotspot using `service/README.md` and confirm the camera addresses. Baseline internet checks are commissioning checks **before the gate is enabled**. Installation of the gate changes the hotspot into a camera LAN with controlled uploads. Use temporary Ethernet for package downloads and a console for activation; include your private management subnet if you intend to keep Ethernet SSH access.

From the copied `service` folder:

```bash
cp config/upload-gate.example.json upload-gate.json
chmod 600 upload-gate.json
nano upload-gate.json
python3 upload_gate.py validate --config upload-gate.json
sudo python3 install_upload_gate.py --config upload-gate.json
```

The installer installs Python, nftables and FFmpeg; creates an unprivileged `ranch-stream` account; validates the rules with the Pi's nftables parser; generates a random 256-bit control key; and activates the lock and controller. Existing keys and the replay database are preserved when reinstalling. The existing ungated hotspot may use cellular data until activation completes; disconnect clients during migration if that matters.

Installed files:

- `/opt/ranch-hotspot/{upload_gate,stream_worker,send_magic}.py`
- `/etc/ranch-hotspot/upload-gate.json` and `upload-gate.key`, root-only
- `/var/lib/ranch-upload-gate/commands.sqlite3`, persistent replay/order history
- `/etc/systemd/system/ranch-upload-{lock,gate}.service`
- `/etc/systemd/system/NetworkManager.service.d/ranch-upload-gate.conf`

Keep both machines' clocks accurate. The Pi blocks cellular NTP, so set its clock locally or synchronize through an allowed local management network **before** sending commands. Rebooted Pis without a reliable clock may reject commands until corrected. Do not delete the replay database to work around time errors. Anyone with the shared key can authorize any configured stream; protect it like a password and do not include it in tickets or logs.

## Send a magic packet

Securely copy the key to your controlling computer over the local administration path. Copy `send_magic.py` and `upload_gate.py` beside it; they use only Python's standard library and work on Windows or Linux. Do not expose the key in command arguments or email it.

Authorize one minute of video (replace the uppercase host placeholder):

```bash
python send_magic.py --host CELLULAR_IPV4 --target ranch-01 --key upload-gate.key --stream barn-video --seconds 60
```

Authorize five minutes of snapshots:

```bash
python send_magic.py --host CELLULAR_IPV4 --target ranch-01 --key upload-gate.key --stream gate-stills --seconds 300
```

Stop that stream early:

```bash
python send_magic.py --host CELLULAR_IPV4 --target ranch-01 --key upload-gate.key --stream gate-stills --action stop
```

The window starts when the Pi accepts the command, including connection/setup time. A fresh start replaces the current stream and restarts the relay. A stop only affects the named active stream. UDP transmission does not prove delivery: inspect the Pi journal or the receiver. To retry after a lost packet, run the sender again to generate a fresh signed command. Commands with identical or older timestamps than the last accepted command are rejected, so coordinate multiple senders and avoid same-millisecond submissions.

Each JSON command contains `v`, `target`, `stream`, `action`, `duration`, `issued_ms`, `nonce`, and `signature`. The signature is hexadecimal HMAC-SHA256 over all fields except `signature`, encoded as ASCII JSON with sorted keys and compact separators. Strict field validation, a freshness window, a 128-bit nonce and persistent timestamp/nonce history prevent modified, replayed and reordered commands. Commands cannot supply arbitrary URLs, IP addresses, shell commands or firewall rules. They can repeatedly renew access with fresh commands; `max_duration` is a per-command cap, not a daily quota.

## Operating and testing

```bash
sudo systemctl status ranch-upload-lock ranch-upload-gate
sudo journalctl -u ranch-upload-gate -f
sudo nft list table inet ranch_upload_gate
```

To change configuration on an installed Pi, edit and validate your private source JSON, stop the controller, copy that file to `/etc/ranch-hotspot/upload-gate.json` with mode 600, and start the controller. Follow the [step-by-step configuration update](CONFIGURATION.md#camera-and-receiver-fields), using a local console when changing interfaces or management access. Startup reads the new JSON and replaces the firewall with all upload permissions closed. Stopping the controller leaves the restrictive firewall in place. Avoid a global `nft flush ruleset`: that would remove protection.

Routine configuration changes do not require rerunning the installer. Both installers perform package downloads; an already active gate blocks public package servers, including through management Ethernet. An installer rerun requires a working APT proxy/mirror inside an allowed private management subnet or another deliberately planned maintenance procedure.

To remove the gate deliberately, first disconnect LTE and disable the original hotspot's autoconnect as described in the base guide. Then disable both gate units, remove the NetworkManager drop-in, reload systemd, and delete only `table inet ranch_upload_gate` with nftables. Removing the firewall re-enables the underlying network policy; do not do it while expecting uploads to remain blocked. Preserve the key/database until you intentionally retire that key. Reusing an old key with deleted replay history weakens replay protection.

Development verification: `python -m unittest discover -s service/tests -v` from the project root. Tests cover signed commands, malformed input, replay across restarts, switch/stop/expiry behavior, worker failure, rule generation, and a real local HTTP JPEG GET/POST with pinned addressing. Linux integration tests are separately provided as `sudo python3 service/tests/linux_gate_integration.py`; they require Linux, nftables and iproute2 and use temporary network namespaces.

On the physical Pi, verify all of these before unattended operation:

- With no authorization, phones cannot browse or upload over LTE, including IPv6; camera LAN access still works.
- Pi DNS/NTP/internet attempts are blocked; only configured local management remains reachable.
- A signed command sent from outside the local LAN reaches the cellular interface. The same command sent over Wi-Fi is ignored.
- Wrong keys, wrong target IDs, expired messages, repeated packets, and unknown streams never open a lease.
- For each real camera and receiver, start for 10 seconds and observe receiver-side arrival/cutoff, including an already-established TCP stream. Try an early stop and a stream switch.
- Kill the controller during a stream, unplug/reconnect LTE, and reboot. Verify closed startup and no uploads beyond the original authorization window. Never enable flow offload afterward.
- Check receiver bitrate, modem data accounting, CPU/temperature and camera reconnection over an extended run.

The development machine is Windows: no physical Pi/modem has been flashed or configured, and Linux firewall behavior, service startup, FFmpeg interoperability, carrier reachability and on-air throughput require the above target checks.

## Implementation references

- [Netfilter nftables manual](https://netfilter.org/projects/nftables/manpage.html): timed sets, hook policies and rule syntax.
- [FFmpeg RTSP protocol documentation](https://ffmpeg.org/ffmpeg-protocols.html#rtsp): TCP interleaving and RTSP publishing.
- [MediaMTX FFmpeg publishing](https://mediamtx.org/docs/publish/ffmpeg): compatible receiver setup.
- [ModemManager LTE IP connectivity](https://modemmanager.org/docs/modemmanager/ip-connectivity-setup-in-lte-modems/): host-side network configuration versus modem signaling.
