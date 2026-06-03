from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from aetherward.session_sanity import (
    format_report,
    load_session,
    route_gaps,
    stale_runs,
    write_csv,
    write_geojson,
    write_html,
)


def _write(path: Path, rows: list[dict | str]) -> None:
    lines = []
    for row in rows:
        if isinstance(row, str):
            lines.append(row)
        else:
            lines.append(json.dumps(row))
    path.write_text('\n'.join(lines) + '\n')


def test_session_sanity_counts_gps_and_observations(tmp_path):
    p = tmp_path / 'session.jsonl'
    _write(p, [
        {'record_type': 'gps', 't': 10, 'lat': 48.0, 'lon': 2.0, 'fix': 3},
        {'id': 'aa:bb:cc:dd:ee:ff', 't': 11, 'lat': 48.0001, 'lon': 2.0001, 'rssi': -50},
        {'id': 'missing-gps', 't': 12, 'rssi': -80},
        '{bad json',
    ])

    r = load_session(p)
    assert r.total_lines == 4
    assert r.parsed_records == 3
    assert r.bad_json == 1
    assert len(r.geotagged) == 2
    assert len(r.geotagged_gps) == 1
    assert len(r.geotagged_obs) == 1
    text = format_report(r)
    assert 'bad_json=1' in text
    assert 'gps=1' in text
    assert any(i.severity == 'ERROR' for i in r.issues)


def test_session_sanity_detects_stale_tail(tmp_path):
    p = tmp_path / 'stale.jsonl'
    rows = []
    for i in range(10):
        rows.append({'id': 'ap', 't': i, 'lat': 48.0 + i * 0.00001, 'lon': 2.0})
    for i in range(10, 40):
        rows.append({'id': 'ap', 't': i, 'lat': 48.0001, 'lon': 2.0})
    _write(p, rows)

    r = load_session(p)
    stale = stale_runs(r.geotagged, min_count=10)
    assert stale
    assert max(run['count'] for run in stale) >= 30
    assert any('stale' in issue.message for issue in r.issues)


def test_session_sanity_outputs_files(tmp_path):
    p = tmp_path / 'session.jsonl'
    _write(p, [
        {'record_type': 'gps', 't': 10, 'lat': 48.0, 'lon': 2.0},
        {'id': 'ap', 't': 11, 'lat': 48.001, 'lon': 2.001, 'rssi': -55},
    ])
    r = load_session(p)
    html = tmp_path / 'out.html'
    geojson = tmp_path / 'out.geojson'
    csv = tmp_path / 'out.csv'
    write_html(r, html)
    write_geojson(r, geojson)
    write_csv(r, csv)
    assert 'every valid geotagged' in html.read_text()
    assert 'FeatureCollection' in geojson.read_text()
    assert 'line,index,t,kind,id,lat,lon,alt,rssi' in csv.read_text().splitlines()[0]


def test_session_sanity_cli_wrapper(tmp_path):
    p = tmp_path / 'session.jsonl'
    _write(p, [{'record_type': 'gps', 't': 10, 'lat': 48.0, 'lon': 2.0}])
    html = tmp_path / 'map.html'
    root = Path(__file__).resolve().parents[1]
    res = subprocess.run(
        [sys.executable, str(root / 'tools' / 'session_sanity.py'), str(p), '--html', str(html)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    assert res.returncode == 0, res.stderr
    assert 'Session sanity:' in res.stdout
    assert html.exists()


def test_route_gaps_reports_large_time_gap(tmp_path):
    p = tmp_path / 'gap.jsonl'
    _write(p, [
        {'id': 'ap', 't': 1, 'lat': 48.0, 'lon': 2.0},
        {'id': 'ap', 't': 99, 'lat': 48.0001, 'lon': 2.0001},
    ])
    r = load_session(p)
    gaps = route_gaps(r.geotagged, min_gap_s=10)
    assert gaps
    assert gaps[0]['dt_s'] == 98
