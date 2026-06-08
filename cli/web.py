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


def _batch_solve_sessions(max_sessions: int = 500) -> list[dict]:
    return [s for s in _list_sessions()
            if s['stype'] in ('wardriver', 'tdoa_raw', 'unknown')][:max_sessions]


def _batch_marker_id(source_id: str, session_path: str) -> str:
    """Namespace bulk-solved markers by session so same MACs do not overwrite."""
    digest = hashlib.sha1(str(session_path).encode('utf-8', 'ignore')).hexdigest()[:10]
    return f'{source_id}#{digest}'



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
        PRIMARY KEY(position_id, seq)
    )""")
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
    }


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
        con.execute('DELETE FROM source_samples WHERE position_id=?', (position_id,))
        rows = _cells_to_sample_rows(position_id, source_id, session_path, cells)
        if rows:
            con.executemany("""INSERT OR REPLACE INTO source_samples
                (position_id,source_id,session_path,seq,lat,lon,rssi,count,json)
                VALUES(?,?,?,?,?,?,?,?,?)""", rows)


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
        args = []
        where = []
        if position_id:
            where.append('position_id=?'); args.append(position_id)
        if source_id:
            where.append('source_id=?'); args.append(source_id)
        if session_path:
            where.append('session_path=?'); args.append(session_path)
        if not where:
            return []
        sql = 'SELECT json FROM source_samples WHERE ' + ' AND '.join(where) + ' ORDER BY seq LIMIT ?'
        args.append(max_obs)
        return [json.loads(r[0]) for r in con.execute(sql, args).fetchall()]
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
                      include_unsolved: bool = True) -> None:
    """Pi-friendly bulk solver for many saved sessions.

    It runs in the same background slot as the normal solver so status/Stop work
    and HTTP requests do not block until every fat JSONL is processed.
    """
    from collections import defaultdict
    from aetherward.position.rss import rss_solve, rssi_centroid
    from aetherward.session import is_gps_record, record_source_id, source_meta_from_record

    sessions = _batch_solve_sessions(max_sessions)
    total_solved = 0
    total_indexed = 0
    total_records = 0
    db_con = None
    db_path = None
    try:
        db_path = _create_solver_db('bulk', mode='bulk', settings={'max_cells': max_cells, 'max_sessions': max_sessions, 'n_exp': n_exp, 'min_obs': min_obs, 'include_unsolved': include_unsolved})
        db_con = sqlite3.connect(db_path)
        _init_solver_db(db_con)
        _broadcast_log(f'[solver-db] writing bulk solved DB: {Path(db_path).name}', 'solve')
    except Exception as exc:
        _broadcast_log(f'[solver-db] disabled: {type(exc).__name__}: {exc}', 'solve')
        db_con = None
    _broadcast_progress(running=True, phase='batch-start', pct=0.0,
                        text=f'Starting bulk solve: {len(sessions)} sessions')
    _broadcast_log(
        f'[batch] starting {len(sessions)} session(s), ≤{max_cells} geo-cells/source, '
        f'min_obs={min_obs}, n_exp={n_exp:g}, include_unsolved={include_unsolved}; '
        f'auto-loading lightweight route previews',
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
                        now = time.time()
                        if now - last_prog > 0.8:
                            base = (sess_i - 1) / max(1, len(sessions)) * 100.0
                            span = 100.0 / max(1, len(sessions))
                            pct = min(99.0, base + span * 0.75 * (pos_now / sess_size))
                            _broadcast_progress(running=True, phase='batch-indexing', pct=pct,
                                                text=f'Bulk {sess_i}/{len(sessions)} indexing {sess["name"]}: {rec_n} records, {len(rss_cells)} sources')
                            last_prog = now
                            time.sleep(0)
            except Exception as exc:
                _broadcast_log(f'[batch] skip {sess["name"]}: {exc}', 'solve')
                continue

            solved_this = 0
            indexed_this = 0
            skipped_this = 0
            total_sources_this = max(1, len(rss_cells))
            for source_i, (sid, cells) in enumerate(rss_cells.items(), 1):
                if _solve_stop.is_set():
                    break
                if source_i == 1 or source_i == total_sources_this or source_i % 100 == 0:
                    base = (sess_i - 1) / max(1, len(sessions)) * 100.0
                    span = 100.0 / max(1, len(sessions))
                    _broadcast_progress(running=True, phase='batch-solving',
                                        pct=min(99.0, base + span * (0.75 + 0.24 * source_i / total_sources_this)),
                                        text=f'Bulk {sess_i}/{len(sessions)} solving {source_i}/{total_sources_this}: {sess["name"]}')
                    time.sleep(0)
                try:
                    pos_rec = _solve_rss_position(
                        sid, cells, rss_meta.get(sid, {}), n_exp, min_obs,
                        rss_solve, rssi_centroid)
                except Exception as exc:
                    skipped_this += 1
                    _broadcast_log(f'[batch] source#{_anon_source_id(sid)} crashed in {sess["name"]}: '
                                   f'{type(exc).__name__}: {exc}', 'solve')
                    continue
                if pos_rec is None:
                    if include_unsolved:
                        pos_rec = _source_observation_centroid(
                            sid, cells, rss_meta.get(sid, {}),
                            reason=f'needs ≥{min_obs} distinct geo-cells for RSS solve')
                    if pos_rec is None:
                        skipped_this += 1
                        continue
                # In batch mode the same AP/client MAC can legitimately appear
                # in several session files.  The browser/server state is keyed
                # by `id`, so keep the real MAC in source_id/identifier and give
                # each session's solved marker a stable namespaced id.
                real_sid = str(sid)
                pos_rec['source_id'] = real_sid
                pos_rec['identifier'] = real_sid
                pos_rec['session_path'] = sess['path']
                pos_rec['session_name'] = sess['name']
                if sess.get('folder'):
                    pos_rec['session_folder'] = sess.get('folder')
                pos_rec['id'] = _batch_marker_id(real_sid, sess['path'])
                if db_path:
                    pos_rec['solver_db_path'] = str(db_path)
                    pos_rec['from_solver_db_capable'] = True
                _broadcast(pos_rec)
                if db_con is not None:
                    try:
                        _store_solver_position(db_con, pos_rec, cells)
                        if (solved_this + indexed_this) % 50 == 0: db_con.commit()
                    except Exception as exc:
                        _broadcast_log(f'[solver-db] store failed for source#{_anon_source_id(real_sid)} in {sess["name"]}: {type(exc).__name__}: {exc}', 'solve')
                if pos_rec.get('unsolved'):
                    total_indexed += 1
                    indexed_this += 1
                else:
                    total_solved += 1
                    solved_this += 1

            total_records += rec_n
            _broadcast_log(
                f'[batch] {sess_i}/{len(sessions)} {sess["name"]}: '
                f'{solved_this} solved, {indexed_this} evidence-centroid, '
                f'{skipped_this} skipped, {geo_n}/{rec_n} geo observation record(s), '
                f'≤{max_cells} cells/source',
                'solve')
            if db_con is not None:
                try:
                    _store_solver_path_preview(db_con, sess['path'], (sess.get('folder') + '/' if sess.get('folder') else '') + sess['name'], overview=True, max_points=_MAP_BULK_PREVIEW_POINTS)
                    db_con.commit()
                except Exception as exc:
                    _broadcast_log(f'[solver-db] path store failed for {sess["name"]}: {type(exc).__name__}: {exc}', 'solve')
            _broadcast_path_ready(sess['path'], (sess.get('folder') + '/' if sess.get('folder') else '') + sess['name'], overview=True)
    finally:
        if db_con is not None:
            try:
                db_con.commit()
                cnt = _db_counts(db_con)
                _broadcast_log(f'[solver-db] saved {cnt["positions"]} positions, {cnt["samples"]} sample-cells, {cnt["paths"]} path(s) → {Path(db_path).name}', 'solve')
            except Exception as exc:
                _broadcast_log(f'[solver-db] final save failed: {type(exc).__name__}: {exc}', 'solve')
            finally:
                try: db_con.close()
                except Exception: pass
        _broadcast_progress(running=False, phase='done', pct=100.0,
                            text=f'Bulk done: {total_solved} real positions, {total_indexed} evidence markers')
        _broadcast_log(
            f'[batch] done — {total_solved} RSS-solved source(s), '
            f'{total_indexed} evidence-centroid source(s) from {len(sessions)} session(s), '
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
            _solve_stop.set()
            if _solve_thread and _solve_thread.is_alive():
                _solve_thread.join(timeout=3)
            _solve_stop.clear(); _solve_session = 'batch'; _solve_follow = False
            _clear_auto_positions('bulk-solve-start')
            _solve_thread = threading.Thread(
                target=_run_batch_solver, args=(max_cells, max_sessions, n_exp, min_obs, include_unsolved), daemon=True)
            _solve_thread.start()
            sessions = _batch_solve_sessions(max_sessions)
            self._json({'ok': True, 'started': True, 'max_cells_per_source': max_cells,
                        'max_sessions': max_sessions, 'n_exp': n_exp, 'min_obs': min_obs,
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
