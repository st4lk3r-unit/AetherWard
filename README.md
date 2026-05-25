<div align="center">

<img src="docs/banner.png" alt="AetherWard" width="720">

![Python](https://img.shields.io/badge/python-3.11%2B-blue?style=flat-square)
![License](https://img.shields.io/badge/license-WTFPL-darkred?style=flat-square)
![Tests](https://img.shields.io/badge/tests-440%20passed-brightgreen?style=flat-square)
![Version](https://img.shields.io/badge/version-0.3.1-orange?style=flat-square)
![Deps](https://img.shields.io/badge/deps-numpy%20only-lightgrey?style=flat-square)

**Hardware-agnostic RF observation framework.**  
Capture, record, and geolocate RF emitters using consumer wireless hardware.

</div>

## Overview

AetherWard ties together three things most tools conflate:

- **Capture** — a plugin interface that abstracts any radio hardware behind a uniform `Frame` callback
- **Record** — append-only JSONL session files readable by any tool
- **Solve** — position solvers (RSS trilateration, TDOA hyperbolic, passive array sensing) operating on the recorded frames

Swap a WiFi NIC for an SDR, a LoRa dongle, or a custom driver without touching any solver code.

| Mode | Technique | Hardware | Typical accuracy | State |
|------|-----------|----------|-----------------|-------|
| `wardriver` | RSS trilateration | 1+ WiFi NIC, GPS | 5–50 m | OK |
| `trilateration` | TDOA / hyperbolic | 4+ synced NICs, GPS, PPS | 0.3–3 m | ⚠ WIP |
| `array_sensing` | RSSI/CSI variance | 2+ NICs | direction vector | ⚠ WIP |

## Install

AetherWard is not on PyPI — install directly from the source tree:

```bash
git clone <repo-url>
cd AetherWard

# create and activate a virtualenv (recommended)
python3 -m venv .venv
source .venv/bin/activate

# install the package and its entry points
pip install .

# optional extras
pip install ".[yaml]"   # YAML config support (pyyaml)
pip install ".[sdr]"    # RTL-SDR backend (pyrtlsdr)
pip install ".[dev]"    # pytest, mypy, ruff
```

Core CI excludes generated showcase/session suites by default and also checks Python compilation plus the C core build:

```bash
python -m compileall -q aetherward cli plugins tests
python -m pytest tests/ -m "not showcase"
cmake -S . -B build && cmake --build build
python -m pytest tests/ -m showcase      # optional: regenerate demo sessions
```

Requires **Python 3.11+** and **numpy ≥ 1.24**. Everything else is optional.  
See [docs/backends.md](docs/backends.md) for per-backend requirements.

After `pip install .`, the `aetherward` and `aw` commands are available in the virtualenv.  
To install them system-wide (outside a venv), run `sudo aetherward install`.

**GPS:** install the `gpsd` system daemon and plug in a GNSS dongle:

```bash
sudo apt install gpsd
sudo gpsd /dev/ttyUSB0 -F /var/run/gpsd.sock   # replace with your device
```

AetherWard connects to gpsd automatically when `gps.backend = "gpsd"` is set in config.

## Quick start

**Wardriving:**

```bash
aetherward wizard          # guided setup → saves config to ~/.aetherward/configs/
sudo aetherward run        # start capturing (needs CAP_NET_ADMIN or root)
aetherward web --open      # live map in browser
```

**TDOA (⚠ work in progress — 4 antennas, PPS sync):**

```bash
sudo aetherward run examples/trilateration_4ant.toml
```

**Post-process a recorded session:**

```bash
aetherward solve session.jsonl --n-exp 2.5 --follow
aetherward process session.jsonl --mode wardrive-map --format geojson
```

## CLI

```
aetherward <command> [options]
aw          <command> [options]     # short alias
```

| Command | Description |
|---------|-------------|
| `wizard` | Guided interactive configuration |
| `run [CONFIG]` | Start a capture session |
| `solve SESSION` | RSS/TDOA solver on a recorded session |
| `process SESSION` | Export to GeoJSON / CSV / KML / WiGLE |
| `config list\|load\|delete` | Manage saved configurations |
| `validate CONFIG` | Syntax-check a config file |
| `info` | Version, C core status, framework summary |
| `web [--host H] [--port P] [--open]` | Start the browser UI |
| `install / uninstall` | Add/remove CLI from `/usr/local/bin` |

**`solve` key options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--n-exp N` | `2.5` | Path loss exponent (`2.0` LOS → `4.5` heavy NLOS) |
| `--min-obs N` | `3` | Minimum observations before solving |
| `--follow` | off | Re-solve continuously as the file grows |
| `--interval S` | `2` | Re-solve interval in seconds with `--follow` |
| `--output FILE` | — | Write solved positions to JSONL |

## Configuration

Configs are TOML, JSON, or YAML stored in `~/.aetherward/configs/`.

```toml
mode     = "wardriver"
array_id = "my-rig"

[[antennas]]
id              = "wlan0"
backend         = "plugins.wifi_nl80211.NL80211Backend"
backend_config  = {interface = "wlan0"}
frequency_range = [2400000000, 2500000000]
position        = [0.0, 0.0, 0.0]          # ENU offset from GPS receiver (metres)
gain_dbi        = 0.0

[gps]
backend = "gpsd"

[mode_config]
channels         = [1, 6, 11]
hop_interval     = 0.1
output_path      = "~/.aetherward/sessions/session.jsonl"
store_raw_frames = true

[output]
format = "jsonl"
path   = "~/.aetherward/sessions/session.jsonl"
```

Full annotated examples in [`examples/`](examples/).  
Complete key reference in [docs/config.md](docs/config.md).

## Web UI

```bash
aetherward web --port 8080 --open
```

- **Live map** — solved positions on a tile map
- **ENU 3-D viewer** — local coordinate frame + antenna geometry
- **Log pane** — solver and runner output
- **Session browser** — load any `~/.aetherward/sessions/*.jsonl`
- **Config editor** — create, edit, delete configs in-browser

Pure stdlib HTTP server + Server-Sent Events. No npm, no build step.

## Session format

Append-only JSONL. One record per observation. Open with any editor, stream with `tail -f`, query with `jq`.

**Wardriver record:**
```json
{"t": 1716000000.1, "freq": 2412000000, "bw": 20000000, "rssi": -62.0,
 "ant": "wlan0", "protocol": "802.11", "id": "aa:bb:cc:dd:ee:ff",
 "ssid": "Home", "auth_mode": "[WPA2-PSK-CCMP][ESS]", "channel": 6,
 "metadata": {"frame_subtype": "beacon"}, "raw_frame_hex": "001122...",
 "lat": 48.8566, "lon": 2.3522, "alt": 35.0, "fix": 3}
```

Full format reference in [docs/session-format.md](docs/session-format.md).

## Plugin API

Drop a class anywhere on `sys.path`, reference it by class path in config:

```toml
backend = "my_package.my_module.MyBackend"
```

```python
from aetherward.hardware.backend import HardwareBackend, BackendCapabilities
from aetherward.signal.frame import Frame

class MyBackend(HardwareBackend):
    def initialize(self): ...
    def configure(self, config: dict): ...
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(frequency_min=433e6, frequency_max=434e6,
                                   bandwidth_max=250e3, supports_channel_hop=False,
                                   supports_csi=False, supports_hw_timestamp=False,
                                   supports_tdoa_sync=False, max_antennas=1)
    def start_capture(self, callback): ...
    def stop_capture(self): ...
    def set_frequency(self, hz): ...
    def close(self): ...
```

Full guide with CSI and hardware-timestamp details in [docs/plugin-api.md](docs/plugin-api.md).

## Documentation

| Doc | Contents |
|-----|----------|
| [docs/modes.md](docs/modes.md) | Wardriver, TDOA trilateration, array sensing — how each works, solver internals |
| [docs/config.md](docs/config.md) | Full config key reference for all modes, backends, and sync sources |
| [docs/backends.md](docs/backends.md) | Hardware + GPS + IMU backends, capabilities, setup requirements |
| [docs/plugin-api.md](docs/plugin-api.md) | Writing custom hardware backends, CSI support, hw timestamps |
| [docs/session-format.md](docs/session-format.md) | JSONL record types, field reference, jq recipes |

## Architecture

```
aetherward/
├── core.py              TDOA solver — Gauss-Newton, C binding + Python fallback
├── config/schema.py     AWConfig, AntennaConfig, GPSConfig, IMUConfig, SyncConfig
├── antenna/             Antenna, AntennaArray, RadiationPattern
├── hardware/            HardwareBackend ABC, GPS backends, IMU backends
├── modes/               ScanMode ABC → wardriver, trilateration, array_sensing
├── orientation/         Quaternion — Hamilton product, Euler, axis-angle
├── position/            AbsolutePosition (WGS84), RelativePosition (ENU), RSS solver
└── signal/              Frame, Observation, SignalSource

cli/
├── aetherward.py        Entry point, argparse, UI primitives
├── _commands.py         run, solve, process, install, config, validate, info
├── _wizard.py           Interactive guided configuration
└── web.py               HTTP server + SSE + solver workers

plugins/
└── wifi_nl80211.py      NL80211Backend — Linux 802.11 monitor mode (Scapy)
```

**Coordinate systems:** absolute (WGS84, lat/lon/alt) and relative (local ENU, metres) are strictly separated. All geometry math lives in ENU; projection to WGS84 is always explicit.

**Optional C core:** `libaw.so` in the package root provides ~10× faster TDOA solving. The pure-Python fallback is always active. Check: `aetherward info`.
