from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
import re


_DEFAULT_SESSION_BASE = Path.home() / '.aetherward' / 'sessions'


def default_session_dir() -> Path:
    """Return AetherWard's default directory for recorded session files."""
    return _DEFAULT_SESSION_BASE


def _safe_session_stem(value: object | None, fallback: str = 'session') -> str:
    if isinstance(value, str):
        stem = value.strip() or fallback
    else:
        stem = fallback
    stem = re.sub(r'[^A-Za-z0-9_.-]+', '-', stem).strip('.-')
    return stem or fallback


def default_session_path(array_id: str | None = None,
                         mode: str | None = None,
                         suffix: str = '.jsonl',
                         now: datetime | None = None,
                         base_dir: str | Path | None = None) -> str:
    """
    Build a per-run session filename under ~/.aetherward/sessions.

    The path is intentionally timestamped so repeated `aetherward run NAME`
    calls do not silently append unrelated captures to the same file.
    """
    base = Path(base_dir).expanduser() if base_dir is not None else default_session_dir()
    if not suffix.startswith('.'):
        suffix = '.' + suffix
    stamp = (now or datetime.now()).strftime('%Y%m%d-%H%M%S')
    stem = _safe_session_stem(array_id, _safe_session_stem(mode, 'session'))
    return str(base / f'{stem}-{stamp}{suffix}')




def record_type(rec: dict) -> str:
    """Return the normalized session record type.

    Legacy observation records usually have no explicit type, so they are
    treated as observations by readers unless marked otherwise.
    """
    rt = rec.get('record_type', rec.get('type', 'observation'))
    return str(rt or 'observation').lower()


def is_gps_record(rec: dict) -> bool:
    return record_type(rec) == 'gps'


def is_observation_record(rec: dict) -> bool:
    return not is_gps_record(rec)


def record_metadata(rec: dict) -> dict:
    meta = rec.get('metadata')
    return meta if isinstance(meta, dict) else {}


def record_get(rec: dict, key: str, default: Any = None) -> Any:
    val = rec.get(key, None)
    if val not in (None, '', [], {}):
        return val
    meta = record_metadata(rec)
    if key in meta and meta[key] not in (None, '', [], {}):
        return meta[key]
    aliases = {
        'id': ('identifier', 'bssid', 'sta', 'mac'),
        'identifier': ('id', 'bssid', 'sta', 'mac'),
        'auth_mode': ('security',),
        'bssid': ('id', 'identifier'),
    }
    for alias in aliases.get(key, ()): 
        val = rec.get(alias, None)
        if val not in (None, '', [], {}):
            return val
        val = meta.get(alias, None)
        if val not in (None, '', [], {}):
            return val
    return default


def record_source_id(rec: dict) -> str:
    sid = record_get(rec, 'id')
    if sid:
        return str(sid)
    freq = rec.get('freq', rec.get('frequency', 0)) or 0
    try:
        return f"anon:{float(freq):.0f}"
    except (TypeError, ValueError):
        return 'anon:0'


def source_meta_from_record(rec: dict) -> dict:
    keys = (
        'ssid', 'protocol', 'auth_mode', 'security', 'bssid', 'channel',
        'band', 'frame_type', 'frame_subtype', 'privacy', 'akm_suites',
        'pairwise_ciphers', 'group_cipher', 'vendor_ouis', 'capabilities',
        'beacon_interval', 'addr1', 'addr2', 'addr3',
    )
    meta = {k: record_get(rec, k) for k in keys}
    meta = {k: v for k, v in meta.items() if v not in (None, '', [], {})}
    freq = rec.get('freq')
    if freq is not None:
        try:
            meta['freq_mhz'] = round(float(freq) / 1e6, 3)
        except (TypeError, ValueError):
            pass
    return meta
