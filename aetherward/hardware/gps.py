from __future__ import annotations

import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from ..position.absolute import AbsolutePosition, FixType


class GPSBackend(ABC):
    """Abstract real-world position source."""

    @abstractmethod
    def initialize(self) -> None: ...

    @abstractmethod
    def get_position(self) -> Optional[AbsolutePosition]: ...

    def get_pps_timestamp(self) -> Optional[float]:
        """Unix epoch of the last PPS pulse. None if not supported."""
        return None

    @abstractmethod
    def close(self) -> None: ...


# ── GNSS / gpsd ──────────────────────────────────────────────────────────────

class GPSDBackend(GPSBackend):
    """
    gpsd-based GNSS backend.
    Speaks the gpsd JSON-over-TCP protocol directly — no gpsd-py3 required.
    Accuracy: 1-10 m (consumer), <1 m (RTK).
    """

    def __init__(self, host: str = 'localhost', port: int = 2947):
        self._host = host
        self._port = port
        self._sock: Optional[object] = None
        self._buf  = ''

    def initialize(self) -> None:
        import socket, select as _sel
        try:
            s = socket.create_connection((self._host, self._port), timeout=5)
        except OSError as e:
            raise RuntimeError(
                f"Cannot connect to gpsd at {self._host}:{self._port}: {e}\n"
                "  Install gpsd:  sudo apt install gpsd\n"
                "  Start gpsd:    sudo gpsd /dev/ttyUSB0 -F /var/run/gpsd.sock\n"
                "  (replace /dev/ttyUSB0 with your dongle device)"
            )
        s.settimeout(None)  # blocking mode; we control timing via select
        self._sock = s
        # Drain the VERSION banner gpsd sends on connect
        deadline = time.time() + 1.0
        while time.time() < deadline:
            r, _, _ = _sel.select([s], [], [], 0.2)
            if not r:
                break
            try:
                s.recv(4096)
            except OSError:
                break

    def get_position(self) -> Optional[AbsolutePosition]:
        if self._sock is None:
            return None
        import json, select as _sel
        # Request current snapshot — no streaming race conditions
        try:
            self._sock.sendall(b'?POLL;\n')  # type: ignore[attr-defined]
        except OSError:
            self._sock = None
            return None
        # Read until we receive the POLL response (usually < 100 ms)
        deadline = time.time() + 2.0
        while time.time() < deadline:
            r, _, _ = _sel.select([self._sock], [], [], 0.3)
            if not r:
                break
            try:
                chunk = self._sock.recv(4096).decode('utf-8', errors='replace')  # type: ignore[attr-defined]
            except OSError:
                self._sock = None
                return None
            if not chunk:          # connection closed by gpsd
                self._sock = None
                return None
            self._buf += chunk
            while '\n' in self._buf:
                line, self._buf = self._buf.split('\n', 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    report = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if report.get('class') != 'POLL':
                    continue
                # POLL contains a list of TPV objects (one per device)
                for tpv in report.get('tpv', []):
                    _fix_map = {2: FixType.FIX_2D, 3: FixType.FIX_3D}
                    fix = _fix_map.get(tpv.get('mode', 0))
                    if fix is None:
                        continue
                    ts_raw = tpv.get('time', '')
                    try:
                        from datetime import datetime
                        ts = datetime.fromisoformat(
                            ts_raw.replace('Z', '+00:00')).timestamp()
                    except Exception:
                        ts = time.time()
                    return AbsolutePosition(
                        lat=tpv.get('lat', 0.0),
                        lon=tpv.get('lon', 0.0),
                        alt=tpv.get('alt', 0.0),
                        accuracy_h=tpv.get('eph', float('inf')),
                        accuracy_v=tpv.get('epv', float('inf')),
                        timestamp=ts,
                        fix_type=fix,
                    )
                return None  # POLL received, no device has a fix yet
        return None

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()  # type: ignore[attr-defined]
            except Exception:
                pass
            self._sock = None


class StaticGPSBackend(GPSBackend):
    """Fixed-position backend for benchtop / simulated setups."""

    def __init__(self, lat: float, lon: float, alt: float = 0.0):
        self._pos = AbsolutePosition(
            lat=lat, lon=lon, alt=alt,
            accuracy_h=0.0, accuracy_v=0.0,
            fix_type=FixType.FIX_3D,
        )

    def initialize(self) -> None:
        pass

    def get_position(self) -> AbsolutePosition:
        from dataclasses import replace
        return replace(self._pos, timestamp=time.time())

    def close(self) -> None:
        pass


# ── LBS: GeoClue2 ────────────────────────────────────────────────────────────

class GeoclueBackend(GPSBackend):
    """
    Linux GeoClue2 location service (D-Bus).

    Aggregates available sources: WiFi positioning, cell tower, GPS if present.
    The OS location daemon handles source selection and accuracy improvement
    automatically — no hardware-specific code needed here.

    Accuracy: 10-200 m typical (WiFi-dense urban areas ~10 m).
    Requires: geoclue-2.0 system package + python3-dbus or dbus-python.

    Install:
        sudo apt install geoclue-2.0 python3-dbus
    """

    def __init__(self) -> None:
        self._client     = None
        self._client_obj = None
        self._bus        = None

    def initialize(self) -> None:
        try:
            import dbus
        except ImportError:
            raise RuntimeError(
                "GeoclueBackend requires dbus-python.\n"
                "  sudo apt install python3-dbus  OR  pip install dbus-python"
            )
        try:
            bus     = dbus.SystemBus()
            manager = dbus.Interface(
                bus.get_object('org.freedesktop.GeoClue2',
                               '/org/freedesktop/GeoClue2/Manager'),
                'org.freedesktop.GeoClue2.Manager',
            )
            client_path     = manager.GetClient()
            self._client_obj = bus.get_object('org.freedesktop.GeoClue2', client_path)
            self._client     = dbus.Interface(
                self._client_obj, 'org.freedesktop.GeoClue2.Client'
            )
            self._client_obj.Set(
                'org.freedesktop.GeoClue2.Client', 'DesktopId', 'aetherward',
                dbus_interface='org.freedesktop.DBus.Properties',
            )
            self._client.Start()
            self._bus = bus
        except Exception as e:
            raise RuntimeError(f"GeoClue2 initialisation failed: {e}")

    def get_position(self) -> Optional[AbsolutePosition]:
        if self._client_obj is None or self._bus is None:
            return None
        try:
            import dbus
            props_iface = 'org.freedesktop.DBus.Properties'
            loc_path = str(self._client_obj.Get(
                'org.freedesktop.GeoClue2.Client', 'Location',
                dbus_interface=props_iface,
            ))
            if loc_path == '/':
                return None   # no fix yet
            loc   = self._bus.get_object('org.freedesktop.GeoClue2', loc_path)
            props = dbus.Interface(loc, props_iface)
            def get(k): return props.Get('org.freedesktop.GeoClue2.Location', k)
            lat   = float(get('Latitude'))
            lon   = float(get('Longitude'))
            acc   = float(get('Accuracy'))
            # Timestamp is a (seconds, microseconds) struct
            ts_struct = get('Timestamp')
            ts    = float(ts_struct[0]) + float(ts_struct[1]) / 1e6
            return AbsolutePosition(
                lat=lat, lon=lon, alt=0.0,
                accuracy_h=acc,
                timestamp=ts,
                fix_type=FixType.FIX_2D,
            )
        except Exception:
            return None

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.Stop()
            except Exception:
                pass
            self._client = None


# ── LBS: Mozilla Location Services ───────────────────────────────────────────

class MozillaLBSBackend(GPSBackend):
    """
    Mozilla Location Services (MLS) WiFi-based positioning.

    Scans nearby WiFi access points using `iw` and submits their BSSIDs +
    signal strengths to the MLS geolocation API.  The server returns a
    lat/lon + accuracy estimate.

    In a wardriving context the AP data is already being captured, so this
    source feeds naturally from the same scan.

    Accuracy: 10-200 m depending on AP density.
    Rate: avoid calling more than once per 5 s (scans are slow too).
    Requires: requests  (pip install requests)

    Note: MLS is rate-limited on the public 'test' key.  For production
    use, register for a proper API key at location.services.mozilla.com.
    """
    _DEFAULT_URL = 'https://location.services.mozilla.com/v1/geolocate?key=test'

    def __init__(self, interface: str = '', api_url: str = '') -> None:
        self._iface    = interface
        self._api_url  = api_url or self._DEFAULT_URL
        self._last_pos: Optional[AbsolutePosition] = None

    def initialize(self) -> None:
        try:
            import requests  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "MozillaLBSBackend requires requests: pip install requests"
            )

    def _resolve_iface(self) -> str:
        if self._iface:
            return self._iface
        net = Path('/sys/class/net')
        if net.exists():
            for iface in sorted(net.iterdir()):
                if (iface / 'wireless').exists() or (iface / 'phy80211').exists():
                    return iface.name
        return ''

    def _scan_aps(self) -> list[dict]:
        iface = self._resolve_iface()
        if not iface:
            return []
        aps: list[dict] = []
        try:
            r = subprocess.run(
                ['iw', 'dev', iface, 'scan'],
                capture_output=True, text=True, timeout=15,
            )
            bssid: Optional[str] = None
            rssi = -100
            for line in r.stdout.splitlines():
                line = line.strip()
                if line.startswith('BSS '):
                    if bssid:
                        aps.append({'macAddress': bssid, 'signalStrength': rssi})
                    bssid = line.split()[1][:17]
                    rssi  = -100
                elif 'signal:' in line:
                    try:
                        rssi = int(float(line.split('signal:')[1].split()[0]))
                    except (ValueError, IndexError):
                        pass
            if bssid:
                aps.append({'macAddress': bssid, 'signalStrength': rssi})
        except Exception:
            pass
        return aps

    def get_position(self) -> Optional[AbsolutePosition]:
        try:
            import requests
        except ImportError:
            return self._last_pos

        aps = self._scan_aps()
        if not aps:
            return self._last_pos

        try:
            resp = requests.post(
                self._api_url,
                json={'wifiAccessPoints': aps},
                timeout=8,
            )
            if resp.status_code != 200:
                return self._last_pos
            data = resp.json()
            loc  = data.get('location', {})
            if 'lat' not in loc:
                return self._last_pos
            pos = AbsolutePosition(
                lat=float(loc['lat']),
                lon=float(loc['lng']),
                alt=0.0,
                accuracy_h=float(data.get('accuracy', float('inf'))),
                timestamp=time.time(),
                fix_type=FixType.FIX_2D,
            )
            self._last_pos = pos
            return pos
        except Exception:
            return self._last_pos

    def close(self) -> None:
        pass


# ── LBS: IP geolocation (fallback) ───────────────────────────────────────────

class IPGeolocationBackend(GPSBackend):
    """
    IP-based geolocation — city-level accuracy, zero hardware required.

    Uses ip-api.com (free, no key, 45 req/min limit).
    Accuracy: 1-50 km depending on ISP and region.

    This is a last-resort fallback — use GNSS or WiFi-LBS when possible.
    Completely useless for trilateration or fine wardriving; acceptable
    for coarse session geo-tagging or when nothing else is available.
    """
    _URL = 'http://ip-api.com/json/'

    def initialize(self) -> None:
        try:
            import requests  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "IPGeolocationBackend requires requests: pip install requests"
            )

    def get_position(self) -> Optional[AbsolutePosition]:
        try:
            import requests
            resp = requests.get(self._URL, timeout=5)
            if resp.status_code != 200:
                return None
            d = resp.json()
            if d.get('status') != 'success':
                return None
            return AbsolutePosition(
                lat=float(d['lat']),
                lon=float(d['lon']),
                alt=0.0,
                accuracy_h=float(d.get('accuracy', 5000.0)),
                timestamp=time.time(),
                fix_type=FixType.FIX_2D,
            )
        except Exception:
            return None

    def close(self) -> None:
        pass
