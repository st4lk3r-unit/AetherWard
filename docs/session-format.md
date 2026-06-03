# Session format

Sessions are newline-delimited JSON (`.jsonl`). The file is append-only as the session runs. Wardriving sessions contain observation records for captured frames and GPS breadcrumb records for the driven path. All mode types write to the same file; the web UI and `solve`/`process` commands detect the record type from the fields present.

Sessions are plain text — open them with any editor, stream them with `tail -f`,
or post-process with `jq`.

---

## Record types

### Wardriver observation

Written by `wardriver` mode for every captured and correlated frame.

```json
{
  "schema": "aetherward.session.v1",
  "record_type": "observation",
  "t": 1716000000.123,
  "freq": 2412000000,
  "bw": 20000000,
  "rssi": -62.0,
  "ant": "wlan0",
  "frame_len": 168,
  "protocol": "802.11",
  "id": "aa:bb:cc:dd:ee:ff",
  "bssid": "aa:bb:cc:dd:ee:ff",
  "ssid": "MyNetwork",
  "auth_mode": "[WPA2-PSK-CCMP][ESS]",
  "security": "WPA2",
  "channel": 6,
  "metadata": {"protocol": "802.11", "frame_subtype": "beacon"},
  "raw_frame_hex": "001122...",
  "lat": 48.8566,
  "lon": 2.3522,
  "alt": 35.0,
  "fix": 3
}
```

| Field | Description |
|-------|-------------|
| `record_type` | `observation` for captured frame records; legacy files may omit it |
| `id` / `bssid` | Source identifier / AP BSSID when known |
| `ssid` | Human-readable name (when parseable from frame) |
| `auth_mode` / `security` | Parsed AP security, WPA/WPA2/WPA3/OPEN/WEP when available |
| `channel` / `freq` | WiFi channel and centre frequency (Hz) |
| `metadata` | Full backend metadata dictionary, including parsed 802.11 fields and IEs |
| `raw_frame_hex` / `raw_frame_b64` | Raw captured frame bytes when `store_raw_frames = true` |
| `rssi` | RSSI at capture time (dBm) |
| `lat` / `lon` | Observer GPS position at capture time |
| `protocol` | Protocol string (`802.11`, `bt`, `lora`, etc.) |
| `t` | Unix timestamp (float seconds) |

### Wardriver GPS breadcrumb

Written by `wardriver` mode at the GPS poll interval, independently of frame captures.
These records let the web map display the real driven route even during quiet RF
sections or temporary adapter stalls. Source solvers ignore them.

```json
{
  "schema": "aetherward.session.v1",
  "record_type": "gps",
  "t": 1716000000.000,
  "lat": 48.8566,
  "lon": 2.3522,
  "alt": 35.0,
  "fix": 2,
  "accuracy_h": 3.5,
  "accuracy_v": 6.0,
  "num_sats": 9
}
```

| Field | Description |
|-------|-------------|
| `record_type` | Always `gps` |
| `lat` / `lon` / `alt` | GPS receiver position |
| `fix` | Numeric fix type from `AbsolutePosition.fix_type` |
| `accuracy_h` / `accuracy_v` | GPS accuracy when supplied by the backend |
| `num_sats` | Satellite count when supplied by the backend |
| `t` | GPS fix timestamp, or wall-clock time if the backend did not provide one |

### TDOA / ENU position

Written by `trilateration` mode after each solver run.

```json
{
  "id":     "source-0",
  "x_enu":   3.512,
  "y_enu":  -1.203,
  "z_enu":   0.0,
  "rssi":   -55.0,
  "method": "tdoa",
  "t":      1716000001.0
}
```

| Field | Description |
|-------|-------------|
| `id` | Source identifier |
| `x_enu` / `y_enu` / `z_enu` | Position in local ENU frame (metres) |
| `rssi` | Mean RSSI across antennas for this solve |
| `method` | Always `"tdoa"` |
| `t` | Solve timestamp |

### Array sensing event

Written by `array_sensing` mode on each state transition.

```json
{
  "type":       "presence",
  "antenna_id": "wlan0",
  "variance":   0.183,
  "direction":  [0.707, 0.707, 0.0],
  "t":          1716000002.5
}
```

| Field | Description |
|-------|-------------|
| `type` | `"presence"`, `"motion"`, or `"absence"` |
| `antenna_id` | Antenna that triggered the event |
| `variance` | EMA-smoothed variance at trigger time |
| `direction` | Unit ENU vector toward most-excited antenna (null if single antenna) |
| `t` | Event timestamp |

### Antenna geometry marker

Optionally written at session start to describe array geometry (used by the ENU viewer).

```json
{
  "method":    "antenna",
  "id":        "ant-ref",
  "x_enu":     0.0,
  "y_enu":     0.0,
  "z_enu":     0.0,
  "t":         1716000000.0
}
```

---

## Filtering with jq

```bash
# All WiFi sources
jq 'select(.protocol == "wifi")' session.jsonl

# Only positions within 50 m of a point
jq --argjson lat 48.8566 --argjson lon 2.3522 \
   'select(.lat != null) | select(((.lat - $lat) * 111320)^2 + ((.lon - $lon) * 85000)^2 < 2500)' \
   session.jsonl

# Presence events only
jq 'select(.type == "presence")' session.jsonl

# Per-source observation counts
jq -s 'group_by(.id) | map({id: .[0].id, count: length})' session.jsonl
```

---

## Loading in Python

```python
import json
from pathlib import Path

records = [json.loads(line) for line in Path('session.jsonl').read_text().splitlines()]
wardriver = [r for r in records if 'lat' in r]
tdoa      = [r for r in records if r.get('method') == 'tdoa']
events    = [r for r in records if 'type' in r]
```
