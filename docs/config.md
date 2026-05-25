# Configuration reference

Configs are TOML, JSON, or YAML files stored in `~/.aetherward/configs/`.

Generate a validated config interactively:

```bash
aetherward wizard
```

---

## Full annotated example (TOML)

```toml
# Operating mode
mode     = "wardriver"          # wardriver | trilateration | array_sensing
array_id = "my-rig"            # identifier for this antenna array

# ── Antennas ──────────────────────────────────────────────────────────────────
# One [[antennas]] block per physical antenna.

[[antennas]]
id              = "wlan0"
backend         = "plugins.wifi_nl80211.NL80211Backend"
backend_config  = {interface = "wlan0"}
frequency_range = [2400000000, 2500000000]   # Hz  (2.4 GHz)
position        = [0.0, 0.0, 0.0]            # ENU offset from GPS receiver (metres)
orientation_euler = [0.0, 0.0, 0.0]          # roll, pitch, yaw (degrees)
gain_dbi        = 0.0
pattern         = "isotropic"                # isotropic | dipole | path/to/pattern.npy

[[antennas]]
id              = "wlan1"
backend         = "plugins.wifi_nl80211.NL80211Backend"
backend_config  = {interface = "wlan1"}
frequency_range = [4900000000, 5900000000]   # Hz  (5 GHz)
position        = [0.5, 0.0, 0.0]            # 0.5 m east of wlan0
orientation_euler = [0.0, 0.0, 0.0]
gain_dbi        = 2.15
pattern         = "dipole"

# ── GPS ───────────────────────────────────────────────────────────────────────
[gps]
backend = "gpsd"                # gpsd | static | geoclue | mls | ip | none
host    = "localhost"           # gpsd host
port    = 2947                  # gpsd port
# For static backend: lat = 48.8566; lon = 2.3522; alt = 35.0

# ── IMU ───────────────────────────────────────────────────────────────────────
[imu]
backend = "null"                # null | serial | custom class path
device  = ""                    # e.g. /dev/ttyUSB0
baud    = 115200

# ── Timing sync ───────────────────────────────────────────────────────────────
[sync]
source = "software"             # software | ntp | pps | gpsdo
device = ""                     # e.g. /dev/pps0

# ── Mode-specific settings ────────────────────────────────────────────────────
[mode_config]
# wardriver
channels         = [1, 6, 11]
hop_interval     = 0.1
output_path      = "~/.aetherward/sessions/session.jsonl"
store_raw_frames = true

[output]
format = "jsonl"
path   = "~/.aetherward/sessions/session.jsonl"

# trilateration (replace mode_config block)
# channel            = 6
# reference_antenna  = "wlan0"
# correlation_window = 0.001
# group_timeout      = 0.05

# array_sensing (replace mode_config block)
# channel            = 6
# history_len        = 100
# calibration_frames = 50
# sensitivity        = 0.05
# hysteresis         = 0.4
# ema_alpha          = 0.3

[output]
format = "jsonl"
path   = "~/.aetherward/sessions/session.jsonl"
```

---

## Key reference

### Top level

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `mode` | string | `wardriver` | Operating mode |
| `array_id` | string | `default` | Array identifier |

### `[[antennas]]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `id` | string | — | Unique ID within the array |
| `backend` | string | — | Class path (e.g. `plugins.wifi_nl80211.NL80211Backend`) |
| `backend_config` | dict | `{}` | Backend-specific settings |
| `position` | [x, y, z] | `[0,0,0]` | ENU offset from array origin (metres) |
| `orientation_euler` | [r, p, y] | `[0,0,0]` | Roll/pitch/yaw in degrees; yaw 0° = boresight North |
| `frequency_range` | [min, max] | — | Hz |
| `pattern` | string | `isotropic` | `isotropic`, `dipole`, or `.npy` gain table path |
| `gain_dbi` | float | `0.0` | Peak antenna gain |

**Antenna position** is the physical offset from where the GPS receiver is mounted,
in local East-North-Up metres. The GPS anchor updates live as the array moves.

### `[gps]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `backend` | string | `gpsd` | GPS source |
| `host` | string | `localhost` | gpsd hostname |
| `port` | int | `2947` | gpsd port |
| `lat` / `lon` | float | — | Required when `backend = "static"` |
| `alt` | float | `0.0` | Altitude (metres) |
| `interface` | string | `""` | WiFi interface for MLS (empty = auto-detect) |
| `api_url` | string | `""` | Override MLS endpoint URL |

### `[imu]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `backend` | string | `null` | `null`, `serial`, or custom class path |
| `device` | string | `""` | Serial device |
| `baud` | int | `115200` | Baud rate |

Serial IMU expects JSON lines:
```json
{"ax": 0.0, "ay": 0.0, "az": 9.81, "gx": 0.0, "gy": 0.0, "gz": 0.0,
 "qw": 1.0, "qx": 0.0, "qy": 0.0, "qz": 0.0}
```

### `[sync]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `source` | string | `software` | Timing reference |
| `device` | string | `""` | PPS device path |

### `[mode_config]` — wardriver

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `channels` | list[int] | 1–13 | Channels to scan |
| `hop_interval` | float | `0.1` | Seconds per channel |
| `output_path` | string | — | JSONL output path. If omitted, `[output].path` is used. |
| `store_raw_frames` | bool | `true` | Store raw frame bytes as `raw_frame_hex`/`raw_frame_b64` |

### `[mode_config]` — trilateration

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `channel` | int | — | Channel all antennas tune to |
| `reference_antenna` | string | — | Antenna ID used as t = 0 |
| `correlation_window` | float | `0.001` | Max expected TDOA (seconds) |
| `group_timeout` | float | `0.05` | Discard incomplete groups after (seconds) |

### `[mode_config]` — array_sensing

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `channel` | int | — | Channel to monitor |
| `history_len` | int | `100` | Rolling window depth |
| `calibration_frames` | int | `50` | Baseline frames before events fire |
| `sensitivity` | float | `0.05` | Variance increase to trigger event |
| `hysteresis` | float | `0.4` | Return-to-idle fraction of sensitivity |
| `ema_alpha` | float | `0.3` | EMA smoothing weight (0–1) |

---

## File paths

| Path | Description |
|------|-------------|
| `~/.aetherward/` | AW home directory |
| `~/.aetherward/configs/` | Saved named configurations |
| `~/.aetherward/sessions/` | Recorded session JSONL files |
| `~/.aetherward/.last_config` | Name of the most recently used config |
