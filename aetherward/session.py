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
        'id': ('identifier', 'bssid', 'sta', 'station', 'client', 'mac'),
        'identifier': ('id', 'bssid', 'sta', 'station', 'client', 'mac'),
        'auth_mode': ('security',),
        # Relationship aliases appear in older sessions and in some backend
        # versions.  Keep them here so the solver/web map can build AP↔client
        # links from the same data the user sees in popups/tables.
        'bssid': ('associated_bssid', 'associated', 'ap_bssid', 'ap_mac', 'ap', 'id', 'identifier'),
        'associated_bssid': ('associated', 'associated_ap', 'ap_bssid', 'ap_mac', 'ap'),
        'linked_client': ('client', 'station', 'sta', 'linked_station', 'associated_client'),
        'client': ('station', 'sta', 'mac', 'linked_client'),
        'station': ('client', 'sta', 'mac', 'linked_client'),
        'source_role': ('role', 'kind'),
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


def _norm_macish(value: Any) -> str:
    return str(value or '').strip().lower()


def _bad_relation_mac(value: Any) -> bool:
    v = _norm_macish(value)
    return v in ('', 'ff:ff:ff:ff:ff:ff', '00:00:00:00:00:00', 'none', 'null')


def _clean_ssid(value: Any) -> Any:
    # Older/imported captures sometimes used the literal placeholder
    # "defaultSSID" when the SSID element was absent or empty.  That is not a
    # real network name; keep it empty so the UI can say "<empty SSID>".
    if isinstance(value, str) and value.strip().lower() == 'defaultssid':
        return ''
    return value


def source_meta_from_record(rec: dict) -> dict:
    keys = (
        'ssid', 'protocol', 'auth_mode', 'security', 'bssid', 'channel',
        'band', 'frame_type', 'frame_subtype', 'privacy', 'akm_suites',
        'pairwise_ciphers', 'group_cipher', 'vendor_ouis', 'capabilities',
        'beacon_interval', 'addr1', 'addr2', 'addr3', 'source_role',
        'client', 'station', 'associated_bssid', 'associated', 'ap_bssid',
        'ap_mac', 'associated_ap', 'linked_client', 'linked_station',
        'associated_client', 'to_ds', 'from_ds',
    )
    meta = {k: record_get(rec, k) for k in keys}
    if 'ssid' in meta:
        meta['ssid'] = _clean_ssid(meta.get('ssid'))
    meta = {k: v for k, v in meta.items() if v not in (None, [], {}) and (v != '' or k == 'ssid')}

    # Never manufacture a self-referential AP/client relation.  This used to
    # happen when associated_bssid fell back to bssid, producing popups like
    # "Linked AP: aa:bb:..." on the same client MAC.
    self_ids = {_norm_macish(record_get(rec, k)) for k in ('id', 'identifier', 'client', 'station')}
    self_ids |= {_norm_macish(meta.get(k)) for k in ('id', 'identifier', 'client', 'station')}
    self_ids = {x for x in self_ids if x}
    assoc = _norm_macish(meta.get('associated_bssid'))
    if assoc and (_bad_relation_mac(assoc) or assoc in self_ids):
        meta.pop('associated_bssid', None)
    linked = _norm_macish(meta.get('linked_client'))
    ap_ids = {_norm_macish(record_get(rec, k)) for k in ('id', 'identifier', 'bssid')}
    ap_ids |= {_norm_macish(meta.get(k)) for k in ('id', 'identifier', 'bssid')}
    ap_ids = {x for x in ap_ids if x}
    if linked and (_bad_relation_mac(linked) or linked in ap_ids):
        meta.pop('linked_client', None)

    freq = rec.get('freq')
    if freq is not None:
        try:
            meta['freq_mhz'] = round(float(freq) / 1e6, 3)
        except (TypeError, ValueError):
            pass
    return meta
