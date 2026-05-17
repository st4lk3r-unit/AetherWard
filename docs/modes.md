# Modes

AetherWard runs in one of three modes set by the `mode` key in your config.

---

## `wardriver`

Scans one or more antennas across RF channels. Every captured frame is tagged with the current GPS position and written to a JSONL session file. The RSS solver estimates source positions from RSSI measurements at multiple known observer locations.

### How it works

1. Channels are partitioned across antennas — no two antennas dwell on the same channel simultaneously.
2. Frames are grouped into `SignalSource` objects by `(frequency, identifier)`, where identifier is BSSID, MAC, or device ID extracted from the protocol header.
3. Anonymous frames (no parseable identifier) are bucketed by frequency + 1-second time window.
4. GPS is polled on a configurable interval; each observation snapshot includes the fix at capture time.

### Config keys

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `channels` | `list[int]` | 1–13 | Channels to scan |
| `hop_interval` | `float` | `0.1` | Seconds per channel dwell |
| `output_path` | `string` | — | JSONL session output path |

### RSS solver

Model: `RSSI_i = A − 10·n·log₁₀(d_i)`

- `A` = RSSI at 1 m (proxy for TX power + antenna gain)
- `n` = path loss exponent (tuned with `--n-exp`)
- `d_i` = distance from observer i to transmitter

Solved jointly for transmitter position and A using Gauss-Newton least squares. Requires ≥3 observations with meaningful spread. Falls back to RSSI-weighted centroid when the system is too ill-conditioned.

**Choosing `n_exp`:**

| Value | Environment |
|-------|-------------|
| `2.0` | Free space / rooftop LOS |
| `2.5` | Typical urban outdoor ← default |
| `3.0` | Suburban / light obstruction |
| `3.5–4.5` | Indoor / heavy NLOS |

---

## `trilateration`

All antennas tune to the same frequency. When a frame arrives at every antenna, the time-difference-of-arrival (TDOA) values are fed to a Gauss-Newton solver that computes the emitter position in local ENU coordinates.

### How it works

1. Frames are grouped by `(frequency, time_bucket)`.
2. A background worker discards incomplete groups after `group_timeout` seconds.
3. When all N antennas have contributed a frame, `tdoa_solve()` is called with the reference antenna as t = 0.
4. The solver outputs a `RelativePosition` (local ENU, metres) with a 3×3 covariance matrix, plus a projected `AbsolutePosition` via the GPS anchor.

### Solver internals

Multi-start Gauss-Newton — centroid seed plus 6 axis-displaced seeds to escape local minima.

Covariance at convergence:

```
Cov(s) = (σ_τ² / dof) · (J'J)⁻¹
```

`residual` in metres is included in every result so callers can reject outlier fixes.

An optional C library (`libaw.so`) provides ~10× faster solving via ctypes. The pure-Python fallback is always active when the library is absent.

### Timing requirements

| Sync source | TDOA σ | Position error |
|-------------|--------|----------------|
| GPSDO + HW timestamp | ~1 ns | ~0.3 m |
| PPS + HW timestamp | ~10 ns | ~3 m |
| PPS + WiFi chip | ~100 ns | ~30 m |
| Software / NTP | ~100 µs | unusable |

### Config keys

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `channel` | `int` | — | Channel all antennas tune to |
| `reference_antenna` | `string` | — | Antenna ID used as t = 0 |
| `correlation_window` | `float` | `0.001` | Maximum expected TDOA in seconds |
| `group_timeout` | `float` | `0.05` | Discard incomplete groups after (seconds) |

### Geometry requirements

- Minimum: 4 antennas (1 reference + 3 non-collinear sensors)
- Collinear antenna arrays degrade to 2-D at best
- Adding a vertical offset (e.g. one antenna at z = 1 m) enables 3-D solving

---

## `array_sensing`

Passive presence and motion detection via RSSI or CSI variance. Does not locate a specific emitter. Useful for occupancy detection, perimeter sensing, or activity monitoring when GPS positioning is not the goal.

### Pipeline

```
frames → rolling window → EMA-smoothed variance
      → state machine (idle ↔ active)
      → SensingEvent(type, antenna_id, variance, direction)
```

### State machine

- `idle → active` when `smoothed_excess > sensitivity` → fires `presence` or `motion`
- `active → idle` when `smoothed_excess < sensitivity × hysteresis` → fires `absence`

### Direction estimation

When ≥2 antennas are calibrated, the mode computes a unit ENU vector toward the most-excited antenna. Direction quality improves with antenna spacing > 0.25λ.

### CSI vs RSSI

The pipeline works with either input:
- **CSI (Channel State Information):** subcarrier-level amplitude/phase; richer, but requires specific hardware (Intel 5300 + linux-80211n-csitool, Nexmon-patched Broadcom)
- **RSSI fallback:** magnitude only; works on any NIC but is noisier

### Config keys

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `channel` | `int` | — | Channel to monitor |
| `history_len` | `int` | `100` | Rolling window depth (frames) |
| `calibration_frames` | `int` | `50` | Baseline frames before events fire |
| `sensitivity` | `float` | `0.05` | Variance increase to trigger event |
| `hysteresis` | `float` | `0.4` | Return-to-idle fraction of sensitivity |
| `ema_alpha` | `float` | `0.3` | EMA smoothing weight (0–1) |
