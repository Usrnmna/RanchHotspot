# Manual code review guide

Start with [Settings and camera sources](CONFIGURATION.md) for everyday adjustments. This guide explains where to read or change program behavior. Every application function has a docstring immediately under its `def` line describing its purpose and important effects. Search for the function name in your editor; these names stay useful when line numbers shift.

## Folder ownership and reading order

In the development workspace, edit the top-level `service/` folder. `ranch-hotspot-service/service/` is its distributable copy, and `ranch-hotspot-service.zip` contains that package. They are synchronized for this review but do not synchronize automatically after future edits. On the Pi, installers copy executable files into `/opt/ranch-hotspot/`; changes to your downloaded source folder do not update those installed files automatically.

Read in this order:

1. [Service README](../README.md): operating design and baseline installation.
2. [Settings reference](CONFIGURATION.md) alongside the two files in [config](../config/): values you can change without editing Python.
3. [hotspot.py](../hotspot.py): begin with `main()`, then follow `validate()`, `configure()` and `start()`.
4. [upload_gate.py](../upload_gate.py): begin with `main()` and `serve()`, then read authentication, controller and firewall functions.
5. [stream_worker.py](../stream_worker.py) and [send_magic.py](../send_magic.py): how media moves and commands are sent.
6. The installers, [systemd files](../systemd/) and tests: installation effects, boot order and expected behavior.

## Complete file map

All paths below are relative to the `service/` folder unless noted.

| File | Role / what to inspect |
| --- | --- |
| Project-level `README.md` | Hardware overview and entry links; not an executable program. |
| `README.md` | Baseline install, configure/start/stop and removal procedures. |
| `hotspot.py` | JSON validation, hardware selection, NetworkManager profile generation and initialization. |
| `install.py` | Pi/OS checks, package installation, base program/config/unit copies. Preserves an existing hotspot JSON. |
| `upload_gate.py` | Gate config checks, signed commands, replay database, firewall permissions and worker lifecycle. |
| `stream_worker.py` | Unprivileged RTSP relay or in-memory JPEG fetch/POST. Invoked by the controller. |
| `send_magic.py` | Sends one signed UDP start/stop command from a separate computer. |
| `install_upload_gate.py` | Installs packages, account, config, signing key and boot units, then activates the gate. |
| `config/hotspot.example.json` | Base settings template; `SET_` placeholders must be replaced. |
| `config/upload-gate.example.json` | Gate/source/receiver template; example destinations pass validation but are not live endpoints. |
| `systemd/ranch-hotspot.service` | Boot initializer; retries failures every 30 seconds. NetworkManager owns connections after it exits. |
| `systemd/ranch-upload-lock.service` | Installs restrictive firewall before networking; stopping this unit leaves rules installed. |
| `systemd/ranch-upload-gate.service` | Long-running controller; restarts after failure and manages child process cleanup. |
| `systemd/NetworkManager-upload-gate.conf` | Installed as a NetworkManager drop-in requiring the boot lock first. |
| `tests/test_hotspot.py` | Base config/profile/hardware/lifecycle expectations using mocked Linux commands. |
| `tests/test_upload_gate.py` | Signature/replay/config/firewall/lifecycle tests and local HTTP JPEG transfer. |
| `tests/linux_gate_integration.py` | Separate privileged Linux test using disposable network namespaces and real nftables. |
| `docs/HARDWARE.md` | Hardware fit, interfaces, drivers and identity checks. |
| `docs/ACCEPTANCE.md` | Baseline physical-device checklist and troubleshooting. |
| `docs/UPLOAD_GATE.md` | Gate installation, protocol, receiver contract, operation and target acceptance. |
| `docs/CONFIGURATION.md` | Field-by-field editing reference, units, limits and change workflows. |
| `docs/MANUAL_REVIEW.md` | This reading guide and function index. |
| `.gitignore` | Patterns excluded if this folder is tracked in Git; does not prevent files entering a ZIP. |
| `__pycache__/`, `*.pyc` (if present locally) | Generated Python caches; no user settings. Not included in the refreshed package. |

## Execution flow

```text
Base hotspot:
  install.py -> installs files and OS dependencies
  hotspot.py configure -> validates JSON -> checks hardware -> writes profiles/hash
  hotspot.py start -> checks hardware/hash -> activates missing AP/LTE connections
  NetworkManager + ModemManager -> own the connections afterward

Camera uploads:
  boot lock -> installs firewall with no upload permission
  send_magic.py -> signed UDP packet -> upload_gate.serve()
    authenticate() -> Controller.handle() -> ReplayStore.consume()
      stop previous worker -> grant expiring receiver permission -> launch worker
        RTSP: FFmpeg pulls camera -> publishes video to RTSP receiver
        stills: fetch JPEG -> POST JPEG -> wait until next cycle
    Controller.tick() -> close permission/worker on expiry or worker exit
  Kernel permission also expires if the controller cannot run its next tick.
```

`c` means a validated configuration dictionary; `s` or `stream` means one camera/receiver definition. `uid` and `gid` are the Linux worker account IDs. `p` usually means a process, path or parser depending on the function; each function's docstring provides context. Times for command freshness are wall-clock milliseconds; active deadlines and intervals use monotonic seconds so ordinary clock corrections do not extend a running window.

## Function index: hotspot.py

| Function | Inputs / result / actions |
| --- | --- |
| `run()` | Executes a Linux command, returns stripped stdout; optional exit checking, default 150-second timeout. Suppresses tool output in raised errors. |
| `root()` | Rejects non-Linux/non-root use; changes nothing. |
| `validate()` / `load()` | Check a dictionary / read and check JSON; return the config. No hardware or network checks. |
| `keyvalue()` | Escapes scalar text for NetworkManager keyfiles. |
| `render()` | Validated config + modem IMEI -> two profile filename/text entries; writes nothing itself. |
| `atomic_write()` | Replaces a file via a temporary neighbor with specified permissions; default mode `0600`. |
| `usb_identity()` | Walks sysfs device ancestors; returns vendor/product IDs or `None`. |
| `wifi_device()` | Finds configured permanent MAC, checks expected USB ID/AP mode/channel, returns interface name. |
| `modem()` | Requires exactly one supported unlocked modem, returns IMEI. Never submits SIM PINs. |
| `no_subnet_conflict()` | Rejects overlap with other interfaces' currently assigned IPv4 networks. |
| `check_hardware()` | Sets regulatory domain, enables radios, checks Wi-Fi/subnet/modem; returns interface and IMEI. This function changes radio state. |
| `configure()` | Checks hardware, stops initializer, writes/reloads owned profiles, disconnects their old active instances, saves applied hash. May trigger NetworkManager autoconnect. |
| `fingerprint()` | Hashes config + IMEI to detect unapplied changes; does not hash Python code or generated profile content. |
| `start()` | Requires matching applied hash; activates missing profiles and checks subnet. Does not monitor them afterward. |
| `status()` | Prints Linux diagnostics and permanent Wi-Fi MACs. Does not prove internet access. |
| `main()` | Parses command/config path, checks privileges, dispatches action and reports failures. |

## Function index: upload_gate.py

| Function/class | Inputs / result / actions |
| --- | --- |
| `integer()` / `text()` | Reject invalid types/ranges or text patterns; no return value on success. |
| `endpoint()` | URL + allowed schemes -> parsed URL and effective TCP port. No connection is attempted. |
| `validate()` / `load()` | Check config dictionary / load JSON and check it. Fixed camera/receiver destinations come from here. |
| `read_key()` | Reads the shared signing key from a protected file; returns 32 bytes. |
| `canonical()` / `sign()` | Produce stable JSON bytes / a new signed command dictionary. Must agree between sender and Pi. |
| `unique_object()` | JSON object hook rejecting duplicate command fields. |
| `authenticate()` | Checks packet size, schema, signature, target, stream and freshness; returns unsigned command fields. Replay consumption is separate. |
| `ReplayStore.__init__()` | Opens/creates persistent SQLite database and schema. |
| `ReplayStore.consume()` | Commits command before upload permission; rejects reused nonce or non-increasing timestamp. Ordering is global, including stop commands. |
| `nft_run()` | Applies nftables script with a 10-second timeout; raises on failure. |
| `rules()` | Config + worker UID -> restrictive firewall text; no system changes until applied. Replaces only the project's table and starts with empty permissions. |
| `no_flowtables()` | Reads existing nftables rules and rejects forwarding offload. |
| `Firewall.close()` / `grant()` | Remove all receiver leases / replace them with one expiring receiver-IP/TCP-port permission. |
| `launch_worker()` | Starts an unprivileged process group; sends stream definition through stdin; returns process handle. |
| `kill_worker()` | Kills the worker's process group and waits up to 5 seconds. Includes FFmpeg descendants. |
| `Controller.__init__()` | Stores config, firewall, replay store, launcher, killer and clock; no active stream initially. Injected dependencies support offline tests. |
| `Controller.stop()` | Closes permission before killing active worker. |
| `Controller.handle()` | Consumes an authenticated command; stops matching stream or replaces current session with a newly authorized one. |
| `Controller.tick()` | Checks deadline/process exit; closes a completed session. |
| `worker_identity()` | Looks up the `ranch-stream` Linux account; rejects UID zero. |
| `serve()` / nested `shutdown()` | Installs closed rules, opens replay store, binds cellular UDP socket, checks messages/deadlines; SIGTERM enters cleanup. |
| `main()` | Dispatches `validate`, `render`, `lock`, or `serve`; render needs the installed Linux worker account, lock/serve need root. |

`rules()` affects all forwarding and Pi input/output, not just a camera program. Its output chain deliberately requires an unexpired lease even for established TCP uploads. Only configured local networks, loopback and specific DHCP traffic receive other output allowances. Keep this scope in mind when reviewing networking changes.

## Function index: worker, sender and installers

| File/function | Inputs / result / actions |
| --- | --- |
| `stream_worker.video_command()` | Stream definition -> FFmpeg argument list; video copy, TCP transport, no audio. Does not start FFmpeg itself. |
| `stream_worker.connection()` | HTTP(S) URL + optional pinned IP -> connection, parsed URL, request path. Preserves Host/SNI and certificate checks. |
| `stream_worker.fetch_jpeg()` | GET camera image, enforce size/JPEG start/end markers, return bytes, close connection. This is not a full JPEG decoder. |
| `stream_worker.post_jpeg()` | POST raw JPEG with optional bearer token, paced chunks, timestamp header; require a 2xx response, close connection. |
| `stream_worker.run()` | Run FFmpeg once, or loop through snapshot fetch/POST/wait. First error ends the worker; controller then closes permission. |
| `send_magic.main()` | Parse sender options, construct fresh timestamp/nonce, sign and send one UDP packet. No acknowledgment. |
| `install.call()` / `install_upload_gate.call()` | Run an installation command; stop on nonzero exit. |
| `install.main()` | Verify Pi/OS and conflicting services; install packages; copy base files and enable NetworkManager/ModemManager. Does not enable the hotspot initializer on first install. |
| `install_upload_gate.main()` | Validate input/hardware/interfaces; install packages/account; check firewall syntax; install config/key/programs/units; enable and activate gate. Existing key/history retained. |

The worker's entry block reads one JSON object from stdin and exits on error without logging camera credentials. Run it through the gate so its worker account, permission and deadline are managed together.

## Advanced settings in source code

These values are intentionally separate from routine JSON configuration. Search for the listed function or directive. Changing a literal requires reviewing related validation, tests and documentation; moving an installation path also requires updating installers and service units.

| Location | Current value/policy | What an edit affects |
| --- | --- | --- |
| `hotspot.py` top constants | Config/profile/hash paths; `AP_UUID`, `LTE_UUID` | Installation and connection ownership. Changing UUIDs can leave old profiles behind. |
| `hotspot.validate()` / `render()` | 2.4 GHz, channels 1-11, private `/24`, WPA2/CCMP, IPv6 disabled | Supported configuration and generated network policy; changing only the template is insufficient. |
| `hotspot.wifi_device()` / `modem()` | USB `0e8d:7961`, model containing `EM060K`, exactly one modem | Hardware compatibility checks. Confirm replacement device capabilities before adapting them. |
| `hotspot.render()` | LTE route metric `50`, autoconnect priority `100`, retries `0`, metered `1`, AP powersave `2` | NetworkManager profile properties; run configure again after a source change. |
| `hotspot.run()` / `start()` / `status()` | Command timeout 150 s; AP/LTE waits 60/120 s; diagnostic command timeout 15 s | How long device activation/diagnostics can block. |
| `upload_gate.py` top constants | Config/key/state paths, table name, packet limit 2048 bytes, command fields | File locations and wire protocol. Sender imports shared signing helpers from this module. |
| `upload_gate.validate()` | Numeric limits listed in the settings reference | Accepted user settings, including duration/rate limits. |
| `upload_gate.rules()` | Control input 20 packets/s with burst 40; upload burst 16000 bytes; filter priority -10 | Firewall admission/rate behavior; requires actual Linux packet tests after changes. |
| `Controller.handle()` | JPEG body pace = integer 75% of configured byte rate | Headroom for HTTP/TCP overhead; RTSP is not paced here. |
| `serve()` / `kill_worker()` / `nft_run()` | Socket timeout 0.2 s / process wait 5 s / command timeout 10 s | Polling, cleanup and firewall command waits. Kernel lease expiry remains independent. |
| `stream_worker.video_command()` | FFmpeg timeout `5000000` microseconds (5 s); first video track; no audio; stream copy | Camera transport/selection, not camera encoding settings. |
| `stream_worker.connection()` / `post_jpeg()` | HTTP socket timeout 10 s; POST chunk size 8192 bytes | HTTP waits and snapshot pacing granularity. |
| `send_magic.main()` | Port 45991, action start, duration 60 s | Sender defaults; `--seconds` can still exceed the configured Pi limit and be rejected there. |
| `install.py` / `install_upload_gate.py` | Apt package lists, OS/model checks, install destinations, account name | Installation prerequisites and file ownership. |
| `ranch-hotspot.service` | Retry 30 s; startup timeout 240 s | Initializer restart behavior; it is a oneshot, not an ongoing health monitor. |
| `ranch-upload-gate.service` | Retry 5 s; stop timeout 15 s; state directory and sandbox directives | Controller availability, child cleanup and filesystem permissions. |
| `ranch-upload-lock.service` / NetworkManager drop-in | Boot ordering and required lock | Prevents normal NetworkManager startup before restrictive rules load. |

## Reviewing a change

1. Change JSON first when the requested behavior is already configurable. Keep credentials out of shared templates and packages.
2. For a code change, read the function docstring and its callers. Confirm return values, units, raised errors and filesystem/network effects still match.
3. Run existing tests from the project/package root: `python -m unittest discover -s service/tests -v`. Or from inside `service/`, use `python -m unittest discover -s tests -v`.
4. Reinstall changed program files on the Pi. Re-run hotspot configure for profile-rendering changes even if JSON is unchanged; the applied hash does not track code. For gate JSON changes, use the validated copy/start procedure in [Settings and camera sources](CONFIGURATION.md#camera-and-receiver-fields). Program/unit changes need updated installed files and, for units, a systemd reload. Both installers download packages; on an already gated Pi that requires an APT proxy/mirror inside an allowed management subnet. Editing a source file alone does not update a running service.
5. For service/firewall/hardware changes, run target acceptance checks. From the project root on an appropriate Linux host, `sudo python3 service/tests/linux_gate_integration.py` checks real packet filtering in temporary namespaces; it does not test the modem or actual cameras.
6. Refresh the distributable folder and ZIP from reviewed source. Include only source, example settings, units, tests and documentation. Exclude private JSON, keys, replay databases, logs and Python caches.

## What verification does and does not show

The automated suite contains 27 tests: 10 hotspot checks and 17 gate/worker checks. Test `config()` helpers supply synthetic settings; `command()`/`packet()` create synthetic signed messages. The fixed test key is public fixture data, never a deployment key. Test methods have descriptive `test_...` names identifying the behavior they assert. `SnapshotTests` opens a loopback HTTP server; other external network/service operations are mocked. Temporary profile/database files are written during tests.

The separate Linux script's `run()` executes checked commands; `main()` creates router/receiver/camera namespaces and cleans them up; nested `ns()` runs commands inside a namespace and `denied()` asserts an attempted connection is blocked. Embedded probe/server/sender snippets generate test TCP traffic; their addresses, UID 65534 and short durations are fixtures, not production settings.

This readability pass preserves executable behavior while adding explanatory docstrings/comments and guides. Offline tests cannot establish Pi driver support, Linux service ordering, real firewall acceptance, camera interoperability, receiver availability or cellular delivery. See [baseline acceptance](ACCEPTANCE.md) and [gate acceptance](UPLOAD_GATE.md#operating-and-testing). No Pi was installed, configured or flashed during this review.

Existing operational limits worth retaining in a manual review: replay history is persistent and currently never pruned; command ordering is global; the controller has no daily data quota; failed uploads do not retry; and the receiver service itself is not included. These are current behavior, not features provided by the documentation changes.
