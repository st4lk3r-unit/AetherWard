# Session format

AetherWard sessions are newline-delimited JSON (`.jsonl`). Each line is one
record and can be appended safely while a capture is running.

The current schema is explicit and protocol-agnostic:

```json
{
  "schema": "aetherward.session.v1",
  "record_type": "observation"
}
```

Older flat fields such as `lat`, `lon`, `rssi`, `freq`, `id`, and `ssid` may
still be emitted as compatibility aliases. New code should read the structured
fields first and use flat aliases only as fallback.

---

## Observation record

A captured RF observation. `signal` contains the protocol-independent envelope;
protocol-specific data lives under keys such as `wifi`.

```json
{
  "schema": "aetherward.session.v1",
  "record_type": "observation",
  "t": 1716000000.123,
  "session": {
    "id": "roof-drive-20260525-155800",
    "mode": "wardriver"
  },
  "receiver": {
    "antenna_id": "wlan0"
  },
  "observer": {
    "lat": 48.8566,
    "lon": 2.3522,
    "alt_m": 35.0,
    "accuracy_h_m": 3.5,
    "accuracy_v_m": 8.0,
    "fix_type": 3,
    "num_sats": 10,
    "timestamp": 1716000000.0
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
    "frame_type": "0/8",
    "channel": 1,
    "auth_mode": "WPA2-PSK",
    "encryption": "WPA2",
    "cipher": "CCMP",
    "akm": "PSK",
    "privacy": true
  }
}
```

| Field | Description |
|-------|-------------|
| `schema` | Schema identifier, currently `aetherward.session.v1` |
| `record_type` | `observation`, `position`, `event`, or `antenna` |
| `t` | Event timestamp, Unix seconds |
| `session.id` | Timestamped session ID, usually the file stem |
| `session.mode` | Capture mode that produced the record |
| `receiver.antenna_id` | Physical/logical receiver ID |
| `observer.*` | Receiver position and GPS quality at capture time |
| `signal.protocol` | Generic protocol name, e.g. `802.11`, `bt`, `lora`, `sdr` |
| `signal.id` | Source identifier when known |
| `signal.frequency_hz` | Centre frequency in Hz |
| `signal.bandwidth_hz` | Occupied/nominal bandwidth in Hz when known |
| `signal.rssi_dbm` | Received signal strength in dBm |
| `wifi.*` | 802.11-specific metadata; omit for non-WiFi protocols |

---

## Position record

A solved/estimated source position. These are analysis results, not raw capture
coordinates.

```json
{
  "schema": "aetherward.session.v1",
  "record_type": "position",
  "id": "aa:bb:cc:dd:ee:ff",
  "protocol": "802.11",
  "estimated_lat": 48.8569,
  "estimated_lon": 2.3519,
  "method": "rss_path_loss_fit",
  "samples": 24,
  "confidence": "low",
  "confidence_radius_m": 95.0,
  "observer_span_m": 180.0,
  "gps_accuracy_mean_m": 4.2,
  "residual_dBm": 8.7,
  "ssid": "Home",
  "channel": 1,
  "auth_mode": "WPA2-PSK",
  "t": 1716000020.0
}
```

Use these for local analysis and GeoJSON/KML map output. Do not treat them as
original observation positions for WiGLE-style exports.

---

## TDOA / ENU record

TDOA modes operate in a local East-North-Up frame. A record may contain ENU
coordinates, projected WGS84 coordinates, or both.

```json
{
  "schema": "aetherward.session.v1",
  "record_type": "position",
  "id": "source-0",
  "x_enu": 3.512,
  "y_enu": -1.203,
  "z_enu": 0.0,
  "method": "tdoa",
  "residual_m": 2.4,
  "t": 1716000001.0
}
```

---

## Array sensing event

```json
{
  "schema": "aetherward.session.v1",
  "record_type": "event",
  "type": "presence",
  "antenna_id": "wlan0",
  "variance": 0.183,
  "direction": [0.707, 0.707, 0.0],
  "t": 1716000002.5
}
```

---

## Antenna geometry marker

Antenna records describe local geometry for the ENU viewer.

```json
{
  "schema": "aetherward.session.v1",
  "record_type": "antenna",
  "method": "antenna",
  "id": "wlan0",
  "x_enu": 0.0,
  "y_enu": 0.0,
  "z_enu": 0.0,
  "t": 1716000000.0
}
```

---

## jq examples

```bash
# All 802.11 observations
jq 'select(.record_type == "observation") | select(.signal.protocol == "802.11" or .protocol == "802.11")' session.jsonl

# Observations with usable GPS accuracy
jq 'select(.record_type == "observation") | select((.observer.accuracy_h_m // .accuracy_h) <= 10)' session.jsonl

# Solved sources with confidence circles
jq 'select(.record_type == "position") | {id, estimated_lat, estimated_lon, confidence_radius_m}' session.jsonl

# Presence events only
jq 'select(.record_type == "event" and .type == "presence")' session.jsonl
```

---

## Loading in Python

```python
import json
from pathlib import Path

records = [json.loads(line) for line in Path("session.jsonl").read_text().splitlines() if line.strip()]
observations = [r for r in records if r.get("record_type") == "observation" or "observer" in r]
positions = [r for r in records if r.get("record_type") == "position" or r.get("method") in {"tdoa", "rss_path_loss_fit"}]
events = [r for r in records if r.get("record_type") == "event" or "type" in r]
```
