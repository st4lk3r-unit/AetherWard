from __future__ import annotations

import argparse
import csv
import html
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from aetherward.session import is_gps_record, record_type
except Exception:  # pragma: no cover - standalone fallback
    def record_type(rec: dict) -> str:
        return str(rec.get('record_type', rec.get('type', 'observation')) or 'observation').lower()
    def is_gps_record(rec: dict) -> bool:
        return record_type(rec) == 'gps'


@dataclass
class Issue:
    severity: str
    message: str
    line: int | None = None


@dataclass
class GeoPoint:
    line: int
    index: int
    t: float | None
    lat: float
    lon: float
    alt: float | None
    kind: str
    rec_id: str
    rssi: float | None = None


@dataclass
class SessionSanity:
    path: str
    total_lines: int = 0
    parsed_records: int = 0
    blank_lines: int = 0
    bad_json: int = 0
    record_types: Counter = field(default_factory=Counter)
    geotagged: list[GeoPoint] = field(default_factory=list)
    geotagged_gps: list[GeoPoint] = field(default_factory=list)
    geotagged_obs: list[GeoPoint] = field(default_factory=list)
    missing_geo_by_type: Counter = field(default_factory=Counter)
    invalid_geo: int = 0
    null_time: int = 0
    time_backsteps: int = 0
    duplicate_times: int = 0
    issues: list[Issue] = field(default_factory=list)
    first_t: float | None = None
    last_t: float | None = None
    file_size: int = 0

    @property
    def ok_records(self) -> int:
        return self.parsed_records - self.invalid_geo


def _as_float(value: Any) -> float | None:
    if value is None or value == '':
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def _rec_id(rec: dict) -> str:
    for key in ('id', 'identifier', 'bssid', 'addr2', 'source', 'ssid'):
        val = rec.get(key)
        if val not in (None, '', [], {}):
            return str(val)
    meta = rec.get('metadata') if isinstance(rec.get('metadata'), dict) else {}
    for key in ('identifier', 'bssid', 'addr2', 'ssid'):
        val = meta.get(key)
        if val not in (None, '', [], {}):
            return str(val)
    if is_gps_record(rec):
        return 'GPS track'
    return 'anon'


def _haversine_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    r = 6_371_000.0
    p1 = math.radians(a_lat)
    p2 = math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lon - a_lon)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(x), math.sqrt(max(0.0, 1.0 - x)))


def _fmt_time(t: float | None) -> str:
    if t is None:
        return '—'
    try:
        import datetime as _dt
        return _dt.datetime.fromtimestamp(float(t)).isoformat(timespec='seconds')
    except Exception:
        return f'{t:.3f}'


def _fmt_coord(p: GeoPoint | None) -> str:
    if p is None:
        return '—'
    return f'{p.lat:.7f},{p.lon:.7f}  line={p.line}  t={_fmt_time(p.t)}  {p.kind}'


def _point_key(p: GeoPoint, decimals: int = 7) -> tuple[float, float]:
    return (round(p.lat, decimals), round(p.lon, decimals))


def load_session(path: str | Path) -> SessionSanity:
    p = Path(path).expanduser()
    report = SessionSanity(path=str(p))
    try:
        report.file_size = p.stat().st_size
    except OSError:
        report.issues.append(Issue('ERROR', f'cannot stat file: {p}'))
        return report

    prev_t: float | None = None
    seen_times: set[float] = set()

    try:
        fh = p.open('r', encoding='utf-8', errors='replace')
    except OSError as exc:
        report.issues.append(Issue('ERROR', f'cannot open file: {exc}'))
        return report

    with fh:
        for lineno, raw in enumerate(fh, 1):
            report.total_lines += 1
            if not raw.strip():
                report.blank_lines += 1
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError as exc:
                report.bad_json += 1
                report.issues.append(Issue('ERROR', f'bad JSON: {exc.msg}', lineno))
                continue
            if not isinstance(rec, dict):
                report.bad_json += 1
                report.issues.append(Issue('ERROR', 'JSON line is not an object', lineno))
                continue

            report.parsed_records += 1
            idx = report.parsed_records - 1
            rt = record_type(rec)
            report.record_types[rt] += 1

            t = _as_float(rec.get('t', rec.get('timestamp')))
            if t is None:
                report.null_time += 1
            else:
                if report.first_t is None:
                    report.first_t = t
                report.last_t = t
                if prev_t is not None and t < prev_t:
                    report.time_backsteps += 1
                    if report.time_backsteps <= 10:
                        report.issues.append(Issue('WARN', f'timestamp moved backwards ({t:.3f} < {prev_t:.3f})', lineno))
                if t in seen_times:
                    report.duplicate_times += 1
                seen_times.add(t)
                prev_t = t

            lat = _as_float(rec.get('lat'))
            lon = _as_float(rec.get('lon'))
            if lat is None or lon is None:
                report.missing_geo_by_type[rt] += 1
                continue
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                report.invalid_geo += 1
                report.issues.append(Issue('ERROR', f'invalid lat/lon: {lat!r},{lon!r}', lineno))
                continue
            if abs(lat) < 1e-12 and abs(lon) < 1e-12:
                report.issues.append(Issue('WARN', 'lat/lon is 0,0 null-island-looking coordinate', lineno))

            gp = GeoPoint(
                line=lineno,
                index=idx,
                t=t,
                lat=lat,
                lon=lon,
                alt=_as_float(rec.get('alt')),
                kind='gps' if is_gps_record(rec) else 'observation',
                rec_id=_rec_id(rec),
                rssi=_as_float(rec.get('rssi')),
            )
            report.geotagged.append(gp)
            if gp.kind == 'gps':
                report.geotagged_gps.append(gp)
            else:
                report.geotagged_obs.append(gp)

    _post_analyse(report)
    return report


def _post_analyse(report: SessionSanity) -> None:
    if report.bad_json:
        report.issues.append(Issue('ERROR', f'{report.bad_json} malformed JSON line(s)'))
    if not report.geotagged:
        report.issues.append(Issue('ERROR', 'no geotagged records with valid lat/lon'))
        return

    pts_file = report.geotagged
    pts_time = sorted(report.geotagged, key=lambda p: (float('inf') if p.t is None else p.t, p.line))

    # Does the time-sorted route drop a large final tail compared to file order?
    if pts_file and pts_time and pts_file[-1].line != pts_time[-1].line:
        report.issues.append(Issue(
            'WARN',
            f'last geotagged point by file order is line {pts_file[-1].line}, '
            f'but last by timestamp is line {pts_time[-1].line}; JS time sorting may reorder the trail',
        ))

    unique_pos = len({_point_key(p) for p in report.geotagged})
    if unique_pos <= max(1, len(report.geotagged) // 100):
        report.issues.append(Issue('WARN', f'only {unique_pos} unique GPS coordinate(s) across {len(report.geotagged)} geotagged records'))

    stale = stale_runs(report.geotagged, min_count=10, min_duration_s=10.0)
    if stale:
        longest = max(stale, key=lambda r: (r['count'], r.get('duration_s') or 0))
        report.issues.append(Issue(
            'WARN',
            f'longest stale coordinate run: {longest["count"]} point(s), '
            f'{longest.get("duration_s", 0):.1f}s, lines {longest["start_line"]}-{longest["end_line"]}',
        ))

    gaps = route_gaps(report.geotagged, min_gap_s=10.0, min_jump_m=250.0)
    if gaps:
        worst_time = max(gaps, key=lambda g: g.get('dt_s') or 0)
        report.issues.append(Issue(
            'WARN',
            f'large time gap in geotagged trail: {worst_time.get("dt_s", 0):.1f}s '
            f'between lines {worst_time["from_line"]}-{worst_time["to_line"]}',
        ))
        worst_jump = max(gaps, key=lambda g: g.get('dist_m') or 0)
        if worst_jump.get('dist_m', 0) >= 250.0:
            report.issues.append(Issue(
                'WARN',
                f'large position jump: {worst_jump.get("dist_m", 0):.1f}m '
                f'between lines {worst_jump["from_line"]}-{worst_jump["to_line"]}',
            ))

    if report.geotagged_obs and not report.geotagged_gps:
        # Old session format. Not necessarily bad, but explains route-via-frames.
        report.issues.append(Issue(
            'INFO',
            'no GPS breadcrumb records found; trail is observation/frame locations only',
        ))

    # Last half/tail sanity: catch "A→B visible, B→C missing".
    if len(report.geotagged) >= 20:
        n = len(report.geotagged)
        tail = report.geotagged[n // 2:]
        tail_unique = len({_point_key(p) for p in tail})
        head_unique = len({_point_key(p) for p in report.geotagged[:n // 2]})
        if tail_unique <= 2 and head_unique > 5:
            report.issues.append(Issue(
                'WARN',
                f'back half has only {tail_unique} unique coordinate(s) while first half has {head_unique}; likely stale GPS after mid-session',
            ))


def route_stats(points: list[GeoPoint]) -> dict[str, Any]:
    if not points:
        return {'count': 0}
    pts = sorted(points, key=lambda p: (float('inf') if p.t is None else p.t, p.line))
    distances = [_haversine_m(a.lat, a.lon, b.lat, b.lon) for a, b in zip(pts, pts[1:])]
    total = sum(distances)
    straight = _haversine_m(pts[0].lat, pts[0].lon, pts[-1].lat, pts[-1].lon) if len(pts) >= 2 else 0.0
    uniq = len({_point_key(p) for p in pts})
    ts = [p.t for p in pts if p.t is not None]
    dts = [b.t - a.t for a, b in zip(pts, pts[1:]) if a.t is not None and b.t is not None and b.t >= a.t]
    return {
        'count': len(pts),
        'unique_coords': uniq,
        'start': pts[0],
        'end': pts[-1],
        'bounds': (min(p.lat for p in pts), min(p.lon for p in pts), max(p.lat for p in pts), max(p.lon for p in pts)),
        'distance_m': total,
        'straight_m': straight,
        'first_t': min(ts) if ts else None,
        'last_t': max(ts) if ts else None,
        'duration_s': (max(ts) - min(ts)) if len(ts) >= 2 else 0.0,
        'median_dt_s': statistics.median(dts) if dts else None,
        'max_dt_s': max(dts) if dts else None,
        'max_step_m': max(distances) if distances else 0.0,
    }


def route_gaps(points: list[GeoPoint], min_gap_s: float = 10.0, min_jump_m: float = 250.0) -> list[dict[str, Any]]:
    pts = sorted([p for p in points if p.t is not None], key=lambda p: (p.t, p.line))
    gaps = []
    for a, b in zip(pts, pts[1:]):
        dt = (b.t or 0.0) - (a.t or 0.0)
        dist = _haversine_m(a.lat, a.lon, b.lat, b.lon)
        if dt >= min_gap_s or dist >= min_jump_m:
            gaps.append({
                'from_line': a.line, 'to_line': b.line,
                'from_t': a.t, 'to_t': b.t,
                'dt_s': dt, 'dist_m': dist,
                'from_lat': a.lat, 'from_lon': a.lon,
                'to_lat': b.lat, 'to_lon': b.lon,
            })
    return gaps


def stale_runs(points: list[GeoPoint], min_count: int = 10, min_duration_s: float = 10.0,
               decimals: int = 7) -> list[dict[str, Any]]:
    if not points:
        return []
    pts = sorted(points, key=lambda p: (float('inf') if p.t is None else p.t, p.line))
    runs = []
    start = prev = pts[0]
    count = 1
    for p in pts[1:]:
        if _point_key(p, decimals) == _point_key(prev, decimals):
            prev = p
            count += 1
            continue
        _maybe_add_stale(runs, start, prev, count, min_count, min_duration_s)
        start = prev = p
        count = 1
    _maybe_add_stale(runs, start, prev, count, min_count, min_duration_s)
    return runs


def _maybe_add_stale(out: list[dict[str, Any]], start: GeoPoint, end: GeoPoint, count: int,
                     min_count: int, min_duration_s: float) -> None:
    duration = None
    if start.t is not None and end.t is not None:
        duration = max(0.0, end.t - start.t)
    if count >= min_count or (duration is not None and duration >= min_duration_s):
        out.append({
            'count': count,
            'duration_s': duration or 0.0,
            'start_line': start.line,
            'end_line': end.line,
            'lat': start.lat,
            'lon': start.lon,
            'start_t': start.t,
            'end_t': end.t,
        })


def source_summary(points: list[GeoPoint], limit: int = 12) -> list[tuple[str, int, int]]:
    d: dict[str, list[GeoPoint]] = defaultdict(list)
    for p in points:
        d[p.rec_id].append(p)
    rows = []
    for sid, pts in d.items():
        rows.append((sid, len(pts), len({_point_key(p) for p in pts})))
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows[:limit]


def _human_bytes(n: int) -> str:
    units = ['B', 'KB', 'MB', 'GB']
    f = float(n)
    for u in units:
        if f < 1024.0 or u == units[-1]:
            return f'{f:.1f} {u}' if u != 'B' else f'{int(f)} B'
        f /= 1024.0
    return f'{n} B'


def format_report(report: SessionSanity, *, details: bool = False) -> str:
    all_stats = route_stats(report.geotagged)
    gps_stats = route_stats(report.geotagged_gps)
    obs_stats = route_stats(report.geotagged_obs)

    lines = []
    lines.append(f'Session sanity: {report.path}')
    lines.append('=' * min(88, max(18, len(lines[-1]))))
    lines.append(f'File: {_human_bytes(report.file_size)}  lines={report.total_lines}  parsed={report.parsed_records}  bad_json={report.bad_json}  blank={report.blank_lines}')
    if report.record_types:
        lines.append('Record types: ' + ', '.join(f'{k}={v}' for k, v in sorted(report.record_types.items())))
    lines.append(f'Geotagged: all={len(report.geotagged)}  gps={len(report.geotagged_gps)}  observations={len(report.geotagged_obs)}  invalid_geo={report.invalid_geo}')
    if report.missing_geo_by_type:
        lines.append('Missing lat/lon: ' + ', '.join(f'{k}={v}' for k, v in sorted(report.missing_geo_by_type.items())))
    lines.append(f'Time: first={_fmt_time(report.first_t)}  last={_fmt_time(report.last_t)}  null_t={report.null_time}  backsteps={report.time_backsteps}  duplicate_t={report.duplicate_times}')

    def add_stats(name: str, st: dict[str, Any]) -> None:
        if not st.get('count'):
            lines.append(f'{name}: no points')
            return
        b = st['bounds']
        lines.append(
            f'{name}: points={st["count"]} unique={st["unique_coords"]} '
            f'duration={st["duration_s"]:.1f}s distance={st["distance_m"]:.1f}m '
            f'straight={st["straight_m"]:.1f}m max_step={st["max_step_m"]:.1f}m'
        )
        lines.append(f'  start: {_fmt_coord(st["start"])}')
        lines.append(f'  end:   {_fmt_coord(st["end"])}')
        lines.append(f'  bbox:  lat {b[0]:.7f}..{b[2]:.7f}  lon {b[1]:.7f}..{b[3]:.7f}')
        if st.get('median_dt_s') is not None:
            lines.append(f'  dt:    median={st["median_dt_s"]:.3f}s  max={st["max_dt_s"]:.3f}s')

    lines.append('')
    add_stats('DUMB TRAIL all geotagged records', all_stats)
    add_stats('GPS breadcrumbs only', gps_stats)
    add_stats('Observation/frame locations only', obs_stats)

    if report.geotagged_obs:
        lines.append('')
        lines.append('Top geotagged sources:')
        for sid, count, uniq in source_summary(report.geotagged_obs):
            lines.append(f'  {sid:<32.32} points={count:<6} unique_pos={uniq}')

    gaps = route_gaps(report.geotagged)
    stale = stale_runs(report.geotagged)
    if details:
        if gaps:
            lines.append('')
            lines.append('Largest route gaps/jumps:')
            for g in sorted(gaps, key=lambda x: (x.get('dt_s', 0), x.get('dist_m', 0)), reverse=True)[:20]:
                lines.append(
                    f'  lines {g["from_line"]}->{g["to_line"]}: dt={g["dt_s"]:.1f}s '
                    f'dist={g["dist_m"]:.1f}m '
                    f'{g["from_lat"]:.7f},{g["from_lon"]:.7f} -> {g["to_lat"]:.7f},{g["to_lon"]:.7f}'
                )
        if stale:
            lines.append('')
            lines.append('Stale coordinate runs:')
            for r in sorted(stale, key=lambda x: (x['count'], x['duration_s']), reverse=True)[:20]:
                lines.append(
                    f'  lines {r["start_line"]}-{r["end_line"]}: count={r["count"]} '
                    f'duration={r["duration_s"]:.1f}s coord={r["lat"]:.7f},{r["lon"]:.7f}'
                )

    lines.append('')
    if report.issues:
        lines.append('Issues:')
        for issue in report.issues[:80]:
            loc = f' line {issue.line}:' if issue.line is not None else ''
            lines.append(f'  [{issue.severity}]{loc} {issue.message}')
        if len(report.issues) > 80:
            lines.append(f'  ... {len(report.issues) - 80} more issue(s)')
    else:
        lines.append('Issues: none obvious')

    return '\n'.join(lines) + '\n'


def write_csv(report: SessionSanity, path: str | Path) -> None:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['line', 'index', 't', 'kind', 'id', 'lat', 'lon', 'alt', 'rssi'])
        w.writeheader()
        for gp in report.geotagged:
            w.writerow({
                'line': gp.line,
                'index': gp.index,
                't': '' if gp.t is None else gp.t,
                'kind': gp.kind,
                'id': gp.rec_id,
                'lat': gp.lat,
                'lon': gp.lon,
                'alt': '' if gp.alt is None else gp.alt,
                'rssi': '' if gp.rssi is None else gp.rssi,
            })


def write_geojson(report: SessionSanity, path: str | Path) -> None:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    features = []
    pts = sorted(report.geotagged, key=lambda p: (float('inf') if p.t is None else p.t, p.line))
    if len(pts) >= 2:
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'LineString', 'coordinates': [[x.lon, x.lat] for x in pts]},
            'properties': {'name': 'dumb-all-geotagged-trail', 'points': len(pts)},
        })
    for gp in pts:
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [gp.lon, gp.lat] + ([] if gp.alt is None else [gp.alt])},
            'properties': {
                'line': gp.line, 'index': gp.index, 't': gp.t,
                'kind': gp.kind, 'id': gp.rec_id, 'rssi': gp.rssi,
            },
        })
    p.write_text(json.dumps({'type': 'FeatureCollection', 'features': features}, indent=2), encoding='utf-8')


def write_html(report: SessionSanity, path: str | Path) -> None:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    pts = sorted(report.geotagged, key=lambda p: (float('inf') if p.t is None else p.t, p.line))
    points = [
        {
            'line': gp.line,
            't': gp.t,
            'kind': gp.kind,
            'id': gp.rec_id,
            'lat': gp.lat,
            'lon': gp.lon,
            'rssi': gp.rssi,
        }
        for gp in pts
    ]
    stats = {
        'all': _stats_for_json(route_stats(report.geotagged)),
        'gps': _stats_for_json(route_stats(report.geotagged_gps)),
        'obs': _stats_for_json(route_stats(report.geotagged_obs)),
        'issues': [{'severity': i.severity, 'line': i.line, 'message': i.message} for i in report.issues[:50]],
    }
    title = f'AetherWard session sanity — {Path(report.path).name}'
    doc = f'''<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
html,body,#map{{height:100%;margin:0;background:#08080b;color:#eee;font-family:system-ui,Segoe UI,sans-serif}}
#panel{{position:absolute;z-index:999;left:12px;top:12px;max-width:min(520px,calc(100vw - 24px));background:rgba(10,10,14,.90);border:1px solid #333;border-radius:12px;padding:12px;box-shadow:0 8px 30px #0008}}
button{{background:#20202a;color:#eee;border:1px solid #555;border-radius:8px;padding:5px 9px;margin:2px;cursor:pointer}}
pre{{white-space:pre-wrap;max-height:32vh;overflow:auto;font-size:12px;color:#ccc}}
.bad{{color:#ff7373}} .warn{{color:#ffd166}} .ok{{color:#6ee7a8}} .muted{{color:#aaa}}
</style></head>
<body><div id="map"></div><div id="panel">
<b>{html.escape(title)}</b><br>
<span class="muted">This view draws every valid geotagged JSONL sample directly. No solver, no source grouping.</span><br>
<button onclick="show('all')">all geotagged</button><button onclick="show('gps')">gps only</button><button onclick="show('obs')">observations only</button>
<button onclick="toggleDots()">dots</button>
<pre id="summary"></pre>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const points = {json.dumps(points, separators=(',', ':'))};
const stats = {json.dumps(stats, separators=(',', ':'))};
let map = L.map('map');
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{maxZoom: 19, attribution: '© OpenStreetMap'}}).addTo(map);
let line = null, dots = [], dotsOn = true;
function byMode(mode) {{
  if(mode==='gps') return points.filter(p=>p.kind==='gps');
  if(mode==='obs') return points.filter(p=>p.kind!=='gps');
  return points;
}}
function colorFor(p) {{ return p.kind==='gps' ? '#38bdf8' : '#ef4444'; }}
function show(mode) {{
  if(line) line.remove(); dots.forEach(d=>d.remove()); dots=[];
  const arr = byMode(mode).filter(p=>p.lat!=null&&p.lon!=null);
  if(!arr.length) {{ document.getElementById('summary').textContent='No points for '+mode; return; }}
  line = L.polyline(arr.map(p=>[p.lat,p.lon]), {{color: mode==='gps'?'#38bdf8':(mode==='obs'?'#ef4444':'#f97316'), weight:3, opacity:.9}}).addTo(map);
  dots = arr.map((p,i)=>{{
    const d=L.circleMarker([p.lat,p.lon], {{radius:p.kind==='gps'?3:4, color:colorFor(p), fillColor:colorFor(p), fillOpacity:.7, weight:1}});
    d.bindPopup(`<b>${{p.kind}}</b> line=${{p.line}}<br>${{p.lat.toFixed(7)}}, ${{p.lon.toFixed(7)}}<br>t=${{p.t??'?'}}<br>${{p.id||''}}`);
    if(dotsOn) d.addTo(map); return d;
  }});
  map.fitBounds(line.getBounds(), {{padding:[24,24]}});
  const s = stats[mode] || stats.all;
  const issues = (stats.issues||[]).map(i=>`[${{i.severity}}]${{i.line?' line '+i.line+':':''}} ${{i.message}}`).join('\n');
  document.getElementById('summary').textContent =
    `mode=${{mode}} points=${{arr.length}} unique=${{s.unique_coords??'?'}}\n`+
    `duration=${{(s.duration_s??0).toFixed(1)}}s distance=${{(s.distance_m??0).toFixed(1)}}m max_step=${{(s.max_step_m??0).toFixed(1)}}m\n`+
    `start=${{s.start||'—'}}\nend=${{s.end||'—'}}\n\n` + (issues || 'No obvious issues');
}}
function toggleDots() {{ dotsOn=!dotsOn; dots.forEach(d=>dotsOn?d.addTo(map):d.remove()); }}
show('all');
</script></body></html>'''
    p.write_text(doc, encoding='utf-8')


def _stats_for_json(st: dict[str, Any]) -> dict[str, Any]:
    out = dict(st)
    for key in ('start', 'end'):
        p = out.get(key)
        if isinstance(p, GeoPoint):
            out[key] = f'{p.lat:.7f},{p.lon:.7f} line={p.line} t={_fmt_time(p.t)} {p.kind}'
    if 'bounds' in out and isinstance(out['bounds'], tuple):
        out['bounds'] = list(out['bounds'])
    return out


def default_output_path(session_path: str | Path, suffix: str) -> Path:
    p = Path(session_path).expanduser()
    if p.suffix == '.jsonl':
        return p.with_suffix(suffix)
    return p.with_name(p.name + suffix)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog='session_sanity.py',
        description='Dumb AetherWard JSONL session checker: validates records and draws raw geotagged samples.',
    )
    ap.add_argument('session', help='Path to a .jsonl session file')
    ap.add_argument('--html', nargs='?', const='', metavar='FILE', help='Write dumb HTML map. Without FILE, writes next to session.')
    ap.add_argument('--geojson', nargs='?', const='', metavar='FILE', help='Write dumb GeoJSON from every geotagged sample')
    ap.add_argument('--csv', nargs='?', const='', metavar='FILE', help='Write geotagged samples as CSV')
    ap.add_argument('--details', action='store_true', help='Show detailed stale runs and gaps')
    ap.add_argument('--fail-on-error', action='store_true', help='Exit non-zero on malformed JSON or invalid/no geotagged records')
    args = ap.parse_args(argv)

    report = load_session(args.session)
    print(format_report(report, details=args.details), end='')

    if args.html is not None:
        out = Path(args.html).expanduser() if args.html else default_output_path(args.session, '.sanity.html')
        write_html(report, out)
        print(f'Wrote HTML sanity map: {out}')
    if args.geojson is not None:
        out = Path(args.geojson).expanduser() if args.geojson else default_output_path(args.session, '.sanity.geojson')
        write_geojson(report, out)
        print(f'Wrote GeoJSON: {out}')
    if args.csv is not None:
        out = Path(args.csv).expanduser() if args.csv else default_output_path(args.session, '.sanity.csv')
        write_csv(report, out)
        print(f'Wrote CSV: {out}')

    if args.fail_on_error:
        has_error = any(i.severity == 'ERROR' for i in report.issues)
        return 2 if has_error else 0
    return 0


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())
