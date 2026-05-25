<div align="center">

<img src="docs/banner.png" alt="AetherWard" width="720">

![Python](https://img.shields.io/badge/python-3.11%2B-blue?style=flat-square)
[![Tests](https://github.com/st4lk3r-unit/AetherWard/actions/workflows/simple-test.yml/badge.svg)](https://github.com/st4lk3r-unit/AetherWard/actions/workflows/simple-test.yml)
![Version](https://img.shields.io/badge/version-0.3.0-orange?style=flat-square)
![License](https://img.shields.io/badge/license-WTFPL-darkred?style=flat-square)

**Protocol-agnostic RF observation framework.**
Capture, record, replay, and geolocate RF emitters using consumer wireless hardware or custom radio backends.

</div>

## What it is

AetherWard separates the RF workflow into four clean layers:

- **Capture** — hardware backends expose frames through one interface.
- **Record** — append-only JSONL sessions preserve raw observations with GPS, radio, and protocol metadata.
- **Solve** — RSS and TDOA solvers operate on recorded observations instead of directly on live devices.
- **Inspect** — CLI and web tools replay sessions, show listener tracks, and visualize estimated sources.

The default backend targets Linux 802.11 monitor mode through Scapy, but the session format is not locked to WiFi. A backend can emit `802.11`, Bluetooth, LoRa, SDR, or custom protocol observations as long as it fills the generic `signal` envelope and optional protocol-specific fields.

## Current status

| Mode | Purpose | State |
|------|---------|-------|
| `wardriver` | GPS-tagged RF observations and RSS-based approximate source positions | usable, still heuristic |
| `trilateration` | TDOA / hyperbolic positioning in local ENU coordinates | experimental; needs real timing discipline |
| `array_sensing` | RSSI/CSI variance sensing over an antenna array | experimental |

RSS positioning is useful for approximate AP/source placement and ranking. It is not a substitute for calibrated survey equipment. TDOA only becomes meaningful with synchronized receivers and reliable hardware timestamps.

## Install

```bash
git clone https://github.com/st4lk3r-unit/AetherWard
cd AetherWard

python3 -m venv .venv
source .venv/bin/activate
pip install .

# optional extras
pip install ".[yaml]"   # YAML config support
pip install ".[gps]"    # gpsd Python helper package
pip install ".[sdr]"    # RTL-SDR backend dependency
pip install ".[dev]"    # tests, lint, type-check helpers
```

Requirements:

- Python 3.11+
- numpy 1.24+
- Linux monitor-mode support for the bundled WiFi backend
- gpsd for live GNSS input when `gps.backend = "gpsd"`

After installation, both commands are available:

```bash
aetherward --help
aw --help
```

## GPSD quick check

AetherWard reads GPS through gpsd. Test the daemon before blaming the scanner:

```bash
sudo systemctl stop gpsd.socket gpsd.service 2>/dev/null || true
sudo gpsd -n /dev/ttyUSB0 -F /var/run/gpsd.sock

gpspipe -w -n 20
```

Look for `TPV` objects with `mode: 2` or `mode: 3`. Do not keep `cat`, `screen`, or `minicom` open on the same tty while gpsd is running.

## Quick start

```bash
aetherward wizard              # guided config creator
sudo aetherward run my-config  # capture into ~/.aetherward/sessions/
aetherward web --open          # browser UI
```

Post-process a saved session:

```bash
aetherward solve ~/.aetherward/sessions/my-drive-20260525-155800.jsonl --n-exp 2.5
aetherward process ~/.aetherward/sessions/my-drive-20260525-155800.jsonl --mode wardrive-map --format geojson
```

## Configuration

Configs are TOML, JSON, or YAML files stored in `~/.aetherward/configs/`.

```toml
mode     = "wardriver"
array_id = "roof-rig"

[[antennas]]
id              = "wlan0"
backend         = "plugins.wifi_nl80211.NL80211Backend"
backend_config  = {interface = "wlan0"}
frequency_range = [2400000000, 2500000000]
position        = [0.0, 0.0, 0.0]        # ENU offset from GPS receiver, metres
orientation_euler = [0.0, 0.0, 0.0]
gain_dbi        = 0.0

[gps]
backend = "gpsd"
host    = "localhost"
port    = 2947

[sync]
source = "software"

[mode_config]
channels     = [1, 6, 11]
hop_interval = 0.1

[output]
format       = "jsonl"
session_name = "roof-drive"
```

The runner creates a timestamped output file automatically:

```text
~/.aetherward/sessions/roof-drive-20260525-155800.jsonl
```

If `session_name` is omitted, the config name is used. A legacy explicit `[output] path = "..."` is still accepted, but new configs should prefer `session_name` to avoid overwriting captures.

## Session format

Sessions are newline-delimited JSON. New records use an explicit schema and record type while keeping legacy flat aliases for simple tools.

```json
{
  "schema": "aetherward.session.v1",
  "record_type": "observation",
  "t": 1716000000.123,
  "session": {"id": "roof-drive-20260525-155800", "mode": "wardriver"},
  "receiver": {"antenna_id": "wlan0"},
  "observer": {
    "lat": 48.8566,
    "lon": 2.3522,
    "alt_m": 35.0,
    "accuracy_h_m": 3.5,
    "fix_type": 3,
    "num_sats": 10
  },
  "signal": {
    "protocol": "802.11",
    "id": "aa:bb:cc:dd:ee:ff",
    "frequency_hz": 2412000000,
    "bandwidth_hz": 20000000,
    "rssi_dbm": -62.0
  },
  "wifi": {
    "bssid": "aa:bb:cc:dd:ee:ff",
    "ssid": "Home",
    "channel": 1,
    "auth_mode": "WPA2-PSK",
    "encryption": "WPA2",
    "cipher": "CCMP",
    "akm": "PSK",
    "privacy": true
  }
}
```

See [`docs/session-format.md`](docs/session-format.md) for the full record reference.

## CLI

```text
aetherward <command> [options]
aw          <command> [options]
```

| Command | Description |
|---------|-------------|
| `wizard` | Guided interactive configuration |
| `run [CONFIG]` | Start a capture session |
| `solve SESSION` | Estimate source positions from a recorded session |
| `process SESSION` | Export sessions to GeoJSON, CSV, KML, or WiGLE-style CSV |
| `config list\|load\|delete` | Manage saved configurations |
| `validate CONFIG` | Syntax-check a config file |
| `info` | Version, C core status, and framework summary |
| `web [--host H] [--port P] [--open]` | Start the browser UI |
| `install / uninstall` | Add/remove CLI wrappers in `/usr/local/bin` |

## Web UI

```bash
aetherward web --port 8080 --open
```

The web UI provides:

- run tab with a red/black terminal and preserved `banner.txt` formatting
- config editor and wizard
- session browser under `~/.aetherward/sessions/`
- listener track on the map
- solved source markers with confidence circles
- position details including AP metadata such as SSID, BSSID, channel, auth mode, cipher, AKM, RSS residual, sample count, and mean GPS accuracy
- ENU 3-D viewer for local antenna/source geometry

It is served by the Python standard library HTTP server and uses Server-Sent Events. No npm or build step is required.

## WiGLE export policy

AetherWard keeps raw observations and solved source estimates separate.

- WiGLE-style export uses raw GPS observation rows.
- RSS/TDOA solved positions are local analysis products and are exported as GeoJSON/JSONL, not uploaded as if they were capture coordinates.

## Project layout

```text
aetherward/              framework package
  antenna/               antenna geometry and patterns
  config/                config dataclasses
  hardware/              backend interfaces, GPS, IMU
  modes/                 wardriver, trilateration, array_sensing
  position/              WGS84/ENU helpers and RSS solver
  signal/                frame, observation, source structures
  session.py             schema-aware JSONL helpers
cli/                     command line and web UI
core/                    optional C solver core
plugins/                 bundled hardware backends
docs/                    references and deeper notes
examples/                ready-to-edit configs
tests/                   pytest suite
```

## Documentation

| Doc | Contents |
|-----|----------|
| [`docs/config.md`](docs/config.md) | Config keys, output handling, path layout |
| [`docs/session-format.md`](docs/session-format.md) | JSONL schema and jq examples |
| [`docs/modes.md`](docs/modes.md) | Mode behavior and solver notes |
| [`docs/backends.md`](docs/backends.md) | Backend setup and capability model |
| [`docs/plugin-api.md`](docs/plugin-api.md) | Writing custom capture backends |

## Development

```bash
pip install ".[dev]"
python -m pytest tests/
```

The package is intentionally usable without a JavaScript build chain. Keep generated sessions append-only, preserve backward compatibility for legacy flat JSONL aliases, and keep protocol-specific details nested under protocol keys such as `wifi` rather than at the top level.
