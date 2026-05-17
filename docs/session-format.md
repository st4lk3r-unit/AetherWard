# Session format

Sessions are newline-delimited JSON (`.jsonl`). One record per observation,
appended as the session runs. All three mode types write to the same file; the
web UI and `solve`/`process` commands detect the record type from the fields present.

Sessions are plain text — open them with any editor, stream them with `tail -f`,
or post-process with `jq`.

---

## Record types

### Wardriver observation

Written by `wardriver` mode for every captured and correlated frame.

```json
{
  "id":       "aa:bb:cc:dd:ee:ff",
  "ssid":     "MyNetwork",
  "rssi":     -62.0,
  "lat":      48.8566,
  "lon":       2.3522,
  "freq":     2412000000,
  "protocol": "wifi",
  "t":        1716000000.123
}
```

| Field | Description |
|-------|-------------|
| `id` | Source identifier (BSSID, MAC, device ID) |
| `ssid` | Human-readable name (when parseable from frame) |
| `rssi` | RSSI at capture time (dBm) |
| `lat` / `lon` | Observer GPS position at capture time |
| `freq` | Centre frequency (Hz) |
| `protocol` | Protocol string (`wifi`, `bt`, `lora`, etc.) |
| `t` | Unix timestamp (float seconds) |

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
