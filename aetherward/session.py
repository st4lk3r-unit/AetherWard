from __future__ import annotations

from typing import Any


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
