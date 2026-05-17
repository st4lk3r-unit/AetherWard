# Plugin API

Any class that implements `HardwareBackend` can be used as a backend. Place it
anywhere on `sys.path` and reference it by fully qualified class name:

```toml
[[antennas]]
backend = "my_package.my_module.MyBackend"
```

Or in the `plugins/` directory inside the project:

```toml
backend = "plugins.my_backend.MyBackend"
```

---

## Minimal backend

```python
from aetherward.hardware.backend import HardwareBackend, BackendCapabilities
from aetherward.signal.frame import Frame
from typing import Callable, Optional
import threading
import time

class MyBackend(HardwareBackend):

    def __init__(self, device: str = '/dev/ttyUSB0') -> None:
        self._device   = device
        self._callback: Optional[Callable[[Frame], None]] = None
        self._thread:   Optional[threading.Thread] = None
        self._running   = False

    def initialize(self) -> None:
        # Open device, load firmware, etc.
        pass

    def configure(self, config: dict) -> None:
        self._device = config.get('device', self._device)

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            frequency_min        = 433e6,
            frequency_max        = 434e6,
            bandwidth_max        = 250e3,
            supports_channel_hop = False,
            supports_csi         = False,
            supports_hw_timestamp= False,
            supports_tdoa_sync   = False,
            max_antennas         = 1,
        )

    def start_capture(self, callback: Callable[[Frame], None]) -> None:
        self._callback = callback
        self._running  = True
        self._thread   = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def stop_capture(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def set_frequency(self, hz: float) -> None:
        pass  # fixed-frequency hardware — ignore

    def close(self) -> None:
        self.stop_capture()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _reader(self) -> None:
        while self._running:
            raw, freq, rssi = self._read_one_frame()
            if raw and self._callback:
                self._callback(Frame(
                    data       = raw,
                    frequency  = freq,
                    bandwidth  = 250e3,
                    timestamp  = time.time(),
                    rssi       = rssi,
                    antenna_id = 'my-ant',
                    metadata   = {'protocol': 'myprotocol'},
                ))

    def _read_one_frame(self):
        # Replace with real hardware read
        time.sleep(0.1)
        return b'', 433.92e6, -70.0
```

---

## `Frame` fields

| Field | Type | Description |
|-------|------|-------------|
| `data` | `bytes` | Raw frame bytes |
| `frequency` | `float` | Centre frequency (Hz) |
| `bandwidth` | `float` | Capture bandwidth (Hz) |
| `timestamp` | `float` | Unix epoch, sub-second (hardware stamp preferred) |
| `rssi` | `float` | Received power at antenna port (dBm) |
| `antenna_id` | `str` | Originating antenna ID (must match config `id`) |
| `sample_rate` | `float` | Hz — for SDR IQ captures |
| `metadata` | `dict` | Protocol extras: `bssid`, `ssid`, `protocol`, `identifier`, etc. |

**`metadata['identifier']`** is the key used for source correlation in wardriver
mode. Set it to any stable string that uniquely identifies the emitter (MAC
address, device ID, ICAO code, etc.). Frames without an identifier are bucketed
by frequency + 1-second time window.

---

## CSI support

If your hardware exports Channel State Information, populate `Observation.csi`
in a custom mode or return it from `backend.get_csi()`:

```python
def get_csi(self) -> Optional[np.ndarray]:
    # shape: (n_subcarriers, n_rx_antennas)  complex64
    return self._last_csi
```

`ArraySensingMode` uses `csi` automatically when available; falls back to RSSI
variance when `csi` is None.

---

## Hardware timestamping

For TDOA accuracy, provide sub-microsecond hardware timestamps:

```python
timestamp = pps_epoch + hardware_counter_offset  # float seconds
```

Set `supports_hw_timestamp = True` and `supports_tdoa_sync = True` in
`BackendCapabilities` when PPS or GPSDO sync is active. This tells the
trilateration mode to trust TDOA values directly rather than applying software
correction.
