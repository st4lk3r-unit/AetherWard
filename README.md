<div align="center">

<img src="img/banner.png" alt="AetherWard" width="720">

![Python](https://img.shields.io/badge/python-3.11%2B-blue?style=flat-square)
![License](https://img.shields.io/badge/license-WTFPL-darkred?style=flat-square)
[![Tests](https://github.com/st4lk3r-unit/AetherWard/actions/workflows/release.yml/badge.svg)](https://github.com/st4lk3r-unit/AetherWard/actions/workflows/release.yml)
![Version](https://img.shields.io/badge/version-0.3.3-orange?style=flat-square)

**Hardware-agnostic RF observation framework.**  
Capture, record, sanity-check, map, and solve RF observations from Wi-Fi adapters and other pluggable radio backends.

</div>

## What AetherWard does

AetherWard is built around a simple workflow:

1. **Create a config** describing your radio adapter, GPS source, and scan mode.
2. **Run a capture session**. AetherWard writes append-only JSONL records under `~/.aetherward/sessions/`.
3. **Open the web UI or post-process the session** to inspect the route, export data, or solve approximate source positions.

The framework deliberately separates:

- **Capture** — hardware plugins produce normalized `Frame` objects.
- **Record** — every observation is written to JSONL so other tools can inspect it.
- **Solve/process** — map/export/solver code runs on recorded sessions.

> Use AetherWard only where you are legally allowed to observe radio traffic. Wardriver mode is passive, but session files can contain SSIDs, BSSIDs, station MAC addresses, GPS coordinates, and raw 802.11 management frames.

## Supported modes

| Mode | What it is for | Typical hardware | State |
|------|----------------|------------------|-------|
| `wardriver` | Wi-Fi observation, GPS-tagged sessions, maps, RSS estimates | 1+ monitor-mode Wi-Fi adapter + optional GPS | usable |
| `trilateration` | TDOA/RSS source solving with a fixed array | 4+ synced antennas, PPS/GPSDO preferred | experimental |
| `array_sensing` | RSSI/CSI variance and direction/event sensing | 2+ adapters | experimental |

Most beginners should start with **`wardriver`**.

## Install

AetherWard includes an installer. Use it first. It asks which optional parts you want and creates a clean virtual environment by default.

### From a release zip

```bash
unzip AetherWard-x.x.zip
cd AetherWard-x.x
bash install.sh
```

Recommended answers for a normal Wi-Fi + GPS wardriving setup:

```text
Build C core?                             N
GPS support (gpsd-py3)?                   Y
YAML config support (pyyaml)?             Y
RTL-SDR support (pyrtlsdr)?               N
WiFi capture support (scapy)?             Y
Developer tools?                          N
Installation target?                      1  # virtual environment
```

After a virtualenv install, run AetherWard with either:

```bash
source .venv/bin/activate
aetherward
```

or without activating:

```bash
./bin/aetherward
```

### From git

```bash
git clone https://github.com/st4lk3r-unit/AetherWard
cd AetherWard
bash install.sh
```

### Manual install, for developers

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[gps,yaml,dev]'
pip install 'scapy>=2.5'      # needed for the Linux Wi-Fi backend
```

AetherWard requires **Python 3.11+** and `numpy`. The Linux Wi-Fi backend also needs Scapy and root/CAP_NET_ADMIN permissions to switch adapters into monitor mode.

## Prepare hardware

### Wi-Fi adapter

Use a USB Wi-Fi adapter that supports monitor mode. Do not use the same adapter for Internet access and capture.

Find the interface name:

```bash
ip link
```

Typical names are `wlan0`, `wlan1`, `wlx...`.

If NetworkManager keeps taking the adapter back, disconnect it from managed Wi-Fi before running AetherWard. The NL80211 backend will try to put the interface into monitor mode and can auto-recover it if channel hopping fails.

### GPS / gpsd

For real wardriving, use a GPS receiver through `gpsd`.

Debian/Ubuntu example:

```bash
sudo apt install gpsd gpsd-clients
ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
sudo gpsd /dev/ttyUSB0 -F /var/run/gpsd.sock
cgps
```

Replace `/dev/ttyUSB0` with your GPS device. `cgps` should show a moving fix before you expect good geotagging.

If you only want to test the UI indoors, use the wizard and choose no GPS or a static GPS position.

## First run: beginner workflow

### 1. Start the web UI

The web UI is the easiest path because it contains the config wizard, editor, runner, map, session browser, and log pane.

```bash
./bin/aetherward web --open
```

If you installed manually inside an activated virtualenv:

```bash
aetherward web --open
```

Open the shown URL, usually:

```text
http://127.0.0.1:8080
```

### 2. Create a config

In the web UI, open the config wizard.

The first question is the **config / array name**. This name becomes:

- the saved config name under `~/.aetherward/configs/`
- `array_id` inside the config
- the default session filename prefix

For a one-adapter GPS wardriver setup, select:

```text
mode: wardriver
adapter/interface: wlan1          # or your real capture interface
GPS: gpsd
session output: default sessions path
channels: 1-13                    # or 1,6,11 for faster common-channel scans
hop interval: 0.1 to 0.2 seconds
store raw frames: true
```

The generated config should contain this output block:

```toml
[output]
format = "jsonl"
path_policy = "default"
```

With `path_policy = "default"`, each run creates a fresh timestamped session file:

```text
~/.aetherward/sessions/<array_id>-YYYYmmdd-HHMMSS.jsonl
```

### 3. Validate the config

```bash
./bin/aetherward validate ~/.aetherward/configs/YOUR-CONFIG.toml
```

or, if the config is saved by name:

```bash
./bin/aetherward config list
./bin/aetherward config load YOUR-CONFIG
```

### 4. Run capture with logging

Wi-Fi monitor mode usually needs root. Use the venv launcher path directly with `sudo`:

```bash
sudo ./bin/aetherward run YOUR-CONFIG --log
```

A default run log is written to:

```text
~/.aetherward/logs/sessions-<config-name>-<timestamp>.log
```

A session JSONL is written to:

```text
~/.aetherward/sessions/<array_id>-<timestamp>.jsonl
```

During the run, watch the status line. In wardriver mode you should see frame count, source count, GPS coordinates, and GPS age. If GPS age grows or says stale, stop and debug GPS before trusting the route.

### 5. Inspect the result

Open the web UI session browser, or run a raw sanity check:

```bash
./bin/aetherward session-check ~/.aetherward/sessions/YOUR-SESSION.jsonl --details --html
```

This creates a simple HTML map next to the session file. It draws **every geotagged sample directly**, without solver or web UI logic. Use it to answer:

- does the raw session reach the end of the route?
- are GPS coordinates stale/repeated?
- are timestamps jumping backward?
- are there malformed JSON lines?

If the sanity HTML reaches the end but the web map does not, the bug is in map loading/rendering. If the sanity HTML stops early too, the session data itself is stale or incomplete.

### 6. Export or solve

Wardrive-style exports:

```bash
./bin/aetherward process ~/.aetherward/sessions/YOUR-SESSION.jsonl --format geojson
./bin/aetherward process ~/.aetherward/sessions/YOUR-SESSION.jsonl --format csv
./bin/aetherward process ~/.aetherward/sessions/YOUR-SESSION.jsonl --format wigle
```

RSS solving:

```bash
./bin/aetherward solve ~/.aetherward/sessions/YOUR-SESSION.jsonl --n-exp 2.5
```

Follow a growing file while a run is still active:

```bash
./bin/aetherward solve ~/.aetherward/sessions/YOUR-SESSION.jsonl --follow --interval 2
```

## CLI reference

```bash
aetherward <command> [options]
aw          <command> [options]
```

If you installed into the repo virtualenv and do not want to activate it, replace `aetherward` with `./bin/aetherward`.

| Command | Purpose |
|---------|---------|
| `aetherward` | Interactive terminal menu |
| `aetherward wizard` | Terminal config wizard |
| `aetherward web --open` | Browser UI: wizard, editor, runner, live map, sessions |
| `aetherward run [CONFIG]` | Start a capture session |
| `aetherward run CONFIG --log` | Run and tee stdout/stderr to `~/.aetherward/logs/` |
| `aetherward run CONFIG --log-file FILE` | Run with a custom log path |
| `aetherward validate CONFIG` | Validate config syntax/schema |
| `aetherward config list` | List saved configs |
| `aetherward config load NAME` | Print a saved config |
| `aetherward config delete NAME` | Delete a saved config |
| `aetherward session-check SESSION --details --html` | Raw JSONL sanity report and dumb map |
| `aetherward process SESSION --format geojson` | Export observations/sources |
| `aetherward solve SESSION` | Solve approximate source positions |
| `aetherward info` | Version and C-core status |
| `aetherward install` | Install a command wrapper to PATH |
| `aetherward uninstall` | Remove AetherWard artifacts |

Useful run examples:

```bash
sudo ./bin/aetherward run my-wardriver --log
sudo ./bin/aetherward run ~/.aetherward/configs/my-wardriver.toml --log
sudo ./bin/aetherward run my-wardriver --log-file /tmp/aetherward-run.log
```

Useful session-check examples:

```bash
./bin/aetherward session-check session.jsonl --details
./bin/aetherward session-check session.jsonl --html
./bin/aetherward session-check session.jsonl --geojson --csv
```

## Configuration basics

Configs live in:

```text
~/.aetherward/configs/
```

Minimal one-adapter wardriver config:

```toml
array_id = "my-wardriver"
mode = "wardriver"

[[antennas]]
id = "wlan1"
backend = "plugins.wifi_nl80211.NL80211Backend"
backend_config = {interface = "wlan1", auto_recover = true, recover_cooldown = 2.0}
frequency_range = [2400000000, 2500000000]
position = [0, 0, 0]
orientation_euler = [0.0, 0.0, 0.0]
gain_dbi = 0.0

[gps]
backend = "gpsd"
host = "localhost"
port = 2947

[sync]
source = "software"

[mode_config]
channels = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]
hop_interval = 0.2
store_raw_frames = true

[output]
format = "jsonl"
path_policy = "default"
```

Important fields:

| Field | Meaning |
|-------|---------|
| `array_id` | Human name for the rig; also used in default session filenames |
| `backend_config.interface` | Linux Wi-Fi interface to put into monitor mode |
| `auto_recover` | Try down/up monitor-mode recovery if channel hopping fails |
| `channels` | Wi-Fi channels to hop through |
| `hop_interval` | Seconds to stay on each channel |
| `store_raw_frames` | Store raw frame hex/base64 in the JSONL session |
| `[output].path_policy = "default"` | Create a fresh timestamped session in `~/.aetherward/sessions/` |

Full reference: [`docs/config.md`](docs/config.md).

## Files and folders

| Path | What it contains |
|------|------------------|
| `~/.aetherward/` | AetherWard home directory |
| `~/.aetherward/configs/` | Saved TOML/JSON/YAML configs |
| `~/.aetherward/sessions/` | Recorded JSONL sessions |
| `~/.aetherward/logs/` | Optional run logs created by `--log` or the web UI log checkbox |
| `~/.aetherward/.last_config` | Last selected config |

## Session files

Sessions are newline-delimited JSON. They are intentionally append-only so they can be tailed, copied, and inspected after crashes.

Observation record example:

```json
{"schema":"aetherward.session.v1","record_type":"observation","t":1716000000.1,"freq":2412000000,"bw":20000000,"rssi":-62.0,"ant":"wlan1","protocol":"802.11","id":"aa:bb:cc:dd:ee:ff","ssid":"Example","channel":6,"lat":48.8566,"lon":2.3522,"alt":35.0,"fix":3,"gps_age_s":0.7}
```

GPS breadcrumb example:

```json
{"schema":"aetherward.session.v1","record_type":"gps","t":1716000001.0,"lat":48.8567,"lon":2.3523,"alt":35.2,"fix":3}
```

The web map prefers GPS breadcrumb records for the driven path when present. Solvers and source processors ignore GPS breadcrumbs so they do not become fake RF sources.

Full format reference: [`docs/session-format.md`](docs/session-format.md).

## Web UI

```bash
./bin/aetherward web --host 127.0.0.1 --port 8080 --open
```

The web UI provides:

- config wizard and config editor
- run button for saved configs
- run-log checkbox
- live status/log pane
- session browser
- raw path and source map
- ENU/array geometry viewer

The server is a Python stdlib HTTP server with Server-Sent Events. There is no npm build step.

## Troubleshooting

### `aetherward: command not found`

If you used the default virtualenv install:

```bash
source .venv/bin/activate
aetherward info
```

or:

```bash
./bin/aetherward info
```

### Permission errors or adapter will not enter monitor mode

Run capture as root:

```bash
sudo ./bin/aetherward run YOUR-CONFIG --log
```

Also make sure the adapter is not your active Internet adapter and is not being controlled by NetworkManager.

### GPS appears to move, but the map/session stalls

Run with logging and watch GPS age:

```bash
sudo ./bin/aetherward run YOUR-CONFIG --log
```

Then sanity-check the session:

```bash
./bin/aetherward session-check ~/.aetherward/sessions/YOUR-SESSION.jsonl --details --html
```

Look for stale coordinate runs, missing geotags, timestamp backsteps, or large gaps. The raw sanity HTML is the fastest way to tell whether the session file or the map UI is to blame.

### Session file is not created

Use the default output policy:

```toml
[output]
format = "jsonl"
path_policy = "default"
```

Do not set `format = "none"` unless you intentionally want no session file.

### Too few frames

Try increasing dwell time:

```toml
[mode_config]
hop_interval = 0.2
```

Or reduce the channel list to common channels:

```toml
channels = [1, 6, 11]
```

### Adapter drops during channel hopping

Keep auto-recovery enabled:

```toml
backend_config = {interface = "wlan1", auto_recover = true, recover_cooldown = 2.0}
```

Check the run log for `set channel` failures and interface recovery attempts.

## Development and tests

Developer install:

```bash
bash install.sh       # answer yes to Developer tools
source .venv/bin/activate
```

Run checks:

```bash
python -m compileall -q aetherward cli plugins tests
python -m pytest tests/ -m "not showcase"
cmake -S . -B build && cmake --build build
python -m pytest tests/ -m showcase      # optional generated/demo session tests
```

## Architecture

```text
aetherward/
├── core.py              TDOA solver: C binding + Python fallback
├── config/schema.py     AWConfig, antenna/GPS/IMU/sync config models
├── antenna/             Antenna, AntennaArray, radiation patterns
├── hardware/            HardwareBackend ABC, GPS backends, IMU backends
├── modes/               wardriver, trilateration, array_sensing
├── orientation/         quaternion and Euler helpers
├── position/            WGS84/ENU positions and RSS solver
├── session.py           session path + record helpers
├── session_sanity.py    raw JSONL sanity checker
└── signal/              Frame, Observation, SignalSource

cli/
├── aetherward.py        entry point and argparse
├── _commands.py         run, solve, process, validate, session-check
├── _wizard.py           terminal wizard
└── web.py               web server and SSE workers

plugins/
└── wifi_nl80211.py      Linux 802.11 monitor-mode backend
```

Coordinate systems are kept separate: GPS is WGS84 latitude/longitude/altitude, while geometry math uses local ENU metres.

The optional C core accelerates TDOA/DSP paths. Wardriver mode works without it.

## More documentation

| Document | Contents |
|----------|----------|
| [`docs/config.md`](docs/config.md) | Complete config key reference |
| [`docs/backends.md`](docs/backends.md) | Hardware, GPS, IMU backend notes |
| [`docs/modes.md`](docs/modes.md) | Wardriver, TDOA, and array-sensing internals |
| [`docs/session-format.md`](docs/session-format.md) | JSONL fields, record types, jq recipes |
| [`docs/plugin-api.md`](docs/plugin-api.md) | Writing custom hardware backends |
