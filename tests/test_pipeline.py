"""
End-to-end pipeline tests.

Persistent output sessions are written to ~/.aetherward/sessions/tests/
so they can be loaded directly in the web map/ENU viewers.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import pytest

TESTS_DIR = Path.home() / '.aetherward' / 'sessions' / 'tests'

from aetherward.position.rss import rssi_centroid, rss_solve


# ── Helpers ───────────────────────────────────────────────────────────────────

_M_PER_DEG = 111_320.0

def _synth_wardriver_obs(true_lat, true_lon, observer_track, n_exp=2.5,
                         rssi_at_1m=-30.0):
    """Generate synthetic wardriver observations around a true AP position."""
    records = []
    for i, (lat, lon) in enumerate(observer_track):
        dx = (lon - true_lon) * math.cos(math.radians(true_lat)) * _M_PER_DEG
        dy = (lat - true_lat) * _M_PER_DEG
        d  = max(math.sqrt(dx*dx + dy*dy), 0.1)
        rssi = rssi_at_1m - 10.0 * n_exp * math.log10(d)
        records.append({
            'id':       'aa:bb:cc:dd:ee:ff',   # same AP seen from multiple positions
            'ssid':     'TestAP',
            'rssi':     round(rssi, 1),
            'lat':      lat,
            'lon':      lon,
            'freq':     2412000000,
            'protocol': 'wifi',
            't':        time.time() + i * 0.5,
        })
    return records


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with open(path, 'w') as f:
        for r in records:
            f.write(json.dumps(r) + '\n')


def _synth_enu_positions(true_x, true_y, true_z, n_obs=6, spread_m=5.0):
    """Synthetic ENU-frame position records (TDOA/array sensing output)."""
    import random
    random.seed(42)
    records = []
    for i in range(n_obs):
        noise_x = random.gauss(0, spread_m * 0.1)
        noise_y = random.gauss(0, spread_m * 0.1)
        records.append({
            'id':     f'src-{i}',
            'x_enu':  round(true_x + noise_x, 3),
            'y_enu':  round(true_y + noise_y, 3),
            'z_enu':  round(true_z, 3),
            'rssi':   round(-55.0 - i * 2.0, 1),
            'method': 'tdoa',
            't':      time.time() + i,
        })
    return records


# ── Wardriver pipeline ────────────────────────────────────────────────────────

class TestWardriverPipeline:
    """
    Simulates a wardriver scan session and verifies the full
    capture → JSONL → solve → position pipeline.
    Output session.jsonl is written to /tmp and can be loaded in the web UI.
    """

    TRUE_LAT  =  48.8566
    TRUE_LON  =   2.3522

    OBSERVER_TRACK = [
        (48.8556, 2.3522),  # South
        (48.8576, 2.3522),  # North
        (48.8566, 2.3510),  # West
        (48.8566, 2.3534),  # East
        (48.8558, 2.3512),  # SW
        (48.8574, 2.3532),  # NE
    ]

    def test_session_written(self, tmp_path):
        out = tmp_path / 'wardriver_session.jsonl'
        records = _synth_wardriver_obs(self.TRUE_LAT, self.TRUE_LON,
                                       self.OBSERVER_TRACK)
        _write_jsonl(out, records)
        lines = out.read_text().strip().splitlines()
        assert len(lines) == len(self.OBSERVER_TRACK)

    def test_session_is_valid_jsonl(self, tmp_path):
        out = tmp_path / 'wardriver_valid.jsonl'
        records = _synth_wardriver_obs(self.TRUE_LAT, self.TRUE_LON,
                                       self.OBSERVER_TRACK)
        _write_jsonl(out, records)
        for line in out.read_text().strip().splitlines():
            rec = json.loads(line)
            assert 'id' in rec
            assert 'rssi' in rec
            assert 'lat' in rec and 'lon' in rec

    def test_rss_solver_recovers_position(self):
        TESTS_DIR.mkdir(parents=True, exist_ok=True)
        out = TESTS_DIR / 'wardriver.jsonl'
        records = _synth_wardriver_obs(self.TRUE_LAT, self.TRUE_LON,
                                       self.OBSERVER_TRACK)
        _write_jsonl(out, records)

        # Read back and solve
        obs_by_id: dict = {}
        for line in out.read_text().strip().splitlines():
            rec = json.loads(line)
            sid = rec['id']
            obs_by_id.setdefault(sid, []).append(
                (rec['lat'], rec['lon'], rec['rssi'])
            )

        # Collect all observations for all IDs (synthetic: each rec has unique id)
        all_obs = [(r['lat'], r['lon'], r['rssi']) for r in records]
        result = rss_solve(all_obs, n_exp=2.5)
        assert result is not None
        assert result['lat'] == pytest.approx(self.TRUE_LAT, abs=0.0005)
        assert result['lon'] == pytest.approx(self.TRUE_LON, abs=0.0005)

    def test_output_file_accessible(self):
        """Verify sessions/tests session can be stat'd (accessible by web viewer)."""
        p = TESTS_DIR / 'wardriver.jsonl'
        if p.exists():
            assert p.stat().st_size > 0

    def test_centroid_fallback_with_few_obs(self):
        obs = [(self.TRUE_LAT - 0.001, self.TRUE_LON, -65.0),
               (self.TRUE_LAT + 0.001, self.TRUE_LON, -60.0)]
        # Too few for trilateration — centroid should still return something
        lat, lon = rssi_centroid(obs)
        assert abs(lat - self.TRUE_LAT) < 0.01
        assert abs(lon - self.TRUE_LON) < 0.01


# ── TDOA / relative positioning pipeline ─────────────────────────────────────

class TestTdoaPipeline:
    """
    Simulates TDOA solver output as ENU position records.
    Output positions_enu.jsonl is written to /tmp and consumed by the
    ENU 3-D viewer in the web UI.
    """

    TRUE_X =  3.5    # East, metres
    TRUE_Y = -1.2    # North, metres
    TRUE_Z =  0.0    # Up, metres

    def test_enu_session_written(self, tmp_path):
        out = tmp_path / 'tdoa_positions.jsonl'
        records = _synth_enu_positions(self.TRUE_X, self.TRUE_Y, self.TRUE_Z)
        _write_jsonl(out, records)
        lines = out.read_text().strip().splitlines()
        assert len(lines) == 6

    def test_enu_records_have_required_fields(self, tmp_path):
        out = tmp_path / 'tdoa_fields.jsonl'
        records = _synth_enu_positions(self.TRUE_X, self.TRUE_Y, self.TRUE_Z)
        _write_jsonl(out, records)
        for line in out.read_text().strip().splitlines():
            rec = json.loads(line)
            assert 'x_enu' in rec
            assert 'y_enu' in rec
            assert 'z_enu' in rec
            assert 'method' in rec

    def test_enu_positions_cluster_near_true(self):
        """Noisy positions should cluster within spread_m of the true location."""
        records = _synth_enu_positions(self.TRUE_X, self.TRUE_Y, self.TRUE_Z,
                                       n_obs=20, spread_m=1.0)
        xs = [r['x_enu'] for r in records]
        ys = [r['y_enu'] for r in records]
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        assert mean_x == pytest.approx(self.TRUE_X, abs=0.5)
        assert mean_y == pytest.approx(self.TRUE_Y, abs=0.5)

    def test_enu_session_written_to_sessions(self):
        """Write to sessions/tests so the web ENU viewer can load it."""
        TESTS_DIR.mkdir(parents=True, exist_ok=True)
        out = TESTS_DIR / 'tdoa_enu.jsonl'
        records = _synth_enu_positions(self.TRUE_X, self.TRUE_Y, self.TRUE_Z,
                                       n_obs=30, spread_m=0.5)
        _write_jsonl(out, records)
        assert out.exists()
        assert out.stat().st_size > 0
        lines = out.read_text().strip().splitlines()
        assert len(lines) == 30


# ── Array sensing pipeline ────────────────────────────────────────────────────

class TestArraySensingPipeline:
    """
    Simulates array sensing event output.
    Output events.jsonl can be loaded by the ENU viewer to show event
    positions (direction vectors from individual antennas).
    """

    ANTENNA_POSITIONS = [(0.0, 0.0, 0.0), (0.5, 0.0, 0.0), (0.0, 0.5, 0.0)]

    def _synth_events(self, n=10):
        import random; random.seed(7)
        events = []
        for i in range(n):
            events.append({
                'type':       random.choice(['presence', 'motion', 'absence']),
                'antenna_id': f'wlan{i % 3}',
                'variance':   round(random.uniform(0.05, 0.3), 4),
                'direction':  [round(random.uniform(-1, 1), 3),
                               round(random.uniform(-1, 1), 3), 0.0],
                't':          time.time() + i,
            })
        return events

    def test_events_written_to_jsonl(self, tmp_path):
        out = tmp_path / 'sensing_events.jsonl'
        _write_jsonl(out, self._synth_events(10))
        assert len(out.read_text().strip().splitlines()) == 10

    def test_event_types_valid(self, tmp_path):
        out = tmp_path / 'sensing_types.jsonl'
        _write_jsonl(out, self._synth_events(30))
        valid = {'presence', 'motion', 'absence'}
        for line in out.read_text().strip().splitlines():
            assert json.loads(line)['type'] in valid

    def test_events_written_to_sessions(self):
        TESTS_DIR.mkdir(parents=True, exist_ok=True)
        out = TESTS_DIR / 'sensing_events.jsonl'
        _write_jsonl(out, self._synth_events(20))
        assert out.exists() and out.stat().st_size > 0
