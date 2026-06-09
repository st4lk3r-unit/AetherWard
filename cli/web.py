"""
AetherWard web interface — self-contained, stdlib only + aetherward.
"""
from __future__ import annotations

import html as _html_mod
import hashlib
import json
import base64
import sqlite3
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
_solve_progress: dict = {'running': False, 'phase': 'idle', 'pct': 0.0, 'text': 'idle'}
_active_solver_db: Optional[str] = None

_run_thread:   Optional[threading.Thread] = None
_run_stop             = threading.Event()
_run_proc:     Optional[subprocess.Popen] = None
_run_state_lock       = threading.Lock()

AW_HOME     = Path.home() / '.aetherward'
AW_CONFIGS  = AW_HOME / 'configs'
AW_SESSIONS = AW_HOME / 'sessions'
AW_LOGS     = AW_HOME / 'logs'
AW_SOLVER   = AW_HOME / 'solver'
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


def _json_safe(value):
    """Return strict-JSON-safe data.

    Python's json.dumps normally emits NaN/Infinity tokens, but browsers reject
    them when calling response.json().  Some GPS providers legitimately emit
    infinity-like accuracy values; those must become null at the API/SSE edge.
    Raw session JSONL is left untouched on disk.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return value


def _json_dumps(data) -> str:
    return json.dumps(_json_safe(data), allow_nan=False)


def _sse_event(data: dict) -> str:
    return 'data: ' + _json_dumps(data) + '\n\n'


def _anon_source_id(value: Any) -> str:
    """Short privacy-safe source label for progress/error logs."""
    return hashlib.sha1(str(value or '').encode('utf-8', 'ignore')).hexdigest()[:10]


def _broadcast_progress(**kwargs) -> None:
    global _solve_progress
    with _state_lock:
        cur = dict(_solve_progress)
        cur.update(kwargs)
        _solve_progress = cur
    _push_sse(_sse_event({'type': 'solve_progress', **cur}))


def _broadcast(rec: dict) -> None:
    global _total_updates
    with _state_lock:
        _positions[rec['id']] = rec
        _total_updates += 1
    _push_sse(_sse_event({'type': 'position', **rec}))


def _broadcast_remove(src_id: str) -> None:
    with _state_lock:
        _positions.pop(src_id, None)
    _push_sse(_sse_event({'type': 'source_removed', 'id': src_id}))


def _broadcast_log(line: str, source: str = 'run') -> None:
    _push_sse(_sse_event({'type': 'log', 'source': source, 'text': line}))


def _clear_auto_positions(reason: str = 'solver-reset') -> None:
    """Clear derived solver rows before a new finite/bulk solve.

    Keep user-pinned/manual rows, but remove RSS/RSSI/TDOA/evidence rows so a
    new Solve All run has deterministic semantics: the Positions tab reflects
    exactly the current run, not whatever was left from a previous single-file
    solve or aborted batch.
    """
    global _positions
    auto_methods = {'rss_trilateration', 'rssi_centroid', 'tdoa',
                    'observation_centroid', 'session_sample_centroid'}
    with _state_lock:
        keep = {k: v for k, v in _positions.items()
                if str(v.get('pos_method') or '').lower() not in auto_methods
                and not v.get('session_path')}
        removed = len(_positions) - len(keep)
        _positions = keep
    _push_sse(_sse_event({
        'type': 'positions_reset', 'reason': reason,
        'removed': removed, 'kept': list(keep.values()),
    }))


def _broadcast_path_ready(path: str, name: str, *, overview: bool = True) -> None:
    """Tell connected browsers that a session has a cheap map path ready.

    Bulk solve must not auto-load full RF dot clouds for every fat session; that
    is what exhausted small Pis.  The browser handles this event by loading a
    GPS/route-only preview with a low point budget.
    """
    _push_sse(_sse_event({
        'type': 'session_path_ready', 'path': path, 'name': name,
        'overview': bool(overview),
    }))


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
# Same BSSID/MAC is only a candidate identity.  When sample cells for one
# source form geographically separate components, split strong components and
# quarantine tiny far components instead of averaging lies into one AP.
_SOURCE_SPLIT_RADIUS_M = 700.0
_SOURCE_SPLIT_MIN_CELLS = 3
_SOURCE_SPLIT_MIN_RAW = 3


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




def _add_geo_cell_aggregate(cells_by_sid: dict, sid: str, cell: dict,
                            max_cells: int = _MAX_SOLVE_CELLS_PER_SOURCE) -> bool:
    """Merge an already-aggregated geo-cell into a bounded source aggregate."""
    n = max(1, int(cell.get('count', 1)))
    lat_f = _as_float(cell.get('lat_sum', 0.0) / n)
    lon_f = _as_float(cell.get('lon_sum', 0.0) / n)
    if lat_f is None or lon_f is None:
        return False
    rssi_f = _as_float(cell.get('rssi_sum', 0.0) / n, -100.0)
    cells = cells_by_sid[sid]
    key = _geo_cell_key(lat_f, lon_f)
    if key in cells:
        c = cells[key]
        c['lat_sum'] += lat_f * n
        c['lon_sum'] += lon_f * n
        c['rssi_sum'] += rssi_f * n
        c['count'] += n
        return True
    if len(cells) < max_cells:
        cells[key] = {'lat_sum': lat_f * n, 'lon_sum': lon_f * n,
                      'rssi_sum': rssi_f * n, 'count': n}
        return True
    weakest_key = None
    weakest_rssi = 1e9
    for k, c in cells.items():
        avg = c['rssi_sum'] / max(1, c['count'])
        if avg < weakest_rssi:
            weakest_rssi = avg; weakest_key = k
    if weakest_key is not None and rssi_f > weakest_rssi:
        cells.pop(weakest_key, None)
        cells[key] = {'lat_sum': lat_f * n, 'lon_sum': lon_f * n,
                      'rssi_sum': rssi_f * n, 'count': n}
        return True
    return False


def _merge_source_cells(dst_by_sid: dict, sid: str, src_cells: dict,
                        max_cells: int = _MAX_SOLVE_CELLS_PER_SOURCE) -> None:
    for cell in (src_cells or {}).values():
        _add_geo_cell_aggregate(dst_by_sid, sid, cell, max_cells=max_cells)

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




def _distance_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    """Small-distance equirectangular distance in metres."""
    lat_mid = math.radians((float(a_lat) + float(b_lat)) / 2.0)
    dx = (float(b_lon) - float(a_lon)) * 111_320.0 * max(0.18, abs(math.cos(lat_mid)))
    dy = (float(b_lat) - float(a_lat)) * 111_320.0
    return math.sqrt(dx * dx + dy * dy)


def _cell_point(cell: dict) -> tuple[float, float, float, int] | None:
    try:
        n = max(1, int(cell.get('count', 1)))
        la = float(cell.get('lat_sum', 0.0)) / n
        lo = float(cell.get('lon_sum', 0.0)) / n
        rs = float(cell.get('rssi_sum', -100.0)) / n
        if not (math.isfinite(la) and math.isfinite(lo)):
            return None
        return la, lo, rs, n
    except Exception:
        return None


def _cluster_center_radius(cells: dict) -> tuple[float, float, float, int]:
    sw = lat = lon = 0.0
    raw = 0
    pts = []
    for c in (cells or {}).values():
        pt = _cell_point(c)
        if not pt:
            continue
        la, lo, rs, n = pt
        w = max(1, n)
        sw += w; lat += la * w; lon += lo * w; raw += n
        pts.append((la, lo))
    if sw <= 0 or not pts:
        return 0.0, 0.0, 0.0, 0
    lat /= sw; lon /= sw
    radius = max([_distance_m(lat, lon, la, lo) for la, lo in pts] + [0.0])
    return lat, lon, radius, raw


def _split_source_geo_clusters(sid: str, cells: dict, *, min_obs: int,
                               split_radius_m: float = _SOURCE_SPLIT_RADIUS_M) -> list[dict]:
    """Return geo-consistent components for one claimed source ID.

    A MAC/BSSID collision, parser bug, or bad held GPS point can otherwise merge
    unrelated places and pull the solved point into a lie.  Strong separated
    components become MAC#geoN.  Weak far components are quarantined as outliers
    and recorded in metadata, not averaged into the solve.
    """
    items = []
    for key, cell in (cells or {}).items():
        pt = _cell_point(cell)
        if not pt:
            continue
        la, lo, rs, n = pt
        items.append({'key': key, 'cell': cell, 'lat': la, 'lon': lo, 'rssi': rs, 'count': n})
    if not items:
        return [{'id': str(sid), 'source_id': str(sid), 'cells': {},
                 'meta': {'geo_clustered': False, 'geo_cluster_count': 0}}]
    if len(items) <= 1:
        return [{'id': str(sid), 'source_id': str(sid), 'cells': dict(cells or {}),
                 'meta': {'geo_clustered': False, 'geo_cluster_count': 1}}]

    # Connected components where each cell is close to at least one cell in the
    # component.  O(n^2) is fine here because cells are already capped per source.
    parent = list(range(len(items)))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if _distance_m(items[i]['lat'], items[i]['lon'], items[j]['lat'], items[j]['lon']) <= split_radius_m:
                union(i, j)

    comps: dict[int, list] = {}
    for i, it in enumerate(items):
        comps.setdefault(find(i), []).append(it)
    comp_list = []
    for comp_items in comps.values():
        comp_cells = {it['key']: it['cell'] for it in comp_items}
        clat, clon, radius, raw = _cluster_center_radius(comp_cells)
        best_rssi = max([it['rssi'] for it in comp_items] + [-999.0])
        comp_list.append({'items': comp_items, 'cells': comp_cells,
                          'cell_count': len(comp_cells), 'raw_count': raw,
                          'lat': clat, 'lon': clon, 'radius_m': radius,
                          'best_rssi': best_rssi})
    comp_list.sort(key=lambda c: (c['raw_count'], c['cell_count'], c['best_rssi']), reverse=True)

    if len(comp_list) <= 1:
        return [{'id': str(sid), 'source_id': str(sid), 'cells': comp_list[0]['cells'],
                 'meta': {'geo_clustered': False, 'geo_cluster_count': 1,
                          'geo_cluster_radius_m': round(comp_list[0]['radius_m'], 2)}}]

    def strong(c):
        return c['cell_count'] >= max(2, int(min_obs or _SOURCE_SPLIT_MIN_CELLS)) or c['raw_count'] >= max(_SOURCE_SPLIT_MIN_RAW, int(min_obs or 3))

    strong_comps = [c for c in comp_list if strong(c)]
    if not strong_comps:
        strong_comps = [comp_list[0]]
    weak = [c for c in comp_list if c not in strong_comps]

    total_raw = sum(c['raw_count'] for c in comp_list)
    max_sep = 0.0
    for i, a in enumerate(comp_list):
        for b in comp_list[i+1:]:
            max_sep = max(max_sep, _distance_m(a['lat'], a['lon'], b['lat'], b['lon']))

    split = len(strong_comps) > 1
    out = []
    for idx, c in enumerate(strong_comps, 1):
        position_id = f'{sid}#geo{idx}' if split else str(sid)
        meta = {
            'geo_clustered': True,
            'geo_cluster_index': idx,
            'geo_cluster_count': len(strong_comps),
            'geo_component_count': len(comp_list),
            'parent_source_id': str(sid),
            'geo_cluster_center_lat': round(c['lat'], 7),
            'geo_cluster_center_lon': round(c['lon'], 7),
            'geo_cluster_radius_m': round(c['radius_m'], 2),
            'geo_cluster_cells': int(c['cell_count']),
            'geo_cluster_raw_samples': int(c['raw_count']),
            'geo_split_radius_m': float(split_radius_m),
            'geo_max_component_separation_m': round(max_sep, 2),
            'geo_outlier_components': len(weak),
            'geo_outlier_cells': int(sum(w['cell_count'] for w in weak)),
            'geo_outlier_raw_samples': int(sum(w['raw_count'] for w in weak)),
        }
        if split:
            meta['split_reason'] = 'geo_inconsistent_same_source_id'
            meta['solve_warning'] = 'Same source ID formed multiple distant clusters; kept separate instead of merging by MAC only.'
        elif weak:
            meta['solve_warning'] = 'Tiny distant sample cluster quarantined; not used in canonical solve.'
        if total_raw:
            meta['geo_cluster_raw_fraction'] = round(c['raw_count'] / total_raw, 4)
        out.append({'id': position_id, 'source_id': str(sid), 'cells': c['cells'], 'meta': meta})
    return out

def _cell_geometry_span_m(cells: dict) -> float:
    pts = []
    for c in (cells or {}).values():
        try:
            n = max(1, int(c.get('count', 1)))
            la = float(c.get('lat_sum', 0.0)) / n
            lo = float(c.get('lon_sum', 0.0)) / n
            if math.isfinite(la) and math.isfinite(lo):
                pts.append((la, lo))
        except Exception:
            pass
    if len(pts) < 2:
        return 0.0
    lat_min = min(p[0] for p in pts); lat_max = max(p[0] for p in pts)
    lon_min = min(p[1] for p in pts); lon_max = max(p[1] for p in pts)
    lat_mid = (lat_min + lat_max) / 2.0
    return math.sqrt(((lat_max - lat_min) * 111_320.0) ** 2 +
                     ((lon_max - lon_min) * 111_320.0 * max(0.18, abs(math.cos(math.radians(lat_mid))))) ** 2)


def _rss_confidence_radius_m(pos_method: str, cells: dict, residual_dBm: float | None = None) -> float:
    """Small honest visual radius for map selection.

    This is not a formal covariance.  It is a bounded UI confidence/quality
    radius derived from geometry span, cell count and RSS residual so the user
    can visually compare a strong trilateration vs a broad centroid.
    """
    n = max(1, len(cells or {}))
    span = _cell_geometry_span_m(cells)
    try:
        res = float(residual_dBm) if residual_dBm is not None else 0.0
        if not math.isfinite(res):
            res = 0.0
    except Exception:
        res = 0.0
    if pos_method == 'rss_trilateration':
        r = 8.0 + 0.18 * span + 7.0 * max(0.0, res) + 45.0 / math.sqrt(n)
        return round(max(5.0, min(500.0, r)), 2)
    # Centroid is explicitly less precise; show it as a larger dashed circle.
    r = 25.0 + 0.38 * span + 10.0 * max(0.0, res) + 80.0 / math.sqrt(n)
    return round(max(12.0, min(1200.0, r)), 2)

def _solve_rss_position(sid: str, cells: dict, meta: dict, n_exp: float,
                        min_obs: int, rss_solve, rssi_centroid) -> dict | None:
    obs = _obs_from_cells(cells)
    if len(obs) < min_obs:
        return None
    raw_samples = sum(max(1, c.get('count', 1)) for c in cells.values())

    # Very small route span or nearly identical cells make RSS trilateration
    # ill-conditioned.  Do not spend Gauss-Newton iterations trying to invent a
    # precise point; keep a real rssi_centroid row instead.  This still appears
    # in the Positions tab, but honestly labels the method.
    try:
        lat_min = min(o[0] for o in obs); lat_max = max(o[0] for o in obs)
        lon_min = min(o[1] for o in obs); lon_max = max(o[1] for o in obs)
        lat_mid = (lat_min + lat_max) / 2.0
        span_m = math.sqrt(((lat_max - lat_min) * 111_320.0) ** 2 +
                           ((lon_max - lon_min) * 111_320.0 * max(0.18, abs(math.cos(math.radians(lat_mid))))) ** 2)
    except Exception:
        span_m = 0.0

    rss = None
    if span_m >= 2.0:
        try:
            # Bound web solves.  The underlying solver is iterative; 25 passes is
            # enough for the aggregated cell view and prevents one pathological
            # source from pinning the whole web worker for ages.
            rss = rss_solve(obs, n_exp=n_exp, max_iter=25)
        except Exception as exc:
            _broadcast_log(f'[solve-warning] RSS failed for source#{_anon_source_id(sid)} '
                           f'({len(obs)} cells/{raw_samples} obs): {type(exc).__name__}; using rssi_centroid',
                           'solve')
            rss = None
    if rss is not None:
        lat = _as_float(rss.get('lat')); lon = _as_float(rss.get('lon'))
        if lat is not None and lon is not None:
            rec = {**meta, 'id': sid, 't': time.time(),
                   'pos_method': 'rss_trilateration',
                   'sample_cells': len(obs), 'raw_samples': raw_samples, **rss}
            rec['geometry_span_m'] = round(span_m, 2)
            rec['confidence_radius_m'] = _rss_confidence_radius_m('rss_trilateration', cells, rec.get('residual_dBm'))
            return rec

    lat, lon = rssi_centroid(obs)
    lat = _as_float(lat); lon = _as_float(lon)
    if lat is None or lon is None:
        return None
    rec = {**meta, 'id': sid, 't': time.time(),
           'pos_method': 'rssi_centroid', 'lat': lat, 'lon': lon,
           'samples': raw_samples, 'sample_cells': len(obs),
           'raw_samples': raw_samples}
    rec['geometry_span_m'] = round(span_m, 2)
    rec['confidence_radius_m'] = _rss_confidence_radius_m('rssi_centroid', cells, None)
    return rec




def _source_observation_centroid(sid: str, cells: dict, meta: dict, reason: str = 'not_enough_geo_cells') -> dict | None:
    """Return a clearly-labelled non-solved marker for a geotagged source.

    This is used by batch Solve All so the map can preserve source coverage even
    when a source has too few distinct geo-cells for an honest RSS solve.  It is
    not a trilateration result; it is only the weighted centre of the evidence
    the session actually contains.
    """
    obs = _obs_from_cells(cells)
    if not obs:
        return None
    raw_samples = sum(max(1, c.get('count', 1)) for c in cells.values())
    sw = lat = lon = 0.0
    best = obs[0]
    for la, lo, rs in obs:
        try:
            rssi = float(rs)
        except (TypeError, ValueError):
            rssi = -100.0
        w = max(1.0, min(80.0, 110.0 + rssi))
        sw += w; lat += float(la) * w; lon += float(lo) * w
        if rssi > float(best[2] if best[2] is not None else -999):
            best = (la, lo, rssi)
    if sw > 0:
        lat /= sw; lon /= sw
    else:
        lat, lon = float(best[0]), float(best[1])
    return {**meta, 'id': sid, 't': time.time(),
            'pos_method': 'observation_centroid',
            'unsolved': True, 'solve_note': reason,
            'lat': lat, 'lon': lon,
            'sample_cells': len(obs), 'raw_samples': raw_samples,
            'samples': raw_samples}

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
            'lat': _as_float(rec.get('lat')), 'lon': _as_float(rec.get('lon')), 'alt': _as_float(rec.get('alt')),
            't': _as_float(rec.get('t'), 0), 'fix': rec.get('fix'),
            'accuracy_h': _as_float(rec.get('accuracy_h')), 'num_sats': rec.get('num_sats'),
        }
    meta = _clean_relation_meta(source_meta_from_record(rec))
    row = {**meta, 'record_type': 'observation', 'lat': _as_float(rec.get('lat')), 'lon': _as_float(rec.get('lon')),
           't': _as_float(rec.get('t'), 0), 'rssi': _as_float(rec.get('rssi')), 'id': record_source_id(rec),
           'freq': _as_float(rec.get('freq')), 'protocol': meta.get('protocol', rec.get('protocol', ''))}
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


def _sample_indexed_rows(rows: list[tuple[int, dict]], max_points: int) -> list[tuple[int, dict]]:
    """Return a deterministic file-order sample including first and last rows."""
    total = len(rows)
    if total <= max_points:
        return list(rows)
    step = max(1, math.ceil(total / max(1, max_points)))
    return [pair for i, pair in enumerate(rows, 1)
            if i == 1 or i == total or ((i - 1) % step == 0)]


def _overview_session_rows(path: Path, max_points: int = _MAP_BULK_PREVIEW_POINTS) -> list[dict]:
    """Return a small but geographically honest preview for bulk map loading.

    Bulk mode must stay cheap enough for small Pis, so it still samples heavily.
    However, the old implementation switched to GPS-only as soon as *any* GPS
    breadcrumb existed.  That hid valid geotagged RF observations from areas
    where the capture had sparse/missing GPS breadcrumbs, making Solve All look
    like it lost whole parts of a session.  Keep a sampled observation coverage
    layer as well, while preserving file order.
    """
    gps_rows: list[tuple[int, dict]] = []
    obs_rows: list[tuple[int, dict]] = []
    seq = 0
    for row in _iter_session_rows(path, raw_all=False):
        seq += 1
        if row.get('lat') is None or row.get('lon') is None:
            continue
        if row.get('record_type') == 'gps' or row.get('source') == 'gps':
            gps_rows.append((seq, row))
        else:
            obs_rows.append((seq, row))

    if gps_rows and obs_rows:
        gps_budget = min(len(gps_rows), max(1, int(max_points * 0.55)))
        obs_budget = max(1, max_points - gps_budget)
        # If one side does not need its whole budget, let the other side use it.
        spare = max_points - gps_budget - obs_budget
        if len(gps_rows) < gps_budget:
            obs_budget += gps_budget - len(gps_rows)
        if len(obs_rows) < obs_budget:
            gps_budget += obs_budget - len(obs_rows)
        obs_budget += max(0, spare)
        pairs = (_sample_indexed_rows(gps_rows, gps_budget) +
                 _sample_indexed_rows(obs_rows, obs_budget))
        pairs.sort(key=lambda p: p[0])
        total = len(gps_rows) + len(obs_rows)
    else:
        base = gps_rows if gps_rows else obs_rows
        pairs = _sample_indexed_rows(base, max_points)
        total = len(base)

    rows = [row for _, row in pairs]
    for row in rows:
        is_gps = row.get('record_type') == 'gps' or row.get('source') == 'gps'
        row['record_type'] = 'gps' if is_gps else 'route'
        row['source'] = 'gps' if is_gps else row.get('source', 'route')
        row['overview'] = True
        row['sampled_total'] = total
    return rows



def _row_ids(row: dict) -> set[str]:
    ids: set[str] = set()
    meta = row.get('metadata') if isinstance(row.get('metadata'), dict) else {}
    for k in ('id', 'source_id', 'identifier', 'source', 'bssid', 'client', 'station', 'mac'):
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


def _normalize_session_selection(session_paths: list | tuple | None) -> set[str]:
    selected: set[str] = set()
    root = AW_SESSIONS.resolve()
    for raw in session_paths or []:
        try:
            p = Path(str(raw)).resolve()
            if p.suffix == '.jsonl' and str(p).startswith(str(root)) and p.exists():
                selected.add(str(p))
        except Exception:
            continue
    return selected


def _batch_solve_sessions(max_sessions: int = 500, session_paths: list | tuple | None = None) -> list[dict]:
    selected = _normalize_session_selection(session_paths)
    sessions = [s for s in _list_sessions() if s['stype'] in ('wardriver', 'tdoa_raw', 'unknown')]
    if selected:
        sessions = [s for s in sessions if str(Path(s['path']).resolve()) in selected]
    return sessions[:max_sessions]



def _solver_db_rel_or_path(value: str) -> Path | None:
    """Validate a solver DB path/name under ~/.aetherward/solver."""
    if not value:
        return None
    p = Path(value)
    if not p.is_absolute():
        p = AW_SOLVER / value
    try:
        rp = p.resolve()
        root = AW_SOLVER.resolve()
    except Exception:
        return None
    if not str(rp).startswith(str(root)):
        return None
    if rp.suffix.lower() not in ('.sqlite', '.db', '.awdb'):
        return None
    return rp


def _solver_db_name(label: str = 'solve') -> str:
    safe = _re.sub(r'[^A-Za-z0-9_.-]+', '-', str(label or 'solve')).strip('-._')[:64] or 'solve'
    ts = time.strftime('%Y%m%d-%H%M%S')
    return f'{safe}-{ts}.sqlite'


def _init_solver_db(con: sqlite3.Connection) -> None:
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA synchronous=NORMAL')
    con.execute("""CREATE TABLE IF NOT EXISTS meta(
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS positions(
        id TEXT PRIMARY KEY,
        source_id TEXT,
        session_path TEXT,
        session_name TEXT,
        pos_method TEXT,
        lat REAL,
        lon REAL,
        sample_cells INTEGER,
        raw_samples INTEGER,
        json TEXT NOT NULL
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS sessions(
        session_path TEXT PRIMARY KEY,
        session_name TEXT,
        size INTEGER,
        mtime_ns INTEGER,
        hash TEXT,
        imported_at TEXT,
        record_count INTEGER,
        source_count INTEGER,
        json TEXT NOT NULL DEFAULT '{}'
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS source_samples(
        position_id TEXT,
        source_id TEXT,
        session_path TEXT,
        seq INTEGER,
        lat REAL,
        lon REAL,
        rssi REAL,
        count INTEGER,
        json TEXT NOT NULL,
        PRIMARY KEY(position_id, session_path, seq)
    )""")
    # Older bulk DBs used PRIMARY KEY(position_id, seq).  That silently
    # overwrote per-session evidence when the same AP appeared in several
    # sessions, which made a real incremental append impossible.  Upgrade the
    # table in-place while preserving whatever evidence exists.
    try:
        info = con.execute('PRAGMA table_info(source_samples)').fetchall()
        pk_cols = [r[1] for r in sorted([r for r in info if r[5]], key=lambda r: r[5])]
        if pk_cols != ['position_id', 'session_path', 'seq']:
            con.execute('ALTER TABLE source_samples RENAME TO source_samples_old')
            con.execute("""CREATE TABLE source_samples(
                position_id TEXT,
                source_id TEXT,
                session_path TEXT,
                seq INTEGER,
                lat REAL,
                lon REAL,
                rssi REAL,
                count INTEGER,
                json TEXT NOT NULL,
                PRIMARY KEY(position_id, session_path, seq)
            )""")
            con.execute("""INSERT OR IGNORE INTO source_samples
                (position_id,source_id,session_path,seq,lat,lon,rssi,count,json)
                SELECT position_id,source_id,COALESCE(session_path,''),seq,lat,lon,rssi,count,json
                FROM source_samples_old""")
            con.execute('DROP TABLE source_samples_old')
    except Exception:
        # Do not make a legacy/partial DB unloadable; later writes will still
        # fail loudly through the caller if the table is genuinely corrupt.
        pass
    con.execute("""CREATE INDEX IF NOT EXISTS idx_solver_samples_position
        ON source_samples(position_id)""")
    con.execute("""CREATE INDEX IF NOT EXISTS idx_solver_samples_source_session
        ON source_samples(source_id, session_path)""")
    con.execute("""CREATE TABLE IF NOT EXISTS path_previews(
        session_path TEXT PRIMARY KEY,
        session_name TEXT,
        overview INTEGER,
        point_count INTEGER,
        total_points INTEGER,
        json TEXT NOT NULL
    )""")
    con.commit()


def _create_solver_db(label: str, *, mode: str, settings: dict | None = None) -> str:
    AW_SOLVER.mkdir(parents=True, exist_ok=True)
    p = AW_SOLVER / _solver_db_name(label)
    con = sqlite3.connect(p)
    try:
        _init_solver_db(con)
        meta = {
            'format': 'aetherward-solvedb-v1',
            'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'mode': mode,
            'settings': _json_dumps(settings or {}),
        }
        for k, v in meta.items():
            con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', (k, str(v)))
        con.commit()
    finally:
        con.close()
    return str(p)


def _db_counts(con: sqlite3.Connection) -> dict:
    def one(sql):
        return int(con.execute(sql).fetchone()[0])
    return {
        'positions': one('SELECT COUNT(*) FROM positions'),
        'samples': one('SELECT COUNT(*) FROM source_samples'),
        'paths': one('SELECT COUNT(*) FROM path_previews'),
        'sessions': one('SELECT COUNT(*) FROM sessions'),
    }


def _session_file_fingerprint(path: Path) -> dict:
    """Cheap but stable enough manifest fingerprint for incremental updates."""
    st = path.stat()
    h = hashlib.sha1()
    h.update(str(path).encode('utf-8', 'ignore'))
    h.update(str(st.st_size).encode())
    h.update(str(getattr(st, 'st_mtime_ns', int(st.st_mtime * 1e9))).encode())
    try:
        with path.open('rb') as fh:
            h.update(fh.read(65536))
            if st.st_size > 65536:
                fh.seek(max(0, st.st_size - 65536))
                h.update(fh.read(65536))
    except Exception:
        pass
    return {
        'session_path': str(path),
        'size': int(st.st_size),
        'mtime_ns': int(getattr(st, 'st_mtime_ns', int(st.st_mtime * 1e9))),
        'hash': h.hexdigest(),
    }


def _session_db_status(con: sqlite3.Connection, session_path: str) -> tuple[str, dict]:
    """Return ('new'|'changed'|'unchanged'|'missing', fingerprint)."""
    p = Path(session_path)
    if not p.exists():
        return 'missing', {'session_path': str(p)}
    fp = _session_file_fingerprint(p)
    row = con.execute('SELECT size,mtime_ns,hash FROM sessions WHERE session_path=?', (str(p),)).fetchone()
    if not row:
        return 'new', fp
    old_size, old_mtime_ns, old_hash = row
    if int(old_size or -1) == fp['size'] and int(old_mtime_ns or -1) == fp['mtime_ns'] and str(old_hash or '') == fp['hash']:
        return 'unchanged', fp
    return 'changed', fp


def _store_solver_session_manifest(con: sqlite3.Connection, sess: dict, *, record_count: int, source_count: int) -> None:
    p = Path(sess['path'])
    fp = _session_file_fingerprint(p)
    name = (sess.get('folder') + '/' if sess.get('folder') else '') + (sess.get('name') or p.name)
    payload = {**fp, 'record_count': int(record_count or 0), 'source_count': int(source_count or 0)}
    con.execute("""INSERT OR REPLACE INTO sessions
        (session_path,session_name,size,mtime_ns,hash,imported_at,record_count,source_count,json)
        VALUES(?,?,?,?,?,?,?,?,?)""", (
        str(p), name, fp['size'], fp['mtime_ns'], fp['hash'],
        time.strftime('%Y-%m-%d %H:%M:%S'), int(record_count or 0), int(source_count or 0),
        _json_dumps(payload)))


def _index_session_for_solver(sess: dict, *, max_cells: int,
                              progress_cb=None) -> tuple[dict, dict, int, int]:
    """Index one JSONL session into per-source bounded RSS geo-cells."""
    from collections import defaultdict
    from aetherward.session import is_gps_record, record_source_id, source_meta_from_record

    rss_cells: dict = defaultdict(dict)
    rss_meta: dict = {}
    rec_n = geo_n = 0
    sess_size = max(1, int(sess.get('size') or Path(sess['path']).stat().st_size))
    last_prog = 0.0
    with open(sess['path'], encoding='utf-8', errors='replace') as fh:
        while True:
            if _solve_stop.is_set():
                break
            raw = fh.readline()
            if not raw:
                break
            pos_now = fh.tell()
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
            if progress_cb:
                now = time.time()
                if now - last_prog > 0.8:
                    progress_cb(rec_n, len(rss_cells), pos_now / sess_size)
                    last_prog = now
                    time.sleep(0)
    return rss_cells, rss_meta, rec_n, geo_n


def _store_solver_samples(con: sqlite3.Connection, position_id: str, source_id: str,
                          session_path: str, cells: dict) -> None:
    con.execute('DELETE FROM source_samples WHERE position_id=? AND session_path=?',
                (position_id, session_path))
    rows = _cells_to_sample_rows(position_id, source_id, session_path, cells)
    if rows:
        con.executemany("""INSERT OR REPLACE INTO source_samples
            (position_id,source_id,session_path,seq,lat,lon,rssi,count,json)
            VALUES(?,?,?,?,?,?,?,?,?)""", rows)


def _delete_session_contribution(con: sqlite3.Connection, session_path: str) -> set[str]:
    rows = con.execute('SELECT DISTINCT source_id FROM source_samples WHERE session_path=?',
                       (session_path,)).fetchall()
    affected = {str(r[0]) for r in rows if r and r[0]}
    con.execute('DELETE FROM source_samples WHERE session_path=?', (session_path,))
    con.execute('DELETE FROM path_previews WHERE session_path=?', (session_path,))
    con.execute('DELETE FROM sessions WHERE session_path=?', (session_path,))
    return affected


def _cells_from_solver_samples(con: sqlite3.Connection, source_id: str,
                               max_cells: int = _MAX_SOLVE_CELLS_PER_SOURCE) -> dict:
    from collections import defaultdict
    by_sid: dict = defaultdict(dict)
    rows = con.execute("""SELECT lat,lon,rssi,count FROM source_samples
                          WHERE source_id=? ORDER BY session_path, seq""", (source_id,)).fetchall()
    for lat, lon, rssi, count in rows:
        n = max(1, int(count or 1))
        cell = {
            'lat_sum': float(lat or 0.0) * n,
            'lon_sum': float(lon or 0.0) * n,
            'rssi_sum': float(rssi if rssi is not None else -100.0) * n,
            'count': n,
        }
        _add_geo_cell_aggregate(by_sid, source_id, cell, max_cells=max_cells)
    return by_sid.get(source_id, {})


def _source_sessions_seen(con: sqlite3.Connection, source_id: str) -> int:
    row = con.execute('SELECT COUNT(DISTINCT session_path) FROM source_samples WHERE source_id=?',
                      (source_id,)).fetchone()
    return int(row[0] or 0) if row else 0


def _position_json_meta(con: sqlite3.Connection, source_id: str) -> dict:
    row = con.execute('SELECT json FROM positions WHERE id=? OR source_id=? LIMIT 1',
                      (source_id, source_id)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row[0])
    except Exception:
        return {}


def _canonical_position_record(sid: str, cells: dict, meta: dict, *, n_exp: float,
                               min_obs: int, include_unsolved: bool,
                               rss_solve, rssi_centroid,
                               position_id: str | None = None,
                               source_id: str | None = None) -> dict | None:
    pid = str(position_id or sid)
    real_sid = str(source_id or sid)
    pos_rec = _solve_rss_position(pid, cells, meta, n_exp, min_obs, rss_solve, rssi_centroid)
    if pos_rec is None and include_unsolved:
        pos_rec = _source_observation_centroid(
            pid, cells, meta, reason=f'needs ≥{min_obs} distinct geo-cells for RSS solve')
    if pos_rec is None:
        return None
    pos_rec['id'] = pid
    pos_rec['source_id'] = real_sid
    pos_rec['identifier'] = real_sid
    if pid != real_sid:
        pos_rec['canonical_id'] = pid
        pos_rec.setdefault('parent_source_id', real_sid)
    return pos_rec


def _canonical_position_records_for_source(sid: str, cells: dict, meta: dict, *, n_exp: float,
                                           min_obs: int, include_unsolved: bool,
                                           rss_solve, rssi_centroid) -> list[dict]:
    records = []
    stale_cluster_keys = {'split_reason', 'solve_warning', 'canonical_id', 'parent_source_id'}
    base_meta = {k: v for k, v in dict(meta or {}).items()
                 if not str(k).startswith('geo_') and k not in stale_cluster_keys}
    clusters = _split_source_geo_clusters(sid, cells, min_obs=min_obs)
    for cl in clusters:
        cl_meta = _merge_source_meta(dict(base_meta), {})
        cl_meta.update(dict(cl.get('meta') or {}))
        pos_rec = _canonical_position_record(
            sid, cl.get('cells') or {}, cl_meta, n_exp=n_exp, min_obs=min_obs,
            include_unsolved=include_unsolved, rss_solve=rss_solve, rssi_centroid=rssi_centroid,
            position_id=cl.get('id') or sid, source_id=cl.get('source_id') or sid)
        if pos_rec is not None:
            records.append(pos_rec)
    return records


def _list_solver_dbs() -> list[dict]:
    if not AW_SOLVER.exists():
        return []
    out = []
    for p in sorted([*AW_SOLVER.glob('*.sqlite'), *AW_SOLVER.glob('*.db'), *AW_SOLVER.glob('*.awdb')], key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            st = p.stat()
            rec = {'name': p.name, 'path': str(p), 'size': st.st_size,
                   'mtime': time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime)),
                   'positions': 0, 'samples': 0, 'paths': 0, 'mode': '?'}
            con = sqlite3.connect(p)
            try:
                _init_solver_db(con)
                rec.update(_db_counts(con))
                row = con.execute("SELECT value FROM meta WHERE key='mode'").fetchone()
                if row:
                    rec['mode'] = row[0]
            finally:
                con.close()
            out.append(rec)
        except Exception:
            continue
    return out


def _cells_to_sample_rows(position_id: str, source_id: str, session_path: str, cells: dict) -> list[tuple]:
    rows = []
    for seq, (k, c) in enumerate(sorted((cells or {}).items(), key=lambda kv: str(kv[0])), 1):
        n = max(1, int(c.get('count', 1)))
        lat = c.get('lat_sum', 0.0) / n
        lon = c.get('lon_sum', 0.0) / n
        rssi = c.get('rssi_sum', 0.0) / n
        row = {
            'record_type': 'source_sample_cell',
            'source_sample': True,
            'from_solver_db': True,
            'position_id': position_id,
            'source_id': source_id,
            'id': source_id,
            'path': session_path,
            'session_path': session_path,
            'lat': lat,
            'lon': lon,
            'rssi': rssi,
            'count': n,
            'sampled_total': len(cells or {}),
        }
        rows.append((position_id, source_id, session_path, seq, lat, lon, rssi, n, _json_dumps(row)))
    return rows


def _store_solver_position(con: sqlite3.Connection, rec: dict, cells: dict | None = None) -> None:
    if not rec or rec.get('lat') is None or rec.get('lon') is None:
        return
    position_id = str(rec.get('id') or '')
    if not position_id:
        return
    source_id = str(rec.get('source_id') or rec.get('identifier') or rec.get('id') or '')
    session_path = str(rec.get('session_path') or '')
    session_name = str(rec.get('session_name') or (Path(session_path).name if session_path else ''))
    rec2 = dict(rec)
    rec2['from_solver_db_capable'] = True
    con.execute("""INSERT OR REPLACE INTO positions
        (id,source_id,session_path,session_name,pos_method,lat,lon,sample_cells,raw_samples,json)
        VALUES(?,?,?,?,?,?,?,?,?,?)""", (
        position_id, source_id, session_path, session_name, str(rec.get('pos_method') or ''),
        _as_float(rec.get('lat')), _as_float(rec.get('lon')),
        int(rec.get('sample_cells') or 0), int(rec.get('raw_samples') or rec.get('samples') or 0),
        _json_dumps(rec2)))
    if cells is not None:
        _store_solver_samples(con, position_id, source_id, session_path, cells)


def _store_solver_path_preview(con: sqlite3.Connection, session_path: str, session_name: str | None = None,
                               *, overview: bool = True, max_points: int = _MAP_BULK_PREVIEW_POINTS) -> None:
    p = Path(session_path)
    if not p.exists():
        return
    try:
        rows = _overview_session_rows(p, max_points=max_points) if overview else _decimated_session_rows(p, _MAP_MAX_GPS, _MAP_MAX_OBS)
    except Exception as exc:
        _broadcast_log(f'[solver-db] path preview not stored for {p.name}: {type(exc).__name__}: {exc}', 'solve')
        return
    total = max([int(r.get('sampled_total') or 0) for r in rows] + [len(rows)])
    con.execute("""INSERT OR REPLACE INTO path_previews
        (session_path,session_name,overview,point_count,total_points,json)
        VALUES(?,?,?,?,?,?)""", (str(p), session_name or p.name, 1 if overview else 0, len(rows), total, _json_dumps(rows)))


def _load_solver_positions(db_path: Path, *, append: bool = False) -> dict:
    con = sqlite3.connect(db_path)
    loaded = 0
    try:
        _init_solver_db(con)
        rows = con.execute('SELECT json FROM positions ORDER BY session_name, id').fetchall()
        if not append:
            _clear_auto_positions('solver-db-load')
        for (js,) in rows:
            try:
                rec = json.loads(js)
            except Exception:
                continue
            rec['solver_db_path'] = str(db_path)
            rec['from_solver_db'] = True
            _broadcast(rec)
            loaded += 1
        counts = _db_counts(con)
    finally:
        con.close()
    _broadcast_log(f'[solver-db] loaded {loaded} position(s) from {db_path.name}', 'solve')
    return {'ok': True, 'loaded': loaded, 'append': append, 'path': str(db_path), **counts}




def _solver_db_update_status(db_path: Path, *, session_paths: list | tuple | None = None,
                             max_sessions: int = 500) -> dict:
    """Summarise whether a DB already contains the selected/current sessions."""
    sessions = _batch_solve_sessions(max_sessions, session_paths=session_paths)
    con = sqlite3.connect(db_path)
    try:
        _init_solver_db(con)
        counts = _db_counts(con)
        rows = []
        pending = 0
        unchanged = changed = new = missing = 0
        for sess in sessions:
            status, _fp = _session_db_status(con, sess['path'])
            if status == 'unchanged': unchanged += 1
            elif status == 'changed': changed += 1; pending += 1
            elif status == 'new': new += 1; pending += 1
            elif status == 'missing': missing += 1
            rows.append({'path': sess['path'], 'name': (sess.get('folder') + '/' if sess.get('folder') else '') + sess.get('name', ''),
                         'status': status, 'records': sess.get('records'), 'stype': sess.get('stype')})
        return {**counts, 'path': str(db_path), 'selected_sessions': len(sessions),
                'pending_sessions': pending, 'unchanged_sessions': unchanged,
                'changed_sessions': changed, 'new_sessions': new,
                'missing_sessions': missing, 'up_to_date': pending == 0,
                'sessions': rows[:250]}
    finally:
        con.close()

def _solver_db_paths(db_path: Path) -> list[dict]:
    con = sqlite3.connect(db_path)
    try:
        _init_solver_db(con)
        rows = con.execute("""SELECT session_path,session_name,overview,point_count,total_points
                              FROM path_previews ORDER BY session_name""").fetchall()
        return [{'db': str(db_path), 'session_path': r[0], 'session_name': r[1],
                 'overview': bool(r[2]), 'points': int(r[3] or 0), 'total': int(r[4] or 0)} for r in rows]
    finally:
        con.close()


def _solver_db_path_records(db_path: Path, session_path: str) -> list[dict]:
    con = sqlite3.connect(db_path)
    try:
        _init_solver_db(con)
        row = con.execute('SELECT json FROM path_previews WHERE session_path=?', (session_path,)).fetchone()
        if not row:
            return []
        return json.loads(row[0])
    finally:
        con.close()


def _solver_db_source_samples(db_path: Path, position_id: str = '', source_id: str = '', session_path: str = '', max_obs: int = 1500) -> list[dict]:
    con = sqlite3.connect(db_path)
    try:
        _init_solver_db(con)
        pos_meta = {}
        if position_id:
            row = con.execute('SELECT source_id,json FROM positions WHERE id=? LIMIT 1', (position_id,)).fetchone()
            if row:
                if not source_id:
                    source_id = str(row[0] or '')
                try:
                    pos_meta = json.loads(row[1] or '{}')
                except Exception:
                    pos_meta = {}

        # Geo-split positions are stored as MAC#geoN but the durable evidence is
        # keyed by parent source_id/session.  Filter by the selected cluster radius
        # so clicking geo1 does not draw geo2/outlier links.
        cluster_filter = False
        clat = _as_float(pos_meta.get('geo_cluster_center_lat'))
        clon = _as_float(pos_meta.get('geo_cluster_center_lon'))
        crad = _as_float(pos_meta.get('geo_cluster_radius_m'), 0.0)
        if pos_meta.get('geo_clustered') and source_id and clat is not None and clon is not None:
            cluster_filter = True
            crad = max(float(crad or 0.0) + _SOLVE_CELL_M * 2.5, _SOLVE_CELL_M * 4.0)

        args = []
        where = []
        if cluster_filter:
            where.append('source_id=?'); args.append(source_id)
        elif position_id:
            where.append('position_id=?'); args.append(position_id)
        if source_id and not cluster_filter:
            where.append('source_id=?'); args.append(source_id)
        if session_path:
            where.append('session_path=?'); args.append(session_path)
        if not where:
            return []
        sql = 'SELECT json FROM source_samples WHERE ' + ' AND '.join(where) + ' ORDER BY session_path, seq LIMIT ?'
        args.append(max_obs * 4 if cluster_filter else max_obs)
        records = []
        for (js,) in con.execute(sql, args).fetchall():
            try:
                rec = json.loads(js)
            except Exception:
                continue
            if cluster_filter:
                la = _as_float(rec.get('lat')); lo = _as_float(rec.get('lon'))
                if la is None or lo is None or _distance_m(clat, clon, la, lo) > crad:
                    continue
                rec['geo_cluster_selected'] = position_id
            records.append(rec)
            if len(records) >= max_obs:
                break
        return records
    finally:
        con.close()


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
    db_con = None
    db_path = None
    if not follow:
        try:
            db_path = _create_solver_db('solve-' + Path(session_path).stem, mode='single', settings={'session': session_path, 'n_exp': n_exp, 'min_obs': min_obs})
            db_con = sqlite3.connect(db_path)
            _init_solver_db(db_con)
            _broadcast_log(f'[solver-db] writing solved DB: {Path(db_path).name}', 'solve')
        except Exception as exc:
            _broadcast_log(f'[solver-db] disabled: {type(exc).__name__}: {exc}', 'solve')
            db_con = None
    file_pos = 0
    _total_recs = 0
    _geo_recs   = 0
    _pass       = 0
    _idle_passes = 0

    _broadcast_progress(running=True, phase='start', pct=0.0, text=f'Starting {Path(session_path).name}')
    while not _solve_stop.is_set():
        _pass += 1
        new_recs = 0
        last_prog = 0.0
        try:
            file_size = max(1, Path(session_path).stat().st_size)
            with open(session_path, encoding='utf-8', errors='replace') as fh:
                fh.seek(file_pos)
                while not _solve_stop.is_set():
                    raw = fh.readline()
                    if not raw:
                        file_pos = fh.tell()
                        break
                    file_pos = fh.tell()
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        continue
                    new_recs += 1
                    if is_gps_record(rec):
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
                    now = time.time()
                    if now - last_prog > 0.65:
                        pct = min(95.0, (file_pos / file_size) * 70.0)
                        _broadcast_progress(running=True, phase='indexing', pct=pct,
                                            text=f'Indexing {Path(session_path).name}: {new_recs} new records, {len(rss_cells)} sources')
                        last_prog = now
                        time.sleep(0)
        except FileNotFoundError:
            _broadcast_progress(running=False, phase='error', pct=0.0, text='session file not found')
            break

        solve_now = list(dirty_sids)
        dirty_sids.clear()
        total_to_solve = max(1, len(solve_now))
        _broadcast_progress(running=True, phase='solving', pct=70.0,
                            text=f'Solving {len(solve_now)} changed source(s)')
        solved_in_pass = 0
        skipped_in_pass = 0
        errors_in_pass = 0
        last_solve_prog = 0.0
        for solve_i, sid in enumerate(solve_now, 1):
            if _solve_stop.is_set():
                break
            now = time.time()
            if solve_i == 1 or solve_i == total_to_solve or solve_i % 25 == 0 or now - last_solve_prog > 0.75:
                _broadcast_progress(
                    running=True, phase='solving',
                    pct=70.0 + 28.0 * solve_i / total_to_solve,
                    text=(f'Solving sources {solve_i}/{total_to_solve} '
                          f'(done {solved_in_pass}, skipped {skipped_in_pass}, current #{_anon_source_id(sid)})'))
                last_solve_prog = now
                time.sleep(0)
            t0 = time.time()
            try:
                pos_rec = _solve_rss_position(
                    sid, rss_cells.get(sid, {}), rss_meta.get(sid, {}),
                    n_exp, min_obs, rss_solve, rssi_centroid)
            except Exception as exc:
                errors_in_pass += 1
                _broadcast_log(f'[solve-error] source#{_anon_source_id(sid)} crashed: {type(exc).__name__}: {exc}', 'solve')
                continue
            dt = time.time() - t0
            if dt > 1.5:
                _broadcast_log(f'[solve-slow] source#{_anon_source_id(sid)} took {dt:.1f}s '
                               f'({len(rss_cells.get(sid, {}))} cells)', 'solve')
            if pos_rec is None:
                skipped_in_pass += 1
                continue
            prev = solved.get(sid)
            if prev is None or _changed(prev, pos_rec):
                # Broadcast the same record identity that is persisted in the solved DB,
                # so map clicks can load sample cells from ~/.aetherward/solver instead
                # of falling back to a slow JSONL scan.
                pos_rec['source_id'] = str(sid)
                pos_rec['identifier'] = str(sid)
                pos_rec['session_path'] = session_path
                pos_rec['session_name'] = Path(session_path).name
                if db_path:
                    pos_rec['solver_db_path'] = str(db_path)
                    pos_rec['from_solver_db_capable'] = True
                solved[sid] = dict(pos_rec); _broadcast(pos_rec)
                if db_con is not None:
                    try:
                        _store_solver_position(db_con, pos_rec, rss_cells.get(sid, {}))
                        if solved_in_pass % 25 == 0: db_con.commit()
                    except Exception as exc:
                        _broadcast_log(f'[solver-db] store failed for source#{_anon_source_id(sid)}: {type(exc).__name__}: {exc}', 'solve')
                solved_in_pass += 1
                label = rss_meta.get(sid, {}).get('ssid') or f'source#{_anon_source_id(sid)}'
                if solved_in_pass <= 20 or solved_in_pass % 100 == 0:
                    _broadcast_log(
                        f'[solved] {label} → {pos_rec["lat"]:.5f},{pos_rec["lon"]:.5f}'
                        f'  ({pos_rec["pos_method"]}, {pos_rec.get("raw_samples", pos_rec.get("samples", "?"))} obs, '
                        f'{pos_rec.get("sample_cells", "?")} cells)',
                        'solve')

        _broadcast_log(f'[solve-pass] {solved_in_pass} solved/updated, {skipped_in_pass} skipped, '
                       f'{errors_in_pass} errors from {len(solve_now)} source(s)', 'solve')
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
            if db_con is not None:
                try:
                    _store_solver_path_preview(db_con, session_path, Path(session_path).name, overview=False)
                    db_con.commit()
                    cnt = _db_counts(db_con)
                    _broadcast_log(f'[solver-db] saved {cnt["positions"]} positions, {cnt["samples"]} sample-cells, {cnt["paths"]} path(s) → {Path(db_path).name}', 'solve')
                except Exception as exc:
                    _broadcast_log(f'[solver-db] final save failed: {type(exc).__name__}: {exc}', 'solve')
                finally:
                    try: db_con.close()
                    except Exception: pass
                    db_con = None
            _broadcast_log(
                f'Solver done — {len(solved)} source(s) positioned from {_total_recs} record(s).',
                'solve')
            _broadcast_progress(running=False, phase='done', pct=100.0,
                                text=f'Done: {len(solved)} positioned from {_total_recs} records')
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
                      min_obs: int = 3,
                      include_unsolved: bool = True,
                      session_paths: list | tuple | None = None) -> None:
    """Pi-friendly global bulk solver for many saved sessions.

    Bulk now deduplicates by real source id/MAC across all indexed sessions.  A
    source seen in five session files becomes one canonical position row, with
    per-session evidence retained in source_samples for popup/sample lookup and
    incremental DB updates.
    """
    from collections import defaultdict
    from aetherward.position.rss import rss_solve, rssi_centroid

    sessions = _batch_solve_sessions(max_sessions, session_paths=session_paths)
    global_cells: dict = defaultdict(dict)
    global_meta: dict = {}
    source_sessions: dict[str, set[str]] = defaultdict(set)
    total_records = 0
    total_geo = 0
    total_solved = 0
    total_indexed = 0
    db_con = None
    db_path = None
    try:
        db_path = _create_solver_db('bulk', mode='bulk', settings={'max_cells': max_cells, 'max_sessions': max_sessions, 'n_exp': n_exp, 'min_obs': min_obs, 'include_unsolved': include_unsolved, 'merge_by_source': True, 'geo_guard': True, 'selected_sessions': len(sessions)})
        db_con = sqlite3.connect(db_path)
        _init_solver_db(db_con)
        _broadcast_log(f'[solver-db] writing global bulk solved DB: {Path(db_path).name}', 'solve')
    except Exception as exc:
        _broadcast_log(f'[solver-db] disabled: {type(exc).__name__}: {exc}', 'solve')
        db_con = None

    _broadcast_progress(running=True, phase='batch-start', pct=0.0,
                        text=f'Starting global bulk solve: {len(sessions)} sessions')
    _broadcast_log(
        f'[batch] starting {len(sessions)} selected session(s), merge_by_source=true+geo_guard, '
        f'≤{max_cells} geo-cells/source, min_obs={min_obs}, n_exp={n_exp:g}, '
        f'include_unsolved={include_unsolved}; route previews are stored once/session',
        'solve')
    try:
        # Pass 1: index every session and persist per-session evidence.
        for sess_i, sess in enumerate(sessions, 1):
            if _solve_stop.is_set():
                _broadcast_log('[batch] stopped by user', 'solve')
                break
            try:
                def _prog(rec_n, src_n, frac, sess_i=sess_i, sess=sess):
                    base = (sess_i - 1) / max(1, len(sessions)) * 70.0
                    span = 70.0 / max(1, len(sessions))
                    _broadcast_progress(running=True, phase='batch-indexing',
                                        pct=min(69.0, base + span * max(0.0, min(1.0, frac))),
                                        text=f'Bulk {sess_i}/{len(sessions)} indexing {sess["name"]}: {rec_n} records, {src_n} sources')

                rss_cells, rss_meta, rec_n, geo_n = _index_session_for_solver(
                    sess, max_cells=max_cells, progress_cb=_prog)
            except Exception as exc:
                _broadcast_log(f'[batch] skip {sess["name"]}: {type(exc).__name__}: {exc}', 'solve')
                continue

            for sid, cells in rss_cells.items():
                real_sid = str(sid)
                _merge_source_cells(global_cells, real_sid, cells, max_cells=max_cells)
                global_meta[real_sid] = _merge_source_meta(global_meta.get(real_sid, {}), rss_meta.get(sid, {}))
                source_sessions[real_sid].add(sess['path'])
                if db_con is not None:
                    try:
                        _store_solver_samples(db_con, real_sid, real_sid, sess['path'], cells)
                    except Exception as exc:
                        _broadcast_log(f'[solver-db] sample store failed for source#{_anon_source_id(real_sid)} in {sess["name"]}: {type(exc).__name__}: {exc}', 'solve')
            total_records += rec_n
            total_geo += geo_n
            _broadcast_log(
                f'[batch] {sess_i}/{len(sessions)} {sess["name"]}: indexed {len(rss_cells)} source(s), '
                f'{geo_n}/{rec_n} geo observation record(s)', 'solve')
            if db_con is not None:
                try:
                    _store_solver_path_preview(db_con, sess['path'], (sess.get('folder') + '/' if sess.get('folder') else '') + sess['name'], overview=True, max_points=_MAP_BULK_PREVIEW_POINTS)
                    _store_solver_session_manifest(db_con, sess, record_count=rec_n, source_count=len(rss_cells))
                    db_con.commit()
                except Exception as exc:
                    _broadcast_log(f'[solver-db] path/session store failed for {sess["name"]}: {type(exc).__name__}: {exc}', 'solve')
            _broadcast_path_ready(sess['path'], (sess.get('folder') + '/' if sess.get('folder') else '') + sess['name'], overview=True)

        # Pass 2: solve each real source exactly once using all sessions' cells.
        source_items = list(global_cells.items())
        skipped = 0
        for source_i, (sid, cells) in enumerate(source_items, 1):
            if _solve_stop.is_set():
                _broadcast_log('[batch] stopped by user during global solve', 'solve')
                break
            if source_i == 1 or source_i == len(source_items) or source_i % 100 == 0:
                _broadcast_progress(running=True, phase='batch-solving',
                                    pct=min(99.0, 70.0 + 29.0 * source_i / max(1, len(source_items))),
                                    text=f'Global solve {source_i}/{len(source_items)} source(s), current #{_anon_source_id(sid)}')
                time.sleep(0)
            try:
                pos_recs = _canonical_position_records_for_source(
                    sid, cells, global_meta.get(sid, {}), n_exp=n_exp, min_obs=min_obs,
                    include_unsolved=include_unsolved, rss_solve=rss_solve, rssi_centroid=rssi_centroid)
            except Exception as exc:
                skipped += 1
                _broadcast_log(f'[batch] source#{_anon_source_id(sid)} crashed in geo-guard/global solve: {type(exc).__name__}: {exc}', 'solve')
                continue
            if not pos_recs:
                skipped += 1
                continue
            if len(pos_recs) > 1:
                _broadcast_log(f'[batch] source#{_anon_source_id(sid)} split into {len(pos_recs)} geo-consistent cluster(s); MAC-only merge refused', 'solve')
            if db_con is not None:
                try:
                    db_con.execute('DELETE FROM positions WHERE source_id=? OR id=?', (sid, sid))
                except Exception:
                    pass
            for pos_rec in pos_recs:
                pos_rec['session_path'] = ''
                pos_rec['session_name'] = f'{len(source_sessions.get(sid, set()))} session(s)'
                pos_rec['sessions_seen'] = len(source_sessions.get(sid, set()))
                if db_path:
                    pos_rec['solver_db_path'] = str(db_path)
                    pos_rec['from_solver_db_capable'] = True
                _broadcast(pos_rec)
                if db_con is not None:
                    try:
                        _store_solver_position(db_con, pos_rec, None)
                        if (total_solved + total_indexed) % 50 == 0:
                            db_con.commit()
                    except Exception as exc:
                        _broadcast_log(f'[solver-db] position store failed for source#{_anon_source_id(sid)}: {type(exc).__name__}: {exc}', 'solve')
                if pos_rec.get('unsolved'):
                    total_indexed += 1
                else:
                    total_solved += 1
        if skipped:
            _broadcast_log(f'[batch] skipped {skipped} source(s) during global solve', 'solve')
    finally:
        if db_con is not None:
            try:
                db_con.commit()
                cnt = _db_counts(db_con)
                _broadcast_log(f'[solver-db] saved {cnt["positions"]} canonical positions, {cnt["samples"]} sample-cells, {cnt["sessions"]} session(s), {cnt["paths"]} path(s) → {Path(db_path).name}', 'solve')
            except Exception as exc:
                _broadcast_log(f'[solver-db] final save failed: {type(exc).__name__}: {exc}', 'solve')
            finally:
                try: db_con.close()
                except Exception: pass
        _broadcast_progress(running=False, phase='done', pct=100.0,
                            text=f'Bulk done: {total_solved} canonical positions, {total_indexed} evidence markers')
        _broadcast_log(
            f'[batch] done — {total_solved} RSS-solved canonical source(s), '
            f'{total_indexed} evidence-centroid source(s), {len(global_cells)} unique source(s), '
            f'{len(sessions)} session(s), {total_geo}/{total_records} geo observation record(s)',
            'solve')


def _run_solver_db_update(db_path: str, max_cells: int = _MAX_SOLVE_CELLS_PER_SOURCE,
                          max_sessions: int = 500,
                          n_exp: float = 2.5,
                          min_obs: int = 3,
                          include_unsolved: bool = True,
                          session_paths: list | tuple | None = None) -> None:
    """Incrementally update an existing solved DB with new/changed sessions."""
    from aetherward.position.rss import rss_solve, rssi_centroid

    p = Path(db_path)
    sessions = _batch_solve_sessions(max_sessions, session_paths=session_paths)
    changed_sessions = 0
    skipped_unchanged = 0
    missing_sessions = 0
    total_records = 0
    affected_sources: set[str] = set()
    meta_updates: dict = {}
    con = None
    _broadcast_progress(running=True, phase='db-update-start', pct=0.0,
                        text=f'Updating {p.name} from session manifest')
    try:
        con = sqlite3.connect(p)
        _init_solver_db(con)
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('updated_at',?)", (time.strftime('%Y-%m-%d %H:%M:%S'),))
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('mode',?)", ('incremental',))
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('settings',?)", (_json_dumps({'max_cells': max_cells, 'max_sessions': max_sessions, 'n_exp': n_exp, 'min_obs': min_obs, 'include_unsolved': include_unsolved, 'merge_by_source': True, 'geo_guard': True, 'selected_sessions': len(sessions)}),))
        _broadcast_log(f'[solver-db] updating DB in place: {p.name} from {len(sessions)} selected session(s)', 'solve')

        # Ingest only new/changed session files; unchanged manifest rows are skipped.
        for sess_i, sess in enumerate(sessions, 1):
            if _solve_stop.is_set():
                _broadcast_log('[solver-db] update stopped by user', 'solve')
                break
            status, _fp = _session_db_status(con, sess['path'])
            if status == 'missing':
                missing_sessions += 1
                continue
            if status == 'unchanged':
                skipped_unchanged += 1
                continue
            old_sources = _delete_session_contribution(con, sess['path']) if status == 'changed' else set()
            affected_sources.update(old_sources)
            try:
                def _prog(rec_n, src_n, frac, sess_i=sess_i, sess=sess, status=status):
                    _broadcast_progress(running=True, phase='db-update-indexing',
                                        pct=min(75.0, 75.0 * sess_i / max(1, len(sessions)) * max(0.05, frac)),
                                        text=f'Update {status} {sess_i}/{len(sessions)} {sess["name"]}: {rec_n} records, {src_n} sources')

                rss_cells, rss_meta, rec_n, geo_n = _index_session_for_solver(
                    sess, max_cells=max_cells, progress_cb=_prog)
            except Exception as exc:
                _broadcast_log(f'[solver-db] skip {sess["name"]}: {type(exc).__name__}: {exc}', 'solve')
                continue
            changed_sessions += 1
            total_records += rec_n
            for sid, cells in rss_cells.items():
                real_sid = str(sid)
                _store_solver_samples(con, real_sid, real_sid, sess['path'], cells)
                affected_sources.add(real_sid)
                meta_updates[real_sid] = _merge_source_meta(meta_updates.get(real_sid, {}), rss_meta.get(sid, {}))
            _store_solver_path_preview(con, sess['path'], (sess.get('folder') + '/' if sess.get('folder') else '') + sess['name'], overview=True, max_points=_MAP_BULK_PREVIEW_POINTS)
            _store_solver_session_manifest(con, sess, record_count=rec_n, source_count=len(rss_cells))
            con.commit()
            _broadcast_path_ready(sess['path'], (sess.get('folder') + '/' if sess.get('folder') else '') + sess['name'], overview=True)
            _broadcast_log(f'[solver-db] ingested {status} session {sess["name"]}: {len(rss_cells)} source(s), {geo_n}/{rec_n} geo observation record(s)', 'solve')

        if not affected_sources:
            cnt = _db_counts(con)
            _broadcast_log(f'[solver-db] no new/changed sessions. skipped={skipped_unchanged}, missing={missing_sessions}; DB unchanged.', 'solve')
            con.commit()
            con.close(); con = None
            _load_solver_positions(p, append=False)
            _broadcast_progress(running=False, phase='done', pct=100.0,
                                text=f'No update needed: {cnt["positions"]} positions already current')
            return

        # Recompute only touched sources from all evidence currently in the DB.
        solved = indexed = removed = errors = 0
        affected = sorted(affected_sources)
        for i, sid in enumerate(affected, 1):
            if _solve_stop.is_set():
                _broadcast_log('[solver-db] update stopped during source recompute', 'solve')
                break
            if i == 1 or i == len(affected) or i % 50 == 0:
                _broadcast_progress(running=True, phase='db-update-solving',
                                    pct=min(99.0, 75.0 + 24.0 * i / max(1, len(affected))),
                                    text=f'Recomputing touched sources {i}/{len(affected)}')
            cells = _cells_from_solver_samples(con, sid, max_cells=max_cells)
            if not cells:
                con.execute('DELETE FROM positions WHERE id=? OR source_id=?', (sid, sid))
                _broadcast_remove(sid)
                removed += 1
                continue
            meta = _merge_source_meta(_position_json_meta(con, sid), meta_updates.get(sid, {}))
            try:
                pos_recs = _canonical_position_records_for_source(
                    sid, cells, meta, n_exp=n_exp, min_obs=min_obs,
                    include_unsolved=include_unsolved, rss_solve=rss_solve, rssi_centroid=rssi_centroid)
            except Exception as exc:
                errors += 1
                _broadcast_log(f'[solver-db] source#{_anon_source_id(sid)} recompute failed in geo-guard: {type(exc).__name__}: {exc}', 'solve')
                continue
            if not pos_recs:
                con.execute('DELETE FROM positions WHERE id=? OR source_id=?', (sid, sid))
                _broadcast_remove(sid)
                removed += 1
                continue
            seen = _source_sessions_seen(con, sid)
            con.execute('DELETE FROM positions WHERE source_id=? OR id=?', (sid, sid))
            if len(pos_recs) > 1:
                _broadcast_log(f'[solver-db] source#{_anon_source_id(sid)} split into {len(pos_recs)} geo-consistent cluster(s); MAC-only merge refused', 'solve')
            for pos_rec in pos_recs:
                pos_rec['session_path'] = ''
                pos_rec['session_name'] = f'{seen} session(s)'
                pos_rec['sessions_seen'] = seen
                pos_rec['solver_db_path'] = str(p)
                pos_rec['from_solver_db_capable'] = True
                _store_solver_position(con, pos_rec, None)
                if pos_rec.get('unsolved'):
                    indexed += 1
                else:
                    solved += 1
            if i % 50 == 0:
                con.commit()
        con.commit()
        cnt = _db_counts(con)
        _broadcast_log(
            f'[solver-db] update complete: {changed_sessions} session(s) ingested, '
            f'{skipped_unchanged} unchanged skipped, {len(affected)} source(s) touched, '
            f'{solved} solved, {indexed} evidence, {removed} removed, {errors} errors. '
            f'DB now has {cnt["positions"]} positions / {cnt["sessions"]} sessions.', 'solve')
        con.close(); con = None
        _load_solver_positions(p, append=False)
        _broadcast_progress(running=False, phase='done', pct=100.0,
                            text=f'DB updated: {cnt["positions"]} positions, {cnt["sessions"]} sessions')
    except Exception as exc:
        _broadcast_log(f'[solver-db] update failed: {type(exc).__name__}: {exc}', 'solve')
        _broadcast_progress(running=False, phase='error', pct=0.0,
                            text=f'DB update failed: {type(exc).__name__}')
    finally:
        if con is not None:
            try: con.close()
            except Exception: pass


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
        progress = dict(_solve_progress)
    return {'version': __version__,
            'c_core': 'loaded' if c_available() else 'not available (Python fallback)',
            'home': str(AW_HOME), 'sessions_dir': str(AW_SESSIONS),
            'configs_dir': str(AW_CONFIGS),
            'solve_running': running, 'solve_session': session or '—',
            'solve_mode': 'live-follow' if follow and running else 'finite',
            'solve_progress': progress,
            'solver_dir': str(AW_SOLVER),
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
        body = _json_dumps(data).encode()
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
        elif p == '/api/solver/dbs':
            self._json(_list_solver_dbs())
        elif p == '/api/solver/db_paths':
            db = _solver_db_rel_or_path((qs.get('path') or [''])[0].strip())
            if not db or not db.exists():
                self._json({'error': 'db not found'}, 404); return
            self._json(_solver_db_paths(db))
        elif p == '/api/solver/db_status':
            db = _solver_db_rel_or_path((qs.get('path') or [''])[0].strip())
            if not db or not db.exists():
                self._json({'error': 'db not found'}, 404); return
            selected = qs.get('session') or []
            max_sessions = _safe_int((qs.get('max_sessions') or [''])[0], 500, 1, 5000)
            self._json(_solver_db_update_status(db, session_paths=selected, max_sessions=max_sessions))
        elif p == '/api/solver/path_records':
            db = _solver_db_rel_or_path((qs.get('db') or [''])[0].strip())
            spath = (qs.get('session_path') or [''])[0].strip()
            if not db or not db.exists():
                self._json({'error': 'db not found'}, 404); return
            self._json(_solver_db_path_records(db, spath))
        elif p == '/api/solver/source_samples':
            db = _solver_db_rel_or_path((qs.get('db') or [''])[0].strip())
            if not db or not db.exists():
                self._json({'error': 'db not found'}, 404); return
            max_obs = _safe_int((qs.get('max_obs') or [''])[0], 1500, 25, 10000)
            self._json(_solver_db_source_samples(
                db,
                position_id=(qs.get('position_id') or [''])[0].strip(),
                source_id=(qs.get('source') or [''])[0].strip(),
                session_path=(qs.get('session_path') or [''])[0].strip(),
                max_obs=max_obs))
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
        if p == '/api/solver/load':
            db = _solver_db_rel_or_path(str(data.get('path') or '').strip())
            if not db or not db.exists():
                self._json({'error': 'db not found'}, 404); return
            try:
                self._json(_load_solver_positions(db, append=bool(data.get('append'))))
            except Exception as exc:
                self._json({'error': f'{type(exc).__name__}: {exc}'}, 500)

        elif p == '/api/solver/import':
            name = str(data.get('name') or 'imported.sqlite').strip()
            b64 = str(data.get('b64') or '')
            stem = Path(name).stem
            if not stem or not _NAME_RE.match(stem):
                self._json({'error': 'invalid db name'}, 400); return
            if not name.lower().endswith(('.sqlite', '.db', '.awdb')):
                name = stem + '.sqlite'
            AW_SOLVER.mkdir(parents=True, exist_ok=True)
            dest = AW_SOLVER / name
            try:
                raw = base64.b64decode(b64.encode(), validate=True)
                dest.write_bytes(raw)
                con = sqlite3.connect(dest)
                try:
                    _init_solver_db(con)
                    counts = _db_counts(con)
                finally:
                    con.close()
            except Exception as exc:
                try: dest.unlink()
                except Exception: pass
                self._json({'error': f'invalid solver db: {type(exc).__name__}: {exc}'}, 400); return
            self._json({'ok': True, 'path': str(dest), **counts})

        elif p == '/api/solve/start':
            session = data.get('session', '')
            if not session or not Path(session).exists():
                self._json({'error': 'session not found'}, 400); return
            _solve_stop.set()
            if _solve_thread and _solve_thread.is_alive():
                _solve_thread.join(timeout=3)
            follow = bool(data.get('follow', False))
            _solve_stop.clear(); _solve_session = session; _solve_follow = follow
            if not follow:
                _clear_auto_positions('solve-start')
            _solve_thread = threading.Thread(
                target=_run_solver,
                args=(session, data.get('config'), data.get('n_exp', 2.5),
                      data.get('min_obs', 3), follow), daemon=True)
            _solve_thread.start()
            self._json({'ok': True, 'session': session})

        elif p == '/api/solve/stop':
            _solve_stop.set(); _solve_session = ''; _solve_follow = False
            _broadcast_progress(running=False, phase='stopping', pct=0.0, text='Stopping solver')
            self._json({'ok': True})

        elif p == '/api/solve/batch':
            max_cells = _safe_int(data.get('max_cells'), _MAX_SOLVE_CELLS_PER_SOURCE, 16, 2048)
            max_sessions = _safe_int(data.get('max_sessions'), 500, 1, 5000)
            try:
                n_exp = float(data.get('n_exp', 2.5))
            except (TypeError, ValueError):
                n_exp = 2.5
            min_obs = _safe_int(data.get('min_obs'), 3, 1, 1000)
            include_unsolved = bool(data.get('include_unsolved', True))
            selected_sessions = data.get('sessions') if isinstance(data.get('sessions'), list) else []
            _solve_stop.set()
            if _solve_thread and _solve_thread.is_alive():
                _solve_thread.join(timeout=3)
            _solve_stop.clear(); _solve_session = 'batch'; _solve_follow = False
            _clear_auto_positions('bulk-solve-start')
            _solve_thread = threading.Thread(
                target=_run_batch_solver, args=(max_cells, max_sessions, n_exp, min_obs, include_unsolved, selected_sessions), daemon=True)
            _solve_thread.start()
            sessions = _batch_solve_sessions(max_sessions, session_paths=selected_sessions)
            self._json({'ok': True, 'started': True, 'max_cells_per_source': max_cells,
                        'max_sessions': max_sessions, 'n_exp': n_exp, 'min_obs': min_obs,
                        'include_unsolved': include_unsolved, 'merge_by_source': True,
                        'sessions': sessions})

        elif p == '/api/solver/update_sessions':
            db = _solver_db_rel_or_path(str(data.get('path') or '').strip())
            if not db or not db.exists():
                self._json({'error': 'db not found'}, 404); return
            max_cells = _safe_int(data.get('max_cells'), _MAX_SOLVE_CELLS_PER_SOURCE, 16, 2048)
            max_sessions = _safe_int(data.get('max_sessions'), 500, 1, 5000)
            try:
                n_exp = float(data.get('n_exp', 2.5))
            except (TypeError, ValueError):
                n_exp = 2.5
            min_obs = _safe_int(data.get('min_obs'), 3, 1, 1000)
            include_unsolved = bool(data.get('include_unsolved', True))
            selected_sessions = data.get('sessions') if isinstance(data.get('sessions'), list) else []
            _solve_stop.set()
            if _solve_thread and _solve_thread.is_alive():
                _solve_thread.join(timeout=3)
            _solve_stop.clear(); _solve_session = 'db-update'; _solve_follow = False
            _clear_auto_positions('solver-db-update-start')
            _solve_thread = threading.Thread(
                target=_run_solver_db_update, args=(str(db), max_cells, max_sessions, n_exp, min_obs, include_unsolved, selected_sessions), daemon=True)
            _solve_thread.start()
            sessions = _batch_solve_sessions(max_sessions, session_paths=selected_sessions)
            self._json({'ok': True, 'started': True, 'path': str(db),
                        'max_cells_per_source': max_cells, 'max_sessions': max_sessions,
                        'n_exp': n_exp, 'min_obs': min_obs,
                        'include_unsolved': include_unsolved, 'sessions': sessions})

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

        q: queue.Queue = queue.Queue(maxsize=50000)
        with _sse_lock:
            _sse_clients.append(q)

        with _state_lock:
            snapshot = list(_positions.values())
        for rec in snapshot:
            try:
                self.wfile.write(
                    _sse_event({'type': 'position', **rec}).encode())
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
