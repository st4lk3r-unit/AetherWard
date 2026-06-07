"""
AetherWard web interface — self-contained, stdlib only + aetherward.
"""
from __future__ import annotations

import html as _html_mod
import json
import math
import queue
import re as _re
import socketserver
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional, Any
from urllib.parse import urlparse, parse_qs

# ── Shared state ──────────────────────────────────────────────────────────────

_positions:    dict = {}
_sse_clients:  list = []
_sse_lock             = threading.Lock()
_state_lock           = threading.Lock()

_solve_thread: Optional[threading.Thread] = None
_solve_stop           = threading.Event()
_solve_session: str  = ''
_solve_follow: bool = False
_total_updates: int  = 0

_run_thread:   Optional[threading.Thread] = None
_run_stop             = threading.Event()
_run_proc:     Optional[subprocess.Popen] = None
_run_state_lock       = threading.Lock()

AW_HOME     = Path.home() / '.aetherward'
AW_CONFIGS  = AW_HOME / 'configs'
AW_SESSIONS = AW_HOME / 'sessions'
AW_LOGS     = AW_HOME / 'logs'
_NAME_RE    = _re.compile(r'^[\w.\-]+$')


# ── SSE helpers ───────────────────────────────────────────────────────────────

def _push_sse(payload: str) -> None:
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)


def _broadcast(rec: dict) -> None:
    global _total_updates
    with _state_lock:
        _positions[rec['id']] = rec
        _total_updates += 1
    _push_sse('data: ' + json.dumps({'type': 'position', **rec}) + '\n\n')


def _broadcast_remove(src_id: str) -> None:
    with _state_lock:
        _positions.pop(src_id, None)
    _push_sse('data: ' + json.dumps({'type': 'source_removed', 'id': src_id}) + '\n\n')


def _broadcast_log(line: str, source: str = 'run') -> None:
    _push_sse('data: ' + json.dumps({'type': 'log', 'source': source, 'text': line}) + '\n\n')


def _broadcast_path_ready(path: str, name: str, *, overview: bool = True) -> None:
    """Tell connected browsers that a session has a cheap map path ready.

    Bulk solve must not auto-load full RF dot clouds for every fat session; that
    is what exhausted small Pis.  The browser handles this event by loading a
    GPS/route-only preview with a low point budget.
    """
    _push_sse('data: ' + json.dumps({
        'type': 'session_path_ready', 'path': path, 'name': name,
        'overview': bool(overview),
    }) + '\n\n')


# ── ANSI → HTML ───────────────────────────────────────────────────────────────

def _ansi_to_html(text: str) -> str:
    result = []
    depth  = 0
    for tok in _re.split(r'(\x1b\[[0-9;]*m)', text):
        m = _re.match(r'\x1b\[([0-9;]*)m', tok)
        if m:
            codes = m.group(1)
            parts = codes.split(';') if codes else ['0']
            if not parts[0] or parts[0] == '0':
                result.append('</span>' * depth)
                depth = 0
            elif parts[0] == '38' and len(parts) >= 5 and parts[1] == '2':
                r2, g2, b2 = int(parts[2]), int(parts[3]), int(parts[4])
                result.append(f'<span style="color:rgb({r2},{g2},{b2})">')
                depth += 1
            elif parts[0] == '30':
                result.append('<span style="color:transparent">')
                depth += 1
        else:
            result.append(_html_mod.escape(tok))
    result.append('</span>' * depth)
    return ''.join(result)




# ── Solving / map performance helpers ────────────────────────────────────────

# Fat wardrive sessions can contain tens of thousands of observations for a
# single MAC, often repeated at nearly identical GPS coordinates during slow
# traffic or held-GPS periods.  Feeding every duplicate into the Gauss-Newton
# RSS solver is expensive and gives very little extra geometry.  The web solver
# therefore aggregates observations into small geo-cells per source and caps the
# cell count.  CLI/offline tools can still consume the raw JSONL when required.
_SOLVE_CELL_M = 8.0
_MAX_SOLVE_CELLS_PER_SOURCE = 256
_MAP_MAX_GPS = 2500
_MAP_MAX_OBS = 6000
_MAP_BULK_PREVIEW_POINTS = 900


def _as_float(value, default=None):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _geo_cell_key(lat: float, lon: float, cell_m: float = _SOLVE_CELL_M) -> tuple[int, int]:
    lat = float(lat); lon = float(lon)
    lat_deg = max(cell_m / 111_320.0, 1e-9)
    cos_lat = max(0.18, abs(math.cos(math.radians(lat))))
    lon_deg = max(cell_m / (111_320.0 * cos_lat), 1e-9)
    return (int(round(lat / lat_deg)), int(round(lon / lon_deg)))


def _add_geo_observation(cells_by_sid: dict, sid: str, lat, lon, rssi,
                         max_cells: int = _MAX_SOLVE_CELLS_PER_SOURCE) -> bool:
    """Add an RSS observation to a bounded per-source geo-cell aggregate.

    Returns True if the source aggregate changed and should be re-solved.
    """
    lat_f = _as_float(lat); lon_f = _as_float(lon)
    if lat_f is None or lon_f is None:
        return False
    rssi_f = _as_float(rssi, -100.0)
    cells = cells_by_sid[sid]
    key = _geo_cell_key(lat_f, lon_f)
    if key in cells:
        c = cells[key]
        c['lat_sum'] += lat_f; c['lon_sum'] += lon_f
        c['rssi_sum'] += rssi_f; c['count'] += 1
        return True
    if len(cells) < max_cells:
        cells[key] = {'lat_sum': lat_f, 'lon_sum': lon_f,
                      'rssi_sum': rssi_f, 'count': 1}
        return True

    # Reservoir is full.  Keep strong/nearby evidence preferentially instead of
    # blindly appending forever.  More positive RSSI is stronger.
    weakest_key = None
    weakest_rssi = 1e9
    for k, c in cells.items():
        avg = c['rssi_sum'] / max(1, c['count'])
        if avg < weakest_rssi:
            weakest_rssi = avg; weakest_key = k
    if weakest_key is not None and rssi_f > weakest_rssi:
        cells.pop(weakest_key, None)
        cells[key] = {'lat_sum': lat_f, 'lon_sum': lon_f,
                      'rssi_sum': rssi_f, 'count': 1}
        return True
    return False


def _obs_from_cells(cells: dict) -> list[tuple[float, float, float]]:
    out = []
    for c in cells.values():
        n = max(1, c.get('count', 1))
        out.append((c['lat_sum'] / n, c['lon_sum'] / n, c['rssi_sum'] / n))
    return out



def _norm_macish(value: Any) -> str:
    return str(value or '').strip().lower()


def _bad_relation_id(value: Any) -> bool:
    v = _norm_macish(value)
    return v in ('', 'ff:ff:ff:ff:ff:ff', '00:00:00:00:00:00', 'none', 'null')


def _clean_ssid_value(value: Any) -> Any:
    if isinstance(value, str) and value.strip().lower() == 'defaultssid':
        return ''
    return value


def _self_ids_from_meta(meta: dict) -> set[str]:
    return {_norm_macish(meta.get(k)) for k in ('id', 'identifier', 'client', 'station') if _norm_macish(meta.get(k))}


def _clean_relation_meta(meta: dict) -> dict:
    if not meta:
        return meta
    if 'ssid' in meta:
        meta['ssid'] = _clean_ssid_value(meta.get('ssid'))
    self_ids = _self_ids_from_meta(meta)
    assoc = _norm_macish(meta.get('associated_bssid'))
    if assoc and (_bad_relation_id(assoc) or assoc in self_ids):
        meta.pop('associated_bssid', None)
    ap_ids = {_norm_macish(meta.get(k)) for k in ('id', 'identifier', 'bssid') if _norm_macish(meta.get(k))}
    linked = _norm_macish(meta.get('linked_client'))
    if linked and (_bad_relation_id(linked) or linked in ap_ids):
        meta.pop('linked_client', None)
    return meta


def _merge_source_meta(dst: dict, src: dict) -> dict:
    """Merge metadata accumulated across observations for one source.

    A relation field is often not present on the first frame that creates a
    source.  Keeping only the first metadata record means the UI can later show
    a client source but miss its AP association, so AP↔client links disappear.
    Fill missing fields opportunistically, with relationship fields treated as
    important aliases.
    """
    if dst is None:
        dst = {}
    for k, v in (src or {}).items():
        if k == 'ssid':
            v = _clean_ssid_value(v)
        if v in (None, '', [], {}):
            # Keep explicitly empty SSIDs empty; do not replace them with fake
            # placeholders such as defaultSSID.
            if k == 'ssid' and dst.get(k) is None:
                dst[k] = ''
            continue
        if dst.get(k) in (None, '', [], {}):
            dst[k] = v

    # Canonicalize relationship aliases, but never fall back from plain bssid
    # to associated_bssid here.  For some frames bssid is just the source ID;
    # using it as a linked AP creates self-links.
    for alias in ('associated', 'associated_ap', 'ap_bssid', 'ap_mac', 'ap'):
        val = dst.get(alias)
        if dst.get('associated_bssid') in (None, '', [], {}) and val not in (None, '', [], {}):
            dst['associated_bssid'] = val
    for alias in ('linked_station', 'associated_client'):
        val = dst.get(alias)
        if dst.get('linked_client') in (None, '', [], {}) and val not in (None, '', [], {}):
            dst['linked_client'] = val
    return _clean_relation_meta(dst)


def _solve_rss_position(sid: str, cells: dict, meta: dict, n_exp: float,
                        min_obs: int, rss_solve, rssi_centroid) -> dict | None:
    obs = _obs_from_cells(cells)
    if len(obs) < min_obs:
        return None
    raw_samples = sum(max(1, c.get('count', 1)) for c in cells.values())
    rss = rss_solve(obs, n_exp=n_exp)
    if rss is not None:
        return {**meta, 'id': sid, 't': time.time(),
                'pos_method': 'rss_trilateration',
                'sample_cells': len(obs), 'raw_samples': raw_samples, **rss}
    lat, lon = rssi_centroid(obs)
    return {**meta, 'id': sid, 't': time.time(),
            'pos_method': 'rssi_centroid', 'lat': lat, 'lon': lon,
            'samples': raw_samples, 'sample_cells': len(obs),
            'raw_samples': raw_samples}


def _safe_int(value, default: int, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _session_record_to_map_row(rec: dict, raw_all: bool = False) -> dict | None:
    """Return the browser-facing row for /api/session/records."""
    if raw_all:
        return rec
    if rec.get('lat') is None or rec.get('lon') is None:
        return None
    from aetherward.session import is_gps_record, record_source_id, source_meta_from_record
    if is_gps_record(rec):
        return {
            'record_type': 'gps', 'source': 'gps', 'id': 'GPS track',
            'lat': rec['lat'], 'lon': rec['lon'], 'alt': rec.get('alt'),
            't': rec.get('t', 0), 'fix': rec.get('fix'),
            'accuracy_h': rec.get('accuracy_h'), 'num_sats': rec.get('num_sats'),
        }
    meta = _clean_relation_meta(source_meta_from_record(rec))
    row = {**meta, 'record_type': 'observation', 'lat': rec['lat'], 'lon': rec['lon'],
           't': rec.get('t', 0), 'rssi': rec.get('rssi'), 'id': record_source_id(rec),
           'freq': rec.get('freq'), 'protocol': meta.get('protocol', rec.get('protocol', ''))}
    for k in ('gps_held', 'gps_hold_s', 'gps_age_s'):
        if k in rec:
            row[k] = rec[k]
    return row


def _iter_session_rows(path: Path, raw_all: bool = False):
    with path.open('r', encoding='utf-8', errors='replace') as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            row = _session_record_to_map_row(rec, raw_all=raw_all)
            if row is not None:
                yield row


def _decimated_session_rows(path: Path, max_gps: int, max_obs: int) -> list[dict]:
    """Return a bounded, file-order-preserving map sample for fat sessions."""
    gps_total = obs_total = 0
    for row in _iter_session_rows(path, raw_all=False):
        if row.get('record_type') == 'gps' or row.get('source') == 'gps':
            gps_total += 1
        else:
            obs_total += 1
    gps_step = max(1, math.ceil(gps_total / max(1, max_gps)))
    obs_step = max(1, math.ceil(obs_total / max(1, max_obs)))
    gps_i = obs_i = 0
    rows: list[dict] = []
    for row in _iter_session_rows(path, raw_all=False):
        is_gps = row.get('record_type') == 'gps' or row.get('source') == 'gps'
        if is_gps:
            gps_i += 1
            keep = gps_i == 1 or gps_i == gps_total or ((gps_i - 1) % gps_step == 0)
            if keep:
                row['sampled_total'] = gps_total
                rows.append(row)
        else:
            obs_i += 1
            keep = obs_i == 1 or obs_i == obs_total or ((obs_i - 1) % obs_step == 0)
            if keep:
                row['sampled_total'] = obs_total
                rows.append(row)
    return rows


def _overview_session_rows(path: Path, max_points: int = _MAP_BULK_PREVIEW_POINTS) -> list[dict]:
    """Return a tiny route preview suitable for auto-loading after Solve All.

    Prefer GPS breadcrumbs for the route.  If the file has no GPS records, fall
    back to geotagged observations so old/partial sessions still show a path.
    RF observation dots are intentionally omitted here; manual Map Path loads
    those with the normal decimated endpoint.
    """
    gps_rows: list[dict] = []
    obs_rows: list[dict] = []
    for row in _iter_session_rows(path, raw_all=False):
        if row.get('lat') is None or row.get('lon') is None:
            continue
        if row.get('record_type') == 'gps' or row.get('source') == 'gps':
            gps_rows.append(row)
        elif not gps_rows:
            # Only keep observation fallback until we know GPS exists.
            obs_rows.append(row)
    base = gps_rows if gps_rows else obs_rows
    total = len(base)
    if total <= max_points:
        rows = list(base)
    else:
        step = max(1, math.ceil(total / max(1, max_points)))
        rows = [r for i, r in enumerate(base, 1)
                if i == 1 or i == total or ((i - 1) % step == 0)]
    for row in rows:
        row['record_type'] = 'gps' if (row.get('record_type') == 'gps' or row.get('source') == 'gps') else 'route'
        row['source'] = 'gps' if row['record_type'] == 'gps' else row.get('source', 'route')
        row['overview'] = True
        row['sampled_total'] = total
    return rows



def _row_ids(row: dict) -> set[str]:
    ids: set[str] = set()
    meta = row.get('metadata') if isinstance(row.get('metadata'), dict) else {}
    for k in ('id', 'identifier', 'source', 'bssid', 'client', 'station', 'mac'):
        for obj in (row, meta):
            v = _norm_macish(obj.get(k))
            if v and not _bad_relation_id(v):
                ids.add(v)
    return ids


def _row_relation_ids(row: dict) -> tuple[set[str], set[str]]:
    """Return (client_ids, ap_ids) inferred from explicit metadata and 802.11 addr roles."""
    clients: set[str] = set()
    aps: set[str] = set()
    role = str(row.get('source_role') or row.get('role') or '').lower()
    rid = _norm_macish(row.get('id') or row.get('identifier'))
    if rid and not _bad_relation_id(rid):
        if role in ('client', 'station', 'sta'):
            clients.add(rid)
        elif role in ('ap', 'access_point', 'access-point'):
            aps.add(rid)
    for k in ('client', 'station'):
        v = _norm_macish(row.get(k))
        if v and not _bad_relation_id(v):
            clients.add(v)
    for k in ('associated_bssid', 'associated', 'associated_ap', 'ap_bssid', 'ap_mac', 'ap'):
        v = _norm_macish(row.get(k))
        if v and not _bad_relation_id(v):
            aps.add(v)
    for k in ('linked_client', 'linked_station', 'associated_client'):
        v = _norm_macish(row.get(k))
        if v and not _bad_relation_id(v):
            clients.add(v)

    a1 = _norm_macish(row.get('addr1'))
    a2 = _norm_macish(row.get('addr2'))
    a3 = _norm_macish(row.get('addr3'))
    to_ds = bool(row.get('to_ds'))
    from_ds = bool(row.get('from_ds'))
    subtype = str(row.get('frame_subtype') or '').lower()
    if to_ds and not from_ds and not _bad_relation_id(a1) and not _bad_relation_id(a2):
        clients.add(a2); aps.add(a1)
    elif from_ds and not to_ds and not _bad_relation_id(a1) and not _bad_relation_id(a2):
        clients.add(a1); aps.add(a2)
    elif subtype in ('assoc_req', 'reassoc_req', 'association_req', 'reassociation_req'):
        if not _bad_relation_id(a2): clients.add(a2)
        if not _bad_relation_id(a1): aps.add(a1)
        elif not _bad_relation_id(a3): aps.add(a3)
    elif subtype in ('assoc_resp', 'reassoc_resp', 'association_resp', 'reassociation_resp'):
        if not _bad_relation_id(a1): clients.add(a1)
        if not _bad_relation_id(a2): aps.add(a2)
        elif not _bad_relation_id(a3): aps.add(a3)

    # Reject self-pairs explicitly.
    both = clients & aps
    clients -= both
    aps -= both
    return clients, aps


def _row_matches_source(row: dict, source: str, role: str = '') -> bool:
    src = _norm_macish(source)
    if not src:
        return False
    if src in _row_ids(row):
        return True
    clients, aps = _row_relation_ids(row)
    if role == 'ap' and src in aps:
        return True
    if role == 'client' and src in clients:
        return True
    return False


def _source_sample_rows(path: Path, source: str, role: str = '', max_obs: int = 1500) -> list[dict]:
    """Return bounded RF observation rows linked to one source.

    Bulk route previews intentionally omit RF dots.  This endpoint lets the
    browser lazily hydrate sample links for a clicked source without loading a
    whole fat session into Leaflet.
    """
    matches: list[dict] = []
    for row in _iter_session_rows(path, raw_all=False):
        if row.get('record_type') == 'gps' or row.get('source') == 'gps':
            continue
        if row.get('lat') is None or row.get('lon') is None:
            continue
        if _row_matches_source(row, source, role):
            matches.append(row)
    total = len(matches)
    if total > max_obs:
        step = max(1, math.ceil(total / max(1, max_obs)))
        matches = [r for i, r in enumerate(matches, 1)
                   if i == 1 or i == total or ((i - 1) % step == 0)]
    for row in matches:
        row['sampled_total'] = total
        row['source_sample'] = True
    return matches


def _batch_solve_sessions(max_sessions: int = 500) -> list[dict]:
    return [s for s in _list_sessions()
            if s['stype'] in ('wardriver', 'tdoa_raw', 'unknown')][:max_sessions]


# ── Solver thread ─────────────────────────────────────────────────────────────

def _run_solver(session_path: str, config_name: Optional[str],
                n_exp: float, min_obs: int, follow: bool = False) -> None:
    from collections import defaultdict
    from aetherward.position.rss import rss_solve, rssi_centroid
    from aetherward.session import is_gps_record, record_source_id, source_meta_from_record

    array = None; tdoa_solve = None; corr_win = 1e-3; ref_id = ''
    if config_name:
        try:
            import sys
            sys_path = str(Path(__file__).parent.parent)
            if sys_path not in sys.path:
                sys.path.insert(0, sys_path)
            from cli.aetherward import _load_config_file
            from cli._commands import _build_array_from_cfg
            from aetherward.core import tdoa_solve as _ts
            cfg = _load_config_file(config_name)
            array = _build_array_from_cfg(cfg); tdoa_solve = _ts
            corr_win = cfg.mode_config.get('correlation_window', 1e-3)
            ref_id   = cfg.antennas[0].id if cfg.antennas else ''
        except Exception:
            array = None

    rss_cells: dict = defaultdict(dict)
    rss_meta: dict = {}
    dirty_sids: set[str] = set()
    tdoa_buf: dict = defaultdict(list)
    solved: dict   = {}
    file_pos = 0
    _total_recs = 0
    _geo_recs   = 0
    _pass       = 0
    _idle_passes = 0

    while not _solve_stop.is_set():
        _pass += 1
        try:
            with open(session_path) as fh:
                fh.seek(file_pos); lines = fh.readlines(); file_pos = fh.tell()
        except FileNotFoundError:
            break
        new_recs = 0
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            new_recs += 1
            if is_gps_record(rec):
                # GPS breadcrumbs are route samples, not RF observations.
                # Feeding them to the RSS solver creates a fake anon:0 source
                # and can make the live map look like frames disappeared.
                continue
            sid = record_source_id(rec)
            if rec.get('lat') is not None and rec.get('lon') is not None:
                _geo_recs += 1
                if _add_geo_observation(rss_cells, sid, rec.get('lat'), rec.get('lon'),
                                        rec.get('rssi', -100.0)):
                    dirty_sids.add(sid)
            rss_meta[sid] = _merge_source_meta(rss_meta.get(sid, {}), source_meta_from_record(rec))
            rss_meta[sid].setdefault('ssid', '')
            rss_meta[sid].setdefault('protocol', '')
            if tdoa_solve and rec.get('ant'):
                bucket = int(rec.get('t', 0.0) / corr_win)
                tdoa_buf[(sid, bucket)].append(rec)

        solve_now = list(dirty_sids)
        dirty_sids.clear()
        for sid in solve_now:
            pos_rec = _solve_rss_position(
                sid, rss_cells.get(sid, {}), rss_meta.get(sid, {}),
                n_exp, min_obs, rss_solve, rssi_centroid)
            if pos_rec is None:
                continue
            prev = solved.get(sid)
            if prev is None or _changed(prev, pos_rec):
                solved[sid] = pos_rec; _broadcast(pos_rec)
                label = rss_meta.get(sid, {}).get('ssid') or sid
                _broadcast_log(
                    f'[solved] {label} → {pos_rec["lat"]:.5f},{pos_rec["lon"]:.5f}'
                    f'  ({pos_rec["pos_method"]}, {pos_rec.get("raw_samples", pos_rec.get("samples", "?"))} obs, '
                    f'{pos_rec.get("sample_cells", "?")} cells)',
                    'solve')

        _total_recs += new_recs
        # After first complete read: warn and stop if file has records but none are geo-tagged
        if _pass == 1 and _total_recs > 0 and _geo_recs == 0:
            _broadcast_log(
                f'! No geo-tagged observations found in {_total_recs} record(s). '
                'RSS solver needs lat/lon fields. '
                'For ENU/TDOA sessions use the Array / ENU tab instead.',
                'solve')
            break
        # Warn (but keep looping) if geo-tagged data exists but no source meets min_obs
        if _pass == 1 and _geo_recs > 0:
            solvable = sum(1 for cells in rss_cells.values() if len(cells) >= min_obs)
            if solvable == 0:
                max_seen = max((len(c) for c in rss_cells.values()), default=0)
                _broadcast_log(
                    f'! {_geo_recs} geo-tagged record(s) across {len(rss_cells)} source(s); '
                    f'max {max_seen} geo-cell(s)/source — need ≥{min_obs}. '
                    'Reduce "Min observations" or collect more passes.',
                    'solve')

        if tdoa_solve:
            ready = [(k, g) for k, g in tdoa_buf.items()
                     if len({r['ant'] for r in g}) >= 3]
            for key, group in ready:
                sid = key[0]
                ant_ids = {r['ant'] for r in group}
                ref = next((r for r in group if r['ant'] == ref_id), group[0])
                meas = [{'antenna_id': r['ant'], 'tdoa': r['t']-ref['t'],
                         'rssi': r.get('rssi', -100.0), 'timestamp': r['t']}
                        for r in group]
                result = tdoa_solve(array, meas, ref['ant'])
                if result and result.get('valid'):
                    pos = result.get('position_absolute') or result.get('position_relative')
                    if pos and getattr(pos, 'lat', None):
                        pos_rec = {**rss_meta.get(sid, {}), 'id': sid, 't': time.time(),
                                   'pos_method': 'tdoa', 'lat': pos.lat, 'lon': pos.lon,
                                   'residual_m': result.get('residual'),
                                   'antennas': len(ant_ids)}
                        solved[sid] = pos_rec; _broadcast(pos_rec)
                tdoa_buf.pop(key, None)
        # Static one-shot solves are finite: consume the current file once,
        # emit positions, then return to idle.  Only explicit live-follow mode
        # should keep polling for appended records.
        if not follow:
            _broadcast_log(
                f'Solver done — {len(solved)} source(s) positioned from {_total_recs} record(s).',
                'solve')
            break
        _solve_stop.wait(2.0)


def _changed(prev: dict, cur: dict, thr_m: float = 5.0) -> bool:
    import math
    dlat = (cur.get('lat', 0.0) - prev.get('lat', 0.0)) * 111_320.0
    dlon = ((cur.get('lon', 0.0) - prev.get('lon', 0.0))
            * 111_320.0 * math.cos(math.radians(cur.get('lat', 0.0) or 0.0)))
    return math.sqrt(dlat*dlat + dlon*dlon) > thr_m




def _run_batch_solver(max_cells: int = _MAX_SOLVE_CELLS_PER_SOURCE,
                      max_sessions: int = 500,
                      n_exp: float = 2.5,
                      min_obs: int = 3) -> None:
    """Pi-friendly bulk solver for many saved sessions.

    It runs in the same background slot as the normal solver so status/Stop work
    and HTTP requests do not block until every fat JSONL is processed.
    """
    from collections import defaultdict
    from aetherward.position.rss import rss_solve, rssi_centroid
    from aetherward.session import is_gps_record, record_source_id, source_meta_from_record

    sessions = _batch_solve_sessions(max_sessions)
    total_solved = 0
    total_records = 0
    _broadcast_log(
        f'[batch] starting {len(sessions)} session(s), ≤{max_cells} geo-cells/source, '
        f'min_obs={min_obs}, n_exp={n_exp:g}; auto-loading lightweight route previews',
        'solve')
    try:
        for sess_i, sess in enumerate(sessions, 1):
            if _solve_stop.is_set():
                _broadcast_log('[batch] stopped by user', 'solve')
                break
            rss_cells: dict = defaultdict(dict)
            rss_meta: dict = {}
            rec_n = geo_n = 0
            try:
                with open(sess['path'], encoding='utf-8', errors='replace') as fh:
                    for raw in fh:
                        if _solve_stop.is_set():
                            break
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            rec = json.loads(raw)
                        except ValueError:
                            continue
                        rec_n += 1
                        if is_gps_record(rec):
                            continue
                        sid = record_source_id(rec)
                        if rec.get('lat') is not None and rec.get('lon') is not None:
                            geo_n += 1
                            _add_geo_observation(
                                rss_cells, sid, rec.get('lat'), rec.get('lon'),
                                rec.get('rssi', -100.0), max_cells=max_cells)
                        rss_meta[sid] = _merge_source_meta(rss_meta.get(sid, {}), source_meta_from_record(rec))
                        rss_meta[sid].setdefault('ssid', '')
                        rss_meta[sid].setdefault('protocol', '')
            except Exception as exc:
                _broadcast_log(f'[batch] skip {sess["name"]}: {exc}', 'solve')
                continue

            solved_this = 0
            for sid, cells in rss_cells.items():
                if _solve_stop.is_set():
                    break
                pos_rec = _solve_rss_position(
                    sid, cells, rss_meta.get(sid, {}), n_exp, min_obs,
                    rss_solve, rssi_centroid)
                if pos_rec is None:
                    continue
                _broadcast(pos_rec)
                total_solved += 1
                solved_this += 1

            total_records += rec_n
            _broadcast_log(
                f'[batch] {sess_i}/{len(sessions)} {sess["name"]}: '
                f'{solved_this} source(s), {geo_n}/{rec_n} geo observation record(s), '
                f'≤{max_cells} cells/source',
                'solve')
            _broadcast_path_ready(sess['path'], (sess.get('folder') + '/' if sess.get('folder') else '') + sess['name'], overview=True)
    finally:
        _broadcast_log(
            f'[batch] done — {total_solved} source(s) from {len(sessions)} session(s), '
            f'{total_records} record(s) scanned',
            'solve')


# ── Capture run thread ────────────────────────────────────────────────────────

def _run_capture(config_name: str, log_run: bool = False) -> None:
    global _run_proc
    import sys, shutil
    aw = shutil.which('aetherward')
    if aw:
        cmd = [aw, 'run', config_name]
    else:
        aw_py = str(Path(__file__).parent / 'aetherward.py')
        cmd   = [sys.executable, aw_py, 'run', config_name]
    if log_run:
        cmd.append('--log')
    _broadcast_log(f'[start] {" ".join(cmd)}')
    try:
        with _run_state_lock:
            _run_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, cwd=str(Path(__file__).parent.parent))
        for line in _run_proc.stdout:
            line = line.rstrip()
            if line:
                _broadcast_log(line)
            if _run_stop.is_set():
                break
        _run_proc.wait()
        _broadcast_log(f'[exit] code={_run_proc.returncode}')
    except Exception as e:
        _broadcast_log(f'[error] {e}')
    finally:
        with _run_state_lock:
            if _run_proc:
                try:
                    _run_proc.terminate()
                except Exception:
                    pass
            _run_proc = None


# ── Data helpers ──────────────────────────────────────────────────────────────

def _session_type(p: Path) -> str:
    """Peek at first valid record to classify session type."""
    try:
        for line in p.open():
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get('lat') is not None and r.get('lon') is not None:
                return 'wardriver'
            if r.get('x_enu') is not None or r.get('y_enu') is not None:
                return 'enu'
            if r.get('type') in ('presence', 'motion', 'absence'):
                return 'sensing'
            if r.get('ant') is not None:
                return 'tdoa_raw'
            return 'unknown'
    except Exception:
        pass
    return 'unknown'


def _list_sessions() -> list[dict]:
    if not AW_SESSIONS.exists():
        return []
    result = []
    for p in sorted(AW_SESSIONS.rglob('*.jsonl'),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            st = p.stat()
            n  = sum(1 for ln in p.open() if ln.strip())
        except OSError:
            continue
        rel    = p.relative_to(AW_SESSIONS)
        folder = str(rel.parent) if rel.parent != Path('.') else ''
        result.append({'name': p.name, 'path': str(p), 'size': st.st_size,
                       'records': n, 'stype': _session_type(p),
                       'folder': folder,
                       'mtime': time.strftime('%Y-%m-%d %H:%M',
                                              time.localtime(st.st_mtime))})
    return result


def _list_configs() -> list[dict]:
    if not AW_CONFIGS.exists():
        return []
    result = []
    for p in sorted(AW_CONFIGS.iterdir()):
        if p.suffix not in ('.json', '.toml', '.yaml', '.yml'):
            continue
        try:
            import sys
            sys_path = str(Path(__file__).parent.parent)
            if sys_path not in sys.path:
                sys.path.insert(0, sys_path)
            from cli.aetherward import _load_config_file
            cfg = _load_config_file(str(p))
            result.append({'name': p.stem, 'path': str(p),
                           'mode': cfg.mode, 'antennas': len(cfg.antennas)})
        except Exception:
            result.append({'name': p.stem, 'path': str(p), 'mode': '?', 'antennas': 0})
    return result


def _get_config_raw(name: str) -> dict | None:
    for ext in ('.toml', '.json', '.yaml', '.yml'):
        p = AW_CONFIGS / f'{name}{ext}'
        if p.exists():
            return {'name': name, 'content': p.read_text(), 'path': str(p)}
    return None


def _detect_hardware() -> dict:
    result: dict = {'wifi_ifaces': [], 'gpsd': False, 'rtlsdr': False,
                    'c_core': False, 'pps': False, 'serial': []}
    # WiFi interfaces
    try:
        r = subprocess.run(['iw', 'dev'], capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            result['wifi_ifaces'] = _re.findall(r'Interface (\S+)', r.stdout)
    except Exception:
        pass
    if not result['wifi_ifaces']:
        try:
            with open('/proc/net/wireless') as f:
                for line in f.readlines()[2:]:
                    iface = line.split(':')[0].strip()
                    if iface:
                        result['wifi_ifaces'].append(iface)
        except Exception:
            pass
    # gpsd
    try:
        import socket as _sock
        s = _sock.create_connection(('localhost', 2947), timeout=1)
        s.close(); result['gpsd'] = True
    except Exception:
        pass
    # Serial ports (GPS dongles appear here as /dev/ttyUSB* or /dev/ttyACM*)
    import glob as _glob
    result['serial'] = sorted(
        _glob.glob('/dev/ttyUSB*') + _glob.glob('/dev/ttyACM*')
    )
    # RTL-SDR
    try:
        import importlib as _il
        _il.import_module('rtlsdr'); result['rtlsdr'] = True
    except ImportError:
        pass
    # C core
    try:
        from aetherward.core import c_available
        result['c_core'] = c_available()
    except Exception:
        pass
    # PPS
    try:
        result['pps'] = any(Path('/dev').glob('pps*'))
    except Exception:
        pass
    return result


def _status() -> dict:
    from aetherward import __version__
    from aetherward.core import c_available
    with _state_lock:
        running     = _solve_thread is not None and _solve_thread.is_alive()
        run_running = _run_thread   is not None and _run_thread.is_alive()
        session = _solve_session; follow = _solve_follow
        updates = _total_updates; sources = len(_positions)
    return {'version': __version__,
            'c_core': 'loaded' if c_available() else 'not available (Python fallback)',
            'home': str(AW_HOME), 'sessions_dir': str(AW_SESSIONS),
            'configs_dir': str(AW_CONFIGS),
            'solve_running': running, 'solve_session': session or '—',
            'solve_mode': 'live-follow' if follow and running else 'finite',
            'run_running': run_running,
            'sources': sources, 'total_updates': updates}


# ── HTTP handler ──────────────────────────────────────────────────────────────

# ── Colour — sourced from shared palette ──────────────────────────────────────
import cli.palette as _pal

_TTY   = _pal.TTY
_C_ACC = _pal.ACC;  _C_RED = _pal.RED;  _C_DRD = _pal.DRD
_C_TXT = _pal.TXT;  _C_MU  = _pal.MU;   _C_VMU = _pal.VMU
_C_GRN = _pal.GRN;  _C_YLW = _pal.YLW;  _C_CYN = _pal.CYN
_C_ORG = _pal.ORG;  _C_RST = _pal.RST

def _wc(text: str, code: str) -> str:
    return _pal.wc(text, code)

def _web_log(method: str, path: str, code: int) -> None:
    if code >= 400:  col = _C_ACC
    elif code >= 300: col = _C_YLW
    else:             col = _C_GRN
    ts_s = _pal.wc(f'[{time.strftime("%H:%M:%S")}]', _pal.DIM)
    sep  = _wc('·', _C_DRD)
    meth = _wc(f'{method:<4}', _C_CYN)
    path_s = _pal.wc(path, _pal.SKY)
    sc   = _wc(str(code), col)
    print(f'  {ts_s} {sep} {meth} {path_s} {sep} {sc}', flush=True)

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        code = int(args[1]) if len(args) > 1 else 0
        path = str(args[0]).split()[1] if args else '?'
        _web_log(self.command, path, code)

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')

    def _json(self, data, code: int = 200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self._cors(); self.end_headers(); self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204); self._cors()
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        p = parsed.path; qs = parse_qs(parsed.query)

        if p in ('/', '/index.html'):
            body = _HTML.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
        elif p == '/api/status':
            self._json(_status())
        elif p == '/api/sessions':
            self._json(_list_sessions())
        elif p == '/api/configs':
            self._json(_list_configs())
        elif p == '/api/config/raw':
            name = (qs.get('name') or [''])[0].strip()
            if not name or not _NAME_RE.match(name):
                self._json({'error': 'invalid name'}, 400); return
            rec = _get_config_raw(name)
            self._json(rec) if rec else self._json({'error': 'not found'}, 404)
        elif p == '/api/positions/all':
            with _state_lock:
                self._json(list(_positions.values()))
        elif p == '/api/session/records':
            name = (qs.get('path') or [''])[0].strip()
            raw_all = (qs.get('raw') or [''])[0] == '1'
            map_mode = (qs.get('map') or [''])[0] == '1'
            p_obj = Path(name)
            if (p_obj.suffix != '.jsonl' or
                    not str(p_obj.resolve()).startswith(str(AW_SESSIONS.resolve()))):
                self._json({'error': 'not allowed'}, 403); return
            records = []
            if p_obj.exists():
                if map_mode and not raw_all:
                    overview = (qs.get('overview') or [''])[0] == '1'
                    if overview:
                        max_points = _safe_int((qs.get('max_points') or [''])[0],
                                               _MAP_BULK_PREVIEW_POINTS, 10, 5000)
                        records = _overview_session_rows(p_obj, max_points=max_points)
                    else:
                        max_gps = _safe_int((qs.get('max_gps') or [''])[0], _MAP_MAX_GPS, 100, 20000)
                        max_obs = _safe_int((qs.get('max_obs') or [''])[0], _MAP_MAX_OBS, 100, 50000)
                        records = _decimated_session_rows(p_obj, max_gps=max_gps, max_obs=max_obs)
                else:
                    records = list(_iter_session_rows(p_obj, raw_all=raw_all))
            self._json(records)
        elif p == '/api/session/source_samples':
            name = (qs.get('path') or [''])[0].strip()
            source = (qs.get('source') or [''])[0].strip()
            role = (qs.get('role') or [''])[0].strip().lower()
            max_obs = _safe_int((qs.get('max_obs') or [''])[0], 1500, 25, 10000)
            p_obj = Path(name)
            if (p_obj.suffix != '.jsonl' or
                    not str(p_obj.resolve()).startswith(str(AW_SESSIONS.resolve()))):
                self._json({'error': 'not allowed'}, 403); return
            if not source:
                self._json({'error': 'source required'}, 400); return
            records = _source_sample_rows(p_obj, source, role=role, max_obs=max_obs) if p_obj.exists() else []
            self._json(records)
        elif p == '/api/banner':
            bp = Path(__file__).parent.parent / 'banner.txt'
            if bp.exists():
                body = _ansi_to_html(bp.read_text()).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body)
            else:
                self._json({'error': 'banner not found'}, 404)
        elif p == '/api/detect':
            self._json(_detect_hardware())
        elif p == '/api/session/download':
            name = (qs.get('path') or [''])[0].strip()
            p_obj = Path(name)
            if (p_obj.suffix != '.jsonl' or
                    not str(p_obj.resolve()).startswith(str(AW_SESSIONS.resolve()))):
                self._json({'error': 'not allowed'}, 403); return
            if not p_obj.exists():
                self._json({'error': 'not found'}, 404); return
            body = p_obj.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'application/x-ndjson')
            self.send_header('Content-Disposition',
                             f'attachment; filename="{p_obj.name}"')
            self.send_header('Content-Length', str(len(body)))
            self._cors(); self.end_headers(); self.wfile.write(body)
        elif p == '/api/events':
            self._sse()
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        global _solve_thread, _solve_stop, _solve_session, _solve_follow
        global _run_thread, _run_stop, _run_proc
        p    = urlparse(self.path).path
        body = b''
        if 'Content-Length' in self.headers:
            body = self.rfile.read(int(self.headers['Content-Length']))
        data = json.loads(body) if body else {}

        # ── Solve ──────────────────────────────────────────────────────────────
        if p == '/api/solve/start':
            session = data.get('session', '')
            if not session or not Path(session).exists():
                self._json({'error': 'session not found'}, 400); return
            _solve_stop.set()
            if _solve_thread and _solve_thread.is_alive():
                _solve_thread.join(timeout=3)
            follow = bool(data.get('follow', False))
            _solve_stop.clear(); _solve_session = session; _solve_follow = follow
            _solve_thread = threading.Thread(
                target=_run_solver,
                args=(session, data.get('config'), data.get('n_exp', 2.5),
                      data.get('min_obs', 3), follow), daemon=True)
            _solve_thread.start()
            self._json({'ok': True, 'session': session})

        elif p == '/api/solve/stop':
            _solve_stop.set(); _solve_session = ''; _solve_follow = False
            self._json({'ok': True})

        elif p == '/api/solve/batch':
            max_cells = _safe_int(data.get('max_cells'), _MAX_SOLVE_CELLS_PER_SOURCE, 16, 2048)
            max_sessions = _safe_int(data.get('max_sessions'), 500, 1, 5000)
            try:
                n_exp = float(data.get('n_exp', 2.5))
            except (TypeError, ValueError):
                n_exp = 2.5
            min_obs = _safe_int(data.get('min_obs'), 3, 1, 1000)
            _solve_stop.set()
            if _solve_thread and _solve_thread.is_alive():
                _solve_thread.join(timeout=3)
            _solve_stop.clear(); _solve_session = 'batch'; _solve_follow = False
            _solve_thread = threading.Thread(
                target=_run_batch_solver, args=(max_cells, max_sessions, n_exp, min_obs), daemon=True)
            _solve_thread.start()
            sessions = _batch_solve_sessions(max_sessions)
            self._json({'ok': True, 'started': True, 'max_cells_per_source': max_cells,
                        'max_sessions': max_sessions, 'n_exp': n_exp, 'min_obs': min_obs,
                        'sessions': sessions})

        # ── Capture run ────────────────────────────────────────────────────────
        elif p == '/api/run/start':
            cfg_name = data.get('config', '').strip()
            log_run = bool(data.get('log'))
            if not cfg_name:
                self._json({'error': 'config required'}, 400); return
            _run_stop.set()
            if _run_thread and _run_thread.is_alive():
                _run_thread.join(timeout=3)
            _run_stop.clear()
            _run_thread = threading.Thread(
                target=_run_capture, args=(cfg_name, log_run), daemon=True)
            _run_thread.start()
            self._json({'ok': True, 'config': cfg_name, 'log': log_run})

        elif p == '/api/run/stop':
            _run_stop.set()
            with _run_state_lock:
                if _run_proc:
                    try: _run_proc.terminate()
                    except Exception: pass
            self._json({'ok': True})

        # ── Config CRUD ────────────────────────────────────────────────────────
        elif p == '/api/config/save':
            name = data.get('name', '').strip(); content = data.get('content', '')
            if not name or not _NAME_RE.match(name):
                self._json({'error': 'invalid name (letters, digits, _ - . only)'}, 400); return
            if not content.strip():
                self._json({'error': 'content is empty'}, 400); return
            AW_CONFIGS.mkdir(parents=True, exist_ok=True)
            cfg_path = AW_CONFIGS / f'{name}.toml'
            cfg_path.write_text(content)
            self._json({'ok': True, 'path': str(cfg_path)})

        elif p == '/api/config/delete':
            name = data.get('name', '').strip()
            if not name or not _NAME_RE.match(name):
                self._json({'error': 'invalid name'}, 400); return
            for ext in ('.toml', '.json', '.yaml', '.yml'):
                cfg_path = AW_CONFIGS / f'{name}{ext}'
                if cfg_path.exists():
                    cfg_path.unlink(); self._json({'ok': True}); return
            self._json({'error': 'not found'}, 404)

        # ── Session CRUD ───────────────────────────────────────────────────────
        elif p == '/api/session/delete':
            path = data.get('path', '').strip()
            if not path:
                self._json({'error': 'path required'}, 400); return
            p_obj = Path(path)
            if (p_obj.suffix != '.jsonl' or
                    not str(p_obj.resolve()).startswith(str(AW_SESSIONS.resolve()))):
                self._json({'error': 'not allowed'}, 403); return
            if p_obj.exists():
                p_obj.unlink(); self._json({'ok': True})
            else:
                self._json({'error': 'not found'}, 404)

        elif p == '/api/session/import':
            name = data.get('name', '').strip()
            content = data.get('content', '')
            if not name:
                self._json({'error': 'name required'}, 400); return
            stem = name.replace('.jsonl', '')
            if not _NAME_RE.match(stem):
                self._json({'error': 'invalid name'}, 400); return
            if not name.endswith('.jsonl'):
                name += '.jsonl'
            AW_SESSIONS.mkdir(parents=True, exist_ok=True)
            dest = AW_SESSIONS / name
            dest.write_text(content)
            self._json({'ok': True, 'path': str(dest)})

        elif p == '/api/session/rename':
            path = data.get('path', '').strip(); name = data.get('name', '').strip()
            if not path or not name:
                self._json({'error': 'path and name required'}, 400); return
            if not name.endswith('.jsonl'):
                name += '.jsonl'
            stem = name.replace('.jsonl', '')
            if not _NAME_RE.match(stem):
                self._json({'error': 'invalid name'}, 400); return
            p_obj = Path(path); new_path = p_obj.parent / name
            if p_obj.exists():
                p_obj.rename(new_path)
                self._json({'ok': True, 'path': str(new_path)})
            else:
                self._json({'error': 'not found'}, 404)

        # ── Source CRUD ────────────────────────────────────────────────────────
        elif p == '/api/source/delete':
            src_id = data.get('id', '')
            if not src_id:
                self._json({'error': 'id required'}, 400); return
            _broadcast_remove(src_id)
            self._json({'ok': True})

        elif p in ('/api/source/add', '/api/source/edit'):
            rec = dict(data)
            if 'id' not in rec:
                self._json({'error': 'id required'}, 400); return
            rec.setdefault('pos_method', 'manual')
            rec.setdefault('t', time.time())
            _broadcast(rec)
            self._json({'ok': True})

        else:
            self._json({'error': 'not found'}, 404)

    def _sse(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self._cors(); self.end_headers()

        q: queue.Queue = queue.Queue(maxsize=200)
        with _sse_lock:
            _sse_clients.append(q)

        with _state_lock:
            snapshot = list(_positions.values())
        for rec in snapshot:
            try:
                self.wfile.write(
                    ('data: ' + json.dumps({'type': 'position', **rec}) + '\n\n').encode())
            except OSError:
                break
        try:
            self.wfile.flush()
        except OSError:
            pass

        try:
            while True:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(msg.encode()); self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b': keepalive\n\n'); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _sse_lock:
                if q in _sse_clients:
                    _sse_clients.remove(q)


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ── HTML ──────────────────────────────────────────────────────────────────────

import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from cli._html_ui import _HTML_UI
from cli._html_js import _HTML_JS
_HTML = _HTML_UI + _HTML_JS



# ── Entry point ───────────────────────────────────────────────────────────────

def _cmd_web(args) -> None:
    host         = getattr(args, 'host', '127.0.0.1')
    port         = getattr(args, 'port', 8080)
    open_browser = getattr(args, 'open_browser', False)

    for attempt in range(10):
        try:
            server = _ThreadedHTTPServer((host, port + attempt), _Handler)
            port   = port + attempt
            break
        except OSError:
            continue
    else:
        print(f'  Could not bind to any port in {port}–{port+9}')
        return

    url = f'http://{host}:{port}'
    def _p(s): print(s, flush=True)
    _p(_wc('─'*52, _C_DRD))
    _p(f'  {_wc("AetherWard", _C_ACC)}  {_pal.wc("web interface", _pal.DIM)}')
    _p(f'  {_wc("─"*48, _C_DRD)}')
    _p(f'  {_pal.wc("url      ", _pal.DIM)} {_wc(url, _C_RED)}')
    _p(f'  {_pal.wc("sessions ", _pal.DIM)} {_pal.wc(str(AW_SESSIONS), _pal.SKY)}')
    _p(f'  {_pal.wc("configs  ", _pal.DIM)} {_pal.wc(str(AW_CONFIGS),  _pal.SKY)}')
    _p(_wc('─'*52, _C_DRD))
    _p(f'  {_pal.wc("Ctrl-C to stop  ·  requests logged below", _pal.DIM)}\n')

    if open_browser:
        import threading as _t
        _t.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _solve_stop.set()
        _run_stop.set()
        server.server_close()
        print('\n  Web server stopped.\n')
