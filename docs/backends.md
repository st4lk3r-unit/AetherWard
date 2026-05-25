# Backends

---

## Hardware backends

All capture backends implement `HardwareBackend`:

```python
class HardwareBackend:
    def initialize(self) -> None: ...
    def configure(self, config: dict) -> None: ...
    def capabilities(self) -> BackendCapabilities: ...
    def start_capture(self, callback: Callable[[Frame], None]) -> None: ...
    def stop_capture(self) -> None: ...
    def set_frequency(self, hz: float) -> None: ...
    def close(self) -> None: ...
    # optional:
    def set_channel(self, channel: int) -> None: ...
    def get_csi(self) -> Optional[np.ndarray]: ...
```

`BackendCapabilities` fields:

| Field | Type | Description |
|-------|------|-------------|
| `frequency_min` / `frequency_max` | float | Hz |
| `bandwidth_max` | float | Hz |
| `supports_channel_hop` | bool | Can rapidly change frequency/channel |
| `supports_csi` | bool | Exports Channel State Information |
| `supports_hw_timestamp` | bool | Hardware timestamping available |
| `supports_tdoa_sync` | bool | PPS or GPSDO available |
| `max_antennas` | int | How many antennas this backend drives |

---

### `NL80211Backend` (bundled)

Linux 802.11 monitor-mode capture via Scapy and `iw`/`ip`.

```toml
[[antennas]]
backend        = "plugins.wifi_nl80211.NL80211Backend"
backend_config = {interface = "wlan0", restore = true}
```

**Config keys:**

| Key | Default | Description |
|-----|---------|-------------|
| `interface` | `wlan0` | Wireless interface |
| `restore` | `true` | Restore managed mode on close |
| `antenna_id` | interface name | Override the antenna ID string |

**Behaviour:**
- Puts interface into monitor mode with `ip link set <iface> down`, `iw dev <iface> set type monitor`, `ip link set <iface> up`
- Captures with Scapy `AsyncSniffer` (non-blocking background thread)
- Extracts RSSI and frequency from the radiotap header
- Parses beacon, probe-response, and probe-request metadata
- Populates `frame.metadata` with `protocol`, addresses, `bssid`, `ssid`, `identifier`, channel/band, capabilities, `auth_mode`, WPA/WPA2/WPA3 cipher/AKM details, vendor IEs, and frame type/subtype

**Frequency coverage:**
- 2.4 GHz: channels 1–14 (`2412 + (ch−1)×5` MHz; ch 14 = 2484 MHz)
- 5 GHz: channels 32–177 (`5000 + ch×5` MHz)
- 6 GHz: best-effort 802.11ax channel numbering (`5950 + ch×5` MHz)

**Requirements:**
```bash
pip install scapy
sudo apt install iw iproute2
# run as root, or grant:
sudo setcap cap_net_raw,cap_net_admin+eip $(which python3)
```

---

## GPS backends

Set `gps.backend` in config:

| Key | Class | Accuracy | Notes |
|-----|-------|----------|-------|
| `gpsd` | `GPSDBackend` | 1–10 m | Requires running `gpsd` daemon |
| `static` | `StaticGPSBackend` | exact | Fixed lat/lon/alt |
| `geoclue` | `GeoclueBackend` | 10–200 m | Linux GeoClue2 via D-Bus |
| `mls` | `MozillaLBSBackend` | 10–200 m | WiFi → Mozilla Location Services API |
| `ip` | `IPGeolocationBackend` | 1–50 km | Coarse only; not usable for solvers |
| `none` | — | — | No GPS |

All return `AbsolutePosition` with a `FixType` enum:

```
NONE · FIX_2D · FIX_3D · DGPS · RTK_FLOAT · RTK_FIXED
```

Positions with `fix_type = NONE` fail `is_valid()` and are not written to sessions.

---

### `GPSDBackend`

Connects to a running `gpsd` daemon.

```toml
[gps]
backend = "gpsd"
host    = "localhost"
port    = 2947
```

**Requires:** `pip install gpsd-py3`, `gpsd` running (`sudo apt install gpsd gpsd-clients`)

---

### `StaticGPSBackend`

Fixed position — no daemon, no network, no radio required.

```toml
[gps]
backend = "static"
lat     = 48.8566
lon     = 2.3522
alt     = 35.0
```

Use for: benchtop testing, fixed sensor installations, development without GPS hardware.

---

### `GeoclueBackend`

Linux GeoClue2 location service. Aggregates WiFi positioning, cell towers, and GPS.

```toml
[gps]
backend = "geoclue"
```

**Requires:**
```bash
sudo apt install geoclue-2.0
pip install dbus-python
```

Typical accuracy: 10–200 m in urban areas.

---

### `MozillaLBSBackend`

Scans nearby APs with `iw` and submits to the Mozilla Location Services geolocation API.

```toml
[gps]
backend   = "mls"
interface = "wlan0"     # WiFi interface to scan (empty = auto-detect)
api_url   = ""          # leave empty for public test key
```

**Rate limit:** do not call more than once per 5 seconds.
**Production:** register for your own API key at `location.services.mozilla.com`.

---

### `IPGeolocationBackend`

IP-based geolocation via ip-api.com. Accuracy 1–50 km.

```toml
[gps]
backend = "ip"
```

Useful only for coarse session geo-tagging. Never use as input to any solver.

---

## IMU backends

### `NullIMUBackend`

No-op. Returns identity orientation (level, North-aligned).

```toml
[imu]
backend = "null"
```

### `SerialIMUBackend`

Generic UART IMU. Reads JSON lines from the serial port.

```toml
[imu]
backend = "serial"
device  = "/dev/ttyUSB0"
baud    = 115200
```

Expected JSON line format:
```json
{
  "ax": 0.0, "ay": 0.0, "az": 9.81,
  "gx": 0.0, "gy": 0.0, "gz": 0.0,
  "qw": 1.0, "qx": 0.0, "qy": 0.0, "qz": 0.0
}
```

Fields `qw/qx/qy/qz` are used for quaternion orientation when present.
`ax/ay/az` are acceleration in m/s² body-frame; `gx/gy/gz` are angular velocity in rad/s.
