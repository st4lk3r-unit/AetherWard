"""Tests for the web server: API endpoints, ANSI→HTML, hardware detect, config CRUD."""
from __future__ import annotations

import json
import sys
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

import pytest

# Make sure cli package is importable
sys.path.insert(0, str(Path(__file__).parent.parent))
from cli.web import (
    _ansi_to_html, _detect_hardware, _ThreadedHTTPServer, _Handler, AW_SESSIONS,
    _positions, _state_lock, _merge_source_meta, _session_record_to_map_row,
    _source_sample_rows,
)


# ── _ansi_to_html ─────────────────────────────────────────────────────────────

class TestAnsiToHtml:
    def test_plain_text_unchanged(self):
        assert _ansi_to_html('hello') == 'hello'

    def test_html_special_chars_escaped(self):
        result = _ansi_to_html('<b>&amp;</b>')
        assert '&lt;b&gt;' in result
        assert '&amp;amp;' in result

    def test_24bit_color_converted(self):
        # \033[38;2;255;28;28m = rgb(255,28,28)
        text = '\033[38;2;255;28;28mRED\033[0m'
        result = _ansi_to_html(text)
        assert 'rgb(255,28,28)' in result
        assert 'RED' in result
        assert '</span>' in result

    def test_reset_closes_spans(self):
        text = '\033[38;2;0;255;0mGREEN\033[0mplain'
        result = _ansi_to_html(text)
        assert result.endswith('plain')
        assert result.count('<span') == result.count('</span')

    def test_transparent_color_for_ansi_30(self):
        text = '\033[30mhidden\033[0m'
        result = _ansi_to_html(text)
        assert 'transparent' in result

    def test_nested_spans_balanced(self):
        text = '\033[38;2;100;100;100mA\033[38;2;200;200;200mB\033[0m'
        result = _ansi_to_html(text)
        assert result.count('<span') == result.count('</span')

    def test_empty_string(self):
        assert _ansi_to_html('') == ''

    def test_no_ansi_passthrough(self):
        s = 'AetherWard v0.1.0 — RF observation framework'
        assert _ansi_to_html(s) == s


# ── _detect_hardware ──────────────────────────────────────────────────────────

class TestDetectHardware:
    def test_returns_expected_keys(self):
        d = _detect_hardware()
        assert 'wifi_ifaces' in d
        assert 'gpsd' in d
        assert 'rtlsdr' in d
        assert 'c_core' in d
        assert 'pps' in d

    def test_wifi_ifaces_is_list(self):
        d = _detect_hardware()
        assert isinstance(d['wifi_ifaces'], list)

    def test_boolean_fields(self):
        d = _detect_hardware()
        assert isinstance(d['gpsd'], bool)
        assert isinstance(d['rtlsdr'], bool)
        assert isinstance(d['c_core'], bool)
        assert isinstance(d['pps'], bool)

    def test_does_not_raise(self):
        # Must complete without exception even in CI / VM environments
        _detect_hardware()


# ── Live server fixture ───────────────────────────────────────────────────────

@pytest.fixture(scope='module')
def server():
    """Start a live web server on an ephemeral port for API tests."""
    import socket as _s
    # Find a free port
    with _s.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]

    srv = _ThreadedHTTPServer(('127.0.0.1', port), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield port
    srv.shutdown()


def _get(port, path) -> tuple[int, bytes]:
    conn = HTTPConnection('127.0.0.1', port, timeout=5)
    conn.request('GET', path)
    r = conn.getresponse()
    return r.status, r.read()


def _post(port, path, data: dict) -> tuple[int, dict]:
    body = json.dumps(data).encode()
    conn = HTTPConnection('127.0.0.1', port, timeout=5)
    conn.request('POST', path, body=body, headers={'Content-Type': 'application/json'})
    r = conn.getresponse()
    return r.status, json.loads(r.read())


# ── GET endpoints ─────────────────────────────────────────────────────────────

class TestGetEndpoints:
    def test_root_returns_html(self, server):
        status, body = _get(server, '/')
        assert status == 200
        assert b'<!DOCTYPE html>' in body

    def test_status_endpoint(self, server):
        status, body = _get(server, '/api/status')
        assert status == 200
        d = json.loads(body)
        assert 'version' in d
        assert 'solve_running' in d
        assert 'run_running' in d

    def test_sessions_endpoint_returns_list(self, server):
        status, body = _get(server, '/api/sessions')
        assert status == 200
        assert isinstance(json.loads(body), list)

    def test_configs_endpoint_returns_list(self, server):
        status, body = _get(server, '/api/configs')
        assert status == 200
        assert isinstance(json.loads(body), list)

    def test_detect_endpoint(self, server):
        status, body = _get(server, '/api/detect')
        assert status == 200
        d = json.loads(body)
        assert 'wifi_ifaces' in d
        assert 'gpsd' in d

    def test_positions_all_endpoint(self, server):
        status, body = _get(server, '/api/positions/all')
        assert status == 200
        assert isinstance(json.loads(body), list)

    def test_session_records_exposes_gps_breadcrumbs(self, server):
        AW_SESSIONS.mkdir(parents=True, exist_ok=True)
        path = AW_SESSIONS / f'test-gps-path-{int(time.time())}.jsonl'
        path.write_text('\n'.join([
            json.dumps({'record_type': 'gps', 't': 1_700_000_000, 'lat': 48.0, 'lon': 2.0, 'fix': 2}),
            json.dumps({'id': 'aa:bb:cc:dd:ee:ff', 't': 1_700_000_001, 'lat': 48.1, 'lon': 2.1,
                        'freq': 2_437_000_000, 'rssi': -50, 'metadata': {'ssid': 'AP'}}),
        ]) + '\n')
        try:
            status, body = _get(server, '/api/session/records?path=' + str(path))
            assert status == 200
            rows = json.loads(body)
            assert rows[0]['record_type'] == 'gps'
            assert rows[0]['id'] == 'GPS track'
            assert rows[1]['record_type'] == 'observation'
            assert rows[1]['id'] == 'aa:bb:cc:dd:ee:ff'
        finally:
            path.unlink(missing_ok=True)


    def test_solver_defaults_to_finite_and_returns_idle(self, server):
        AW_SESSIONS.mkdir(parents=True, exist_ok=True)
        with _state_lock:
            _positions.clear()
        path = AW_SESSIONS / f'test-solve-finite-default-{int(time.time())}.jsonl'
        obs = [
            {'record_type': 'observation', 'id': '11:22:33:44:55:66', 't': 1_700_000_001,
             'lat': 48.0000, 'lon': 2.0000, 'freq': 2_437_000_000, 'rssi': -70},
            {'record_type': 'observation', 'id': '11:22:33:44:55:66', 't': 1_700_000_002,
             'lat': 48.0001, 'lon': 2.0001, 'freq': 2_437_000_000, 'rssi': -60},
            {'record_type': 'observation', 'id': '11:22:33:44:55:66', 't': 1_700_000_003,
             'lat': 48.0002, 'lon': 2.0002, 'freq': 2_437_000_000, 'rssi': -50},
        ]
        path.write_text('\n'.join(json.dumps(r) for r in obs) + '\n')
        try:
            status, d = _post(server, '/api/solve/start', {
                'session': str(path), 'config': None, 'n_exp': 2.5,
                'min_obs': 3,
            })
            assert status == 200
            deadline = time.time() + 6.0
            stat = {}
            while time.time() < deadline:
                status2, body = _get(server, '/api/status')
                assert status2 == 200
                stat = json.loads(body)
                if not stat.get('solve_running'):
                    break
                time.sleep(0.1)
            assert stat.get('solve_running') is False
            assert stat.get('solve_mode') == 'finite'
        finally:
            _post(server, '/api/solve/stop', {})
            path.unlink(missing_ok=True)

    def test_solver_skips_gps_breadcrumbs_and_solves_only_observations(self, server):
        AW_SESSIONS.mkdir(parents=True, exist_ok=True)
        with _state_lock:
            _positions.clear()
        path = AW_SESSIONS / f'test-solve-gps-skip-{int(time.time())}.jsonl'
        obs = [
            {'record_type': 'gps', 't': 1_700_000_000, 'lat': 48.0, 'lon': 2.0, 'fix': 2},
            {'record_type': 'observation', 'id': 'aa:bb:cc:dd:ee:ff', 't': 1_700_000_001,
             'lat': 48.0000, 'lon': 2.0000, 'freq': 2_437_000_000, 'rssi': -70,
             'metadata': {'ssid': 'AP'}},
            {'record_type': 'observation', 'id': 'aa:bb:cc:dd:ee:ff', 't': 1_700_000_002,
             'lat': 48.0001, 'lon': 2.0001, 'freq': 2_437_000_000, 'rssi': -60,
             'metadata': {'ssid': 'AP'}},
            {'record_type': 'observation', 'id': 'aa:bb:cc:dd:ee:ff', 't': 1_700_000_003,
             'lat': 48.0002, 'lon': 2.0002, 'freq': 2_437_000_000, 'rssi': -50,
             'metadata': {'ssid': 'AP'}},
        ]
        path.write_text('\n'.join(json.dumps(r) for r in obs) + '\n')
        try:
            status, d = _post(server, '/api/solve/start', {
                'session': str(path), 'config': None, 'n_exp': 2.5,
                'min_obs': 3, 'follow': False,
            })
            assert status == 200
            deadline = time.time() + 5.0
            rows = []
            while time.time() < deadline:
                status2, body = _get(server, '/api/positions/all')
                assert status2 == 200
                rows = json.loads(body)
                if any(r.get('id') == 'aa:bb:cc:dd:ee:ff' for r in rows):
                    break
                time.sleep(0.1)
            ids = {r.get('id') for r in rows}
            assert 'aa:bb:cc:dd:ee:ff' in ids
            assert 'anon:0' not in ids
            assert 'GPS track' not in ids
        finally:
            _post(server, '/api/solve/stop', {})
            path.unlink(missing_ok=True)

    def test_unknown_endpoint_404(self, server):
        status, _ = _get(server, '/api/nonexistent')
        assert status == 404

    def test_config_raw_missing_name(self, server):
        status, body = _get(server, '/api/config/raw')
        assert status == 400

    def test_config_raw_invalid_name(self, server):
        status, body = _get(server, '/api/config/raw?name=../etc/passwd')
        assert status == 400


# ── Config CRUD ───────────────────────────────────────────────────────────────

class TestConfigCrud:
    TOML = """\
mode = "wardriver"
array_id = "test-web"
[[antennas]]
id = "wlan0"
backend = "null"
[gps]
backend = "none"
[sync]
source = "software"
[mode_config]
channels = [1, 6, 11]
hop_interval = 0.1
output_path = "/tmp/test.jsonl"
[output]
format = "jsonl"
path = "/tmp/test.jsonl"
"""

    def test_save_and_load_config(self, server):
        name = f'test-crud-{int(time.time())}'
        # Save
        status, d = _post(server, '/api/config/save', {'name': name, 'content': self.TOML})
        assert status == 200
        assert d.get('ok')
        # Raw load
        status2, body = _get(server, f'/api/config/raw?name={name}')
        assert status2 == 200
        result = json.loads(body)
        assert result['name'] == name
        assert 'wardriver' in result['content']
        # Cleanup
        _post(server, '/api/config/delete', {'name': name})

    def test_delete_config(self, server):
        name = f'test-del-{int(time.time())}'
        _post(server, '/api/config/save', {'name': name, 'content': self.TOML})
        status, d = _post(server, '/api/config/delete', {'name': name})
        assert status == 200
        assert d.get('ok')
        # Verify gone
        status2, _ = _get(server, f'/api/config/raw?name={name}')
        assert status2 == 404

    def test_save_invalid_name_rejected(self, server):
        status, d = _post(server, '/api/config/save', {'name': '../evil', 'content': 'x'})
        assert status == 400

    def test_save_empty_content_rejected(self, server):
        status, d = _post(server, '/api/config/save', {'name': 'good-name', 'content': '   '})
        assert status == 400

    def test_delete_nonexistent_404(self, server):
        status, d = _post(server, '/api/config/delete', {'name': 'no-such-config-xyz'})
        assert status == 404


# ── Source (position) CRUD ────────────────────────────────────────────────────

class TestSourceCrud:
    def test_add_source(self, server):
        rec = {'id': 'test:mac:01', 'lat': 48.8566, 'lon': 2.3522, 'pos_method': 'manual'}
        status, d = _post(server, '/api/source/add', rec)
        assert status == 200
        assert d.get('ok')

    def test_edit_source(self, server):
        rec = {'id': 'test:mac:02', 'lat': 48.0, 'lon': 2.0}
        _post(server, '/api/source/add', rec)
        rec['lat'] = 49.0
        status, d = _post(server, '/api/source/edit', rec)
        assert status == 200

    def test_delete_source(self, server):
        _post(server, '/api/source/add', {'id': 'test:del:03', 'lat': 1.0, 'lon': 1.0})
        status, d = _post(server, '/api/source/delete', {'id': 'test:del:03'})
        assert status == 200
        assert d.get('ok')

    def test_add_source_missing_id(self, server):
        status, d = _post(server, '/api/source/add', {'lat': 48.0, 'lon': 2.0})
        assert status == 400


# ── Solve start/stop ──────────────────────────────────────────────────────────

class TestSolveApi:
    def test_start_with_missing_session(self, server):
        status, d = _post(server, '/api/solve/start', {'session': '/tmp/nonexistent.jsonl'})
        assert status == 400

    def test_stop_always_ok(self, server):
        status, d = _post(server, '/api/solve/stop', {})
        assert status == 200

    def test_run_stop_always_ok(self, server):
        status, d = _post(server, '/api/run/stop', {})
        assert status == 200

    def test_run_start_requires_config(self, server):
        status, d = _post(server, '/api/run/start', {'config': ''})
        assert status == 400

class TestWebFatSessionRegressions:
    def test_session_records_map_mode_decimates_fat_paths(self, server):
        AW_SESSIONS.mkdir(parents=True, exist_ok=True)
        path = AW_SESSIONS / f'test-map-decimate-{int(time.time())}.jsonl'
        rows = []
        for i in range(300):
            rows.append({'record_type': 'gps', 't': 1_700_000_000 + i,
                         'lat': 48.0 + i * 0.00001, 'lon': 2.0, 'fix': 2})
            rows.append({'record_type': 'observation', 'id': 'aa:bb:cc:dd:ee:ff',
                         't': 1_700_000_000 + i, 'lat': 48.0 + i * 0.00001,
                         'lon': 2.0, 'freq': 2_437_000_000, 'rssi': -60 - (i % 4)})
        path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
        try:
            status, body = _get(server, '/api/session/records?map=1&max_gps=100&max_obs=120&path=' + str(path))
            assert status == 200
            got = json.loads(body)
            gps = [r for r in got if r.get('record_type') == 'gps']
            obs = [r for r in got if r.get('record_type') == 'observation']
            # First/last are kept, so the count is allowed to be cap+1-ish
            assert len(gps) <= 101
            assert len(obs) <= 121
            assert gps[0]['t'] == rows[0]['t']
            assert gps[-1]['t'] == rows[-2]['t']
            assert obs[0]['t'] == rows[1]['t']
            assert obs[-1]['t'] == rows[-1]['t']
        finally:
            path.unlink(missing_ok=True)

    def test_session_records_overview_is_route_only_and_bounded(self, server):
        AW_SESSIONS.mkdir(parents=True, exist_ok=True)
        path = AW_SESSIONS / f'test-map-overview-{int(time.time())}.jsonl'
        rows = []
        for i in range(240):
            rows.append({'record_type': 'gps', 't': 1_700_010_000 + i,
                         'lat': 48.0 + i * 0.00001, 'lon': 2.0, 'fix': 2})
            rows.append({'record_type': 'observation', 'id': 'aa:bb:cc:dd:ee:ff',
                         't': 1_700_010_000 + i, 'lat': 48.0 + i * 0.00001,
                         'lon': 2.0, 'freq': 2_437_000_000, 'rssi': -60})
        path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
        try:
            status, body = _get(server, '/api/session/records?map=1&overview=1&max_points=60&path=' + str(path))
            assert status == 200
            got = json.loads(body)
            assert 2 <= len(got) <= 61
            assert all(r.get('overview') is True for r in got)
            assert all(r.get('record_type') == 'gps' for r in got)
            assert got[0]['t'] == rows[0]['t']
            assert got[-1]['t'] == rows[-2]['t']
        finally:
            path.unlink(missing_ok=True)

    def test_session_records_overview_falls_back_to_observation_route(self, server):
        AW_SESSIONS.mkdir(parents=True, exist_ok=True)
        path = AW_SESSIONS / f'test-map-overview-fallback-{int(time.time())}.jsonl'
        rows = [
            {'record_type': 'observation', 'id': 'aa:bb:cc:dd:ee:ff', 't': 1_700_020_000 + i,
             'lat': 48.0 + i * 0.00001, 'lon': 2.0, 'freq': 2_437_000_000, 'rssi': -60}
            for i in range(100)
        ]
        path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
        try:
            status, body = _get(server, '/api/session/records?map=1&overview=1&max_points=25&path=' + str(path))
            assert status == 200
            got = json.loads(body)
            assert 2 <= len(got) <= 26
            assert all(r.get('overview') is True for r in got)
            assert all(r.get('record_type') == 'route' for r in got)
            assert got[0]['t'] == rows[0]['t']
            assert got[-1]['t'] == rows[-1]['t']
        finally:
            path.unlink(missing_ok=True)

    def test_batch_solve_starts_background_job(self, server):
        status, d = _post(server, '/api/solve/batch', {'max_cells': 32, 'max_sessions': 1})
        assert status == 200
        assert d.get('started') is True
        status2, body = _get(server, '/api/status')
        assert status2 == 200
        stat = json.loads(body)
        assert 'solve_running' in stat
        _post(server, '/api/solve/stop', {})



# ── Map relationship extraction ──────────────────────────────────────────────

class TestMapRelationshipExtraction:
    def test_late_association_metadata_is_merged(self):
        meta = _merge_source_meta({'source_role': 'client', 'ssid': ''}, {'associated': 'AA:BB:CC:DD:EE:FF'})
        assert meta['associated_bssid'] == 'AA:BB:CC:DD:EE:FF'

    def test_map_row_preserves_associated_alias(self):
        row = _session_record_to_map_row({
            'record_type': 'observation',
            'lat': 48.0, 'lon': 2.0, 'rssi': -55,
            'metadata': {
                'identifier': '11:22:33:44:55:66',
                'source_role': 'client',
                'associated': 'AA:BB:CC:DD:EE:FF',
            },
        })
        assert row is not None
        assert row['id'] == '11:22:33:44:55:66'
        assert row['source_role'] == 'client'
        assert row['associated_bssid'] == 'AA:BB:CC:DD:EE:FF'


    def test_self_bssid_does_not_become_linked_ap(self):
        row = _session_record_to_map_row({
            'record_type': 'observation',
            'lat': 48.0, 'lon': 2.0, 'rssi': -55,
            'metadata': {
                'identifier': '02:00:00:25:75:50',
                'source_role': 'client',
                'bssid': '02:00:00:25:75:50',
                'frame_subtype': 'qos_null',
            },
        })
        assert row is not None
        assert row['id'] == '02:00:00:25:75:50'
        assert row['source_role'] == 'client'
        assert row.get('associated_bssid') in (None, '')

    def test_source_sample_rows_match_associated_ap(self):
        path = AW_SESSIONS / f'test-source-samples-{int(time.time())}.jsonl'
        rows = [
            {'record_type': 'observation', 'lat': 48.0, 'lon': 2.0, 'rssi': -60,
             'metadata': {'identifier': '11:22:33:44:55:66', 'source_role': 'client',
                          'associated_bssid': 'AA:BB:CC:DD:EE:FF'}},
            {'record_type': 'observation', 'lat': 48.1, 'lon': 2.1, 'rssi': -61,
             'metadata': {'identifier': '77:88:99:aa:bb:cc', 'source_role': 'client',
                          'associated_bssid': 'AA:BB:CC:DD:EE:FF'}},
        ]
        path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
        try:
            got = _source_sample_rows(path, 'AA:BB:CC:DD:EE:FF', role='ap', max_obs=100)
            assert len(got) == 2
            assert all(r.get('source_sample') is True for r in got)
        finally:
            path.unlink(missing_ok=True)

    def test_defaultssid_is_treated_as_empty_ssid(self):
        meta = _merge_source_meta({}, {'ssid': 'defaultSSID', 'identifier': 'aa:bb:cc:dd:ee:ff'})
        assert meta.get('ssid') == ''

    def test_browser_relation_aliases_are_present(self):
        js = Path(__file__).parent.parent.joinpath('cli', '_html_js.py').read_text()
        assert 'associated_bssid' in js
        assert 'associated' in js
        assert 'ap_mac' in js
        assert 'linked_station' in js

    def test_selected_sample_links_use_path_colors(self):
        js = Path(__file__).parent.parent.joinpath('cli', '_html_js.py').read_text()
        assert 'function _baseSamplePath(path)' in js
        assert 'function _colorForSamplePath(path)' in js
        assert "split('#')[0]" in js
        assert "const color=_colorForSamplePath(s.path);" in js
        assert "color,weight:1.6" in js
        assert "fillColor:color" in js

    def test_source_samples_match_bssid_for_bulk_search_hydration(self, server, tmp_path):
        import cli.web as web
        path = web.AW_SESSIONS / 'sample-search.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {'t': 1.0, 'id': 'aa:bb:cc:00:11:22', 'bssid': 'aa:bb:cc:00:11:22',
             'ssid': 'TestNet', 'lat': 48.0, 'lon': 2.0, 'rssi': -65, 'freq': 2412000000,
             'metadata': {'source_role': 'ap'}},
            {'t': 2.0, 'id': 'aa:bb:cc:00:11:22', 'bssid': 'aa:bb:cc:00:11:22',
             'ssid': 'TestNet', 'lat': 48.0002, 'lon': 2.0002, 'rssi': -60, 'freq': 2412000000,
             'metadata': {'source_role': 'ap'}},
        ]
        path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
        status, raw = _get(server, '/api/session/source_samples?path=' + str(path) + '&source=aa:bb:cc:00:11:22&max_obs=50')
        assert status == 200
        body = json.loads(raw)
        assert len(body) == 2
        assert body[0]['id'] == 'aa:bb:cc:00:11:22'

    def test_batch_solve_accepts_min_obs_and_n_exp(self, server):
        status, d = _post(server, '/api/solve/batch', {'max_cells': 32, 'max_sessions': 1, 'min_obs': 1, 'n_exp': 2.1})
        assert status == 200
        assert d['min_obs'] == 1
        assert abs(d['n_exp'] - 2.1) < 1e-9
        _post(server, '/api/solve/stop', {})

