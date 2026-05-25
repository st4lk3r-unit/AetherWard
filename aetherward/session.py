from __future__ import annotations

import math
from typing import Any, Optional

SCHEMA = 'aetherward.session.v1'


def _finite(v: Any) -> bool:
    try:
        return v is not None and math.isfinite(float(v))
    except Exception:
        return False


def _clean(d: dict) -> dict:
    """Drop None and non-finite floats recursively for JSON friendliness."""
    out: dict = {}
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, float) and not math.isfinite(v):
            continue
        if isinstance(v, dict):
            vv = _clean(v)
            if vv:
                out[k] = vv
        elif isinstance(v, list):
            out[k] = v
        else:
            out[k] = v
    return out


def make_observation_record(*, frame, antenna_id: str, gps=None,
                            session_id: str = '', mode: str = 'warddriver') -> dict:
    """
    Build a protocol-agnostic v1 session observation record.

    The record is intentionally nested (receiver/signal/location/protocol), but
    also emits the historical flat keys used by older AetherWard tools/tests.
    """
    meta = dict(getattr(frame, 'metadata', {}) or {})
    protocol = meta.get('protocol') or 'unknown'
    source_id = meta.get('identifier') or meta.get('bssid') or meta.get('id') or ''

    signal = _clean({
        'protocol': protocol,
        'id': source_id,
        'frequency_hz': getattr(frame, 'frequency', None),
        'bandwidth_hz': getattr(frame, 'bandwidth', None),
        'rssi_dbm': getattr(frame, 'rssi', None),
        'sample_rate_hz': getattr(frame, 'sample_rate', None) or None,
    })
    receiver = _clean({'antenna_id': antenna_id})

    observer = None
    if gps is not None and gps.is_valid():
        observer = _clean({
            'lat': gps.lat,
            'lon': gps.lon,
            'alt_m': gps.alt,
            'accuracy_h_m': gps.accuracy_h,
            'accuracy_v_m': gps.accuracy_v,
            'fix_type': int(gps.fix_type),
            'num_sats': getattr(gps, 'num_sats', 0),
            'timestamp': getattr(gps, 'timestamp', 0.0) or None,
        })

    wifi = None
    if str(protocol).lower() in ('802.11', 'wifi', 'wi-fi'):
        wifi = _clean({
            'bssid': meta.get('bssid') or source_id or None,
            'ssid': meta.get('ssid'),
            'frame_type': meta.get('frame_type'),
            'channel': meta.get('channel'),
            'frequency_mhz': meta.get('frequency_mhz'),
            'auth_mode': meta.get('auth_mode'),
            'privacy': meta.get('privacy'),
            'encryption': meta.get('encryption'),
            'cipher': meta.get('cipher'),
            'akm': meta.get('akm'),
            'capabilities': meta.get('capabilities'),
        })

    rec = _clean({
        'schema': SCHEMA,
        'record_type': 'observation',
        't': getattr(frame, 'timestamp', None),
        'session': {'id': session_id, 'mode': mode} if session_id else {'mode': mode},
        'receiver': receiver,
        'observer': observer,
        'signal': signal,
        'wifi': wifi,
        'metadata': {k: v for k, v in meta.items()
                     if k not in {'protocol', 'identifier', 'bssid', 'ssid',
                                  'frame_type', 'channel', 'frequency_mhz',
                                  'auth_mode', 'privacy', 'encryption', 'cipher',
                                  'akm', 'capabilities'}},
    })

    # Backward-compatible flat aliases.  Keep old consumers alive while the
    # nested v1 schema becomes the canonical format.
    rec['freq'] = signal.get('frequency_hz', 0.0)
    rec['bw'] = signal.get('bandwidth_hz', 0.0)
    rec['rssi'] = signal.get('rssi_dbm', -100.0)
    rec['ant'] = antenna_id
    rec['protocol'] = protocol
    if source_id:
        rec['id'] = source_id
    if wifi:
        if wifi.get('ssid'):
            rec['ssid'] = wifi['ssid']
        if wifi.get('channel') is not None:
            rec['channel'] = wifi['channel']
        if wifi.get('auth_mode'):
            rec['auth_mode'] = wifi['auth_mode']
    if observer:
        rec['lat'] = observer['lat']
        rec['lon'] = observer['lon']
        rec['alt'] = observer.get('alt_m', 0.0)
        rec['fix'] = observer.get('fix_type')
        if observer.get('accuracy_h_m') is not None:
            rec['gps_accuracy_h'] = observer['accuracy_h_m']
        if observer.get('accuracy_v_m') is not None:
            rec['gps_accuracy_v'] = observer['accuracy_v_m']
    return rec


def source_id(rec: dict) -> str:
    sig = rec.get('signal') or {}
    wifi = rec.get('wifi') or {}
    return (sig.get('id') or wifi.get('bssid') or rec.get('id') or
            f"anon:{float(signal_frequency(rec) or rec.get('freq') or 0):.0f}")


def signal_frequency(rec: dict) -> Optional[float]:
    sig = rec.get('signal') or {}
    v = sig.get('frequency_hz', rec.get('freq'))
    return float(v) if _finite(v) else None


def signal_rssi(rec: dict, default: float = -100.0) -> float:
    sig = rec.get('signal') or {}
    v = sig.get('rssi_dbm', rec.get('rssi', default))
    try:
        return float(v)
    except Exception:
        return default


def receiver_id(rec: dict) -> str:
    return ((rec.get('receiver') or {}).get('antenna_id') or rec.get('ant') or '')


def observer_point(rec: dict) -> Optional[tuple[float, float, float, float, Optional[float]]]:
    """Return (lat, lon, alt_m, rssi_dbm, accuracy_h_m) for RSS solving."""
    obs = rec.get('observer') or {}
    lat = obs.get('lat', rec.get('lat'))
    lon = obs.get('lon', rec.get('lon'))
    if not (_finite(lat) and _finite(lon)):
        return None
    alt = obs.get('alt_m', rec.get('alt', 0.0))
    acc = obs.get('accuracy_h_m', rec.get('gps_accuracy_h'))
    return (float(lat), float(lon), float(alt) if _finite(alt) else 0.0,
            signal_rssi(rec), float(acc) if _finite(acc) else None)


def signal_meta(rec: dict) -> dict:
    sig = rec.get('signal') or {}
    wifi = rec.get('wifi') or {}
    meta = rec.get('metadata') or {}
    freq = signal_frequency(rec) or 0.0
    return _clean({
        'ssid': wifi.get('ssid') or rec.get('ssid') or '',
        'freq_mhz': round(freq / 1e6, 3),
        'protocol': sig.get('protocol') or rec.get('protocol') or '',
        'channel': wifi.get('channel') or rec.get('channel'),
        'auth_mode': wifi.get('auth_mode') or rec.get('auth_mode') or '',
        'privacy': wifi.get('privacy', rec.get('privacy')),
        'encryption': wifi.get('encryption') or rec.get('encryption') or meta.get('encryption') or '',
        'cipher': wifi.get('cipher') or rec.get('cipher') or meta.get('cipher') or '',
        'akm': wifi.get('akm') or rec.get('akm') or meta.get('akm') or '',
        'frame_type': wifi.get('frame_type') or rec.get('frame_type') or '',
    })


def oldstyle_observation(rec: dict) -> dict:
    """Flatten v1/legacy records for existing web path viewers."""
    pt = observer_point(rec)
    meta = signal_meta(rec)
    out = {
        't': rec.get('t', 0),
        'rssi': signal_rssi(rec),
        'id': source_id(rec),
        'ssid': meta.get('ssid', ''),
        'freq': signal_frequency(rec),
        'protocol': meta.get('protocol', ''),
        'channel': meta.get('channel'),
        'auth_mode': meta.get('auth_mode', ''),
        'privacy': meta.get('privacy'),
        'encryption': meta.get('encryption', ''),
        'cipher': meta.get('cipher', ''),
        'akm': meta.get('akm', ''),
        'frame_type': meta.get('frame_type', ''),
    }
    if pt:
        out.update({'lat': pt[0], 'lon': pt[1], 'alt': pt[2], 'gps_accuracy_h': pt[4]})
    return out
