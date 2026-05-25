"""
Session file generation tests.

Writes realistic JSONL session files to ~/.aetherward/sessions/tests/ organised
in subfolders by type.  These files can be loaded directly in the web map and
ENU viewers to showcase every session variant the UI supports.

Tree written:
  tests/wardriver/
      paris_2ghz.jsonl          — Paris, 5 APs, 2.4 GHz, circular track
      london_5ghz.jsonl         — London, 4 APs, 5 GHz, circular track
      newyork_mixed.jsonl       — NYC, 8 APs, mixed 2.4 + 5 GHz, grid track
      rural_sparse.jsonl        — Rural France, 2 APs, wide spread
      urban_dense.jsonl         — Generic urban, 15 APs, tight cluster
      driving_route.jsonl       — Linear driving route, 6 APs at intervals
      single_ap_trilaterate.jsonl — One AP, many observer positions
  tests/tdoa/
      indoor_room.jsonl         — 2 m × 3 m room, low noise
      outdoor_courtyard.jsonl   — 15 m × 15 m, medium spread
      corridor.jsonl            — Long narrow corridor (20 m × 2 m)
      high_noise.jsonl          — High variance, many observations
      multi_source.jsonl        — 4 distinct ENU sources
      z_elevated.jsonl          — Sources at non-zero height (drone/ceiling)
  tests/sensing/
      busy_room.jsonl           — Frequent presence + motion, 2 antennas
      empty_corridor.jsonl      — Mostly absence events
      three_antenna_array.jsonl — 3-antenna array with direction vectors
      long_session.jsonl        — 150-event time series
      motion_sequence.jsonl     — Presence → motion → absence cycle
      multi_variance.jsonl      — Varying variance levels per antenna
"""
from __future__ import annotations

import json
import math
import random
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.showcase

TESTS_DIR   = Path.home() / '.aetherward' / 'sessions' / 'tests'
DIR_WD      = TESTS_DIR / 'wardriver'
DIR_TDOA    = TESTS_DIR / 'tdoa'
DIR_SENSING = TESTS_DIR / 'sensing'

_M_PER_DEG = 111_320.0
_NOW        = time.time()

# Standard non-collinear 4-antenna array used in all TDOA sessions
# so the ENU viewer can show receiver geometry alongside position estimates.
_TDOA_ARRAY = [
    (0.0, 0.0, 0.0),  # rx-0 reference
    (2.0, 0.0, 0.0),  # rx-1 East
    (1.0, 2.0, 0.0),  # rx-2 North-East
    (0.0, 1.5, 1.5),  # rx-3 North-Elevated
]


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w') as f:
        for r in records:
            f.write(json.dumps(r) + '\n')


def _antenna_records(positions: list = None, t: float = None) -> list[dict]:
    """Emit method='antenna' marker records so the ENU viewer shows receiver geometry."""
    positions = positions or _TDOA_ARRAY
    t = t or _NOW
    return [
        {'id': f'rx-{i}', 'x_enu': float(x), 'y_enu': float(y), 'z_enu': float(z),
         'rssi': 0.0, 'method': 'antenna', 't': round(t, 3)}
        for i, (x, y, z) in enumerate(positions)
    ]


def _rssi(true_lat, true_lon, obs_lat, obs_lon, n_exp=2.5, rssi_at_1m=-30.0,
          seed_jitter=0.0) -> float:
    dx = (obs_lon - true_lon) * math.cos(math.radians(true_lat)) * _M_PER_DEG
    dy = (obs_lat - true_lat) * _M_PER_DEG
    d  = max(math.sqrt(dx * dx + dy * dy), 0.1)
    base = rssi_at_1m - 10.0 * n_exp * math.log10(d)
    return round(base + seed_jitter, 1)


def _circle_track(center_lat, center_lon, radius_deg, n_points):
    """Observer positions on a circle around a point."""
    return [
        (center_lat + radius_deg * math.sin(2 * math.pi * i / n_points),
         center_lon + radius_deg * math.cos(2 * math.pi * i / n_points))
        for i in range(n_points)
    ]


def _grid_track(center_lat, center_lon, span_deg, steps):
    """Observer positions on a grid."""
    half = span_deg / 2
    pts  = []
    for iy in range(steps):
        for ix in range(steps):
            pts.append((
                center_lat - half + span_deg * iy / (steps - 1),
                center_lon - half + span_deg * ix / (steps - 1),
            ))
    return pts


def _linear_track(start_lat, start_lon, end_lat, end_lon, n_points):
    return [
        (start_lat + (end_lat - start_lat) * i / (n_points - 1),
         start_lon + (end_lon - start_lon) * i / (n_points - 1))
        for i in range(n_points)
    ]


def _wardriver_obs(aps: list[dict], track, rng: random.Random,
                   freq_hz=2412000000, protocol='wifi') -> list[dict]:
    """
    Build wardriver JSONL records for multiple APs observed from every
    position on *track*.  Each AP dict must have: id, ssid, lat, lon.
    Optional per-AP: freq_hz, n_exp, rssi_at_1m.
    """
    records = []
    t = _NOW
    for pos_idx, (obs_lat, obs_lon) in enumerate(track):
        for ap in aps:
            jitter = rng.gauss(0, 1.5)
            rssi   = _rssi(ap['lat'], ap['lon'], obs_lat, obs_lon,
                           n_exp=ap.get('n_exp', 2.5),
                           rssi_at_1m=ap.get('rssi_at_1m', -30.0),
                           seed_jitter=jitter)
            records.append({
                'id':       ap['id'],
                'ssid':     ap['ssid'],
                'rssi':     rssi,
                'lat':      round(obs_lat, 7),
                'lon':      round(obs_lon, 7),
                'freq':     ap.get('freq_hz', freq_hz),
                'protocol': protocol,
                't':        round(t, 3),
            })
            t += 0.05
    return records


def _enu_cluster(x, y, z, n_obs, spread_m, method, src_id, rng):
    records = []
    for i in range(n_obs):
        records.append({
            'id':     src_id,
            'x_enu':  round(x + rng.gauss(0, spread_m), 3),
            'y_enu':  round(y + rng.gauss(0, spread_m), 3),
            'z_enu':  round(z + rng.gauss(0, spread_m * 0.3), 3),
            'rssi':   round(-55.0 - i * 0.5 + rng.gauss(0, 2), 1),
            'method': method,
            't':      round(_NOW + i * 0.1, 3),
        })
    return records


def _sensing_event(antenna_id, event_type, variance, direction, t) -> dict:
    return {
        'type':       event_type,
        'antenna_id': antenna_id,
        'variance':   round(variance, 4),
        'direction':  [round(d, 3) for d in direction],
        't':          round(t, 3),
    }


# ── AP libraries ──────────────────────────────────────────────────────────────

def _ap(mac, ssid, lat, lon, freq_hz=2412000000, n_exp=2.5, rssi_at_1m=-30.0):
    return dict(id=mac, ssid=ssid, lat=lat, lon=lon,
                freq_hz=freq_hz, n_exp=n_exp, rssi_at_1m=rssi_at_1m)


# ── Wardriver sessions ────────────────────────────────────────────────────────

class TestWardriverSessions:

    def test_paris_2ghz(self):
        """Paris — 5 APs, 2.4 GHz, circular observer track, 12 positions."""
        rng  = random.Random(1)
        clat, clon = 48.8566, 2.3522
        aps  = [
            _ap('aa:bb:cc:01:00:01', 'Freebox-AB12',  clat+0.0002, clon+0.0003),
            _ap('aa:bb:cc:01:00:02', 'SFR-5G-F3A2',   clat-0.0003, clon+0.0001),
            _ap('aa:bb:cc:01:00:03', 'BBoxFibre_2GHz', clat+0.0001, clon-0.0004),
            _ap('aa:bb:cc:01:00:04', 'LIVEBOX-A1B2',   clat-0.0002, clon-0.0002),
            _ap('aa:bb:cc:01:00:05', 'linksys',         clat+0.0004, clon+0.0001),
        ]
        track   = _circle_track(clat, clon, 0.0012, 12)
        records = _wardriver_obs(aps, track, rng)
        out = DIR_WD / 'paris_2ghz.jsonl'
        _write(out, records)
        assert out.stat().st_size > 0
        assert sum(1 for _ in out.open()) == len(aps) * 12

    def test_london_5ghz(self):
        """London — 4 APs, 5 GHz, circular track, 10 positions."""
        rng  = random.Random(2)
        clat, clon = 51.5074, -0.1278
        aps  = [
            _ap('bb:cc:dd:02:00:01', 'BTHub6-A3F1',   clat+0.0003, clon+0.0002,
                freq_hz=5180000000),
            _ap('bb:cc:dd:02:00:02', 'VM-5G-AB12',    clat-0.0002, clon+0.0004,
                freq_hz=5500000000),
            _ap('bb:cc:dd:02:00:03', 'SKY-5GHz-2C9D', clat+0.0001, clon-0.0003,
                freq_hz=5745000000),
            _ap('bb:cc:dd:02:00:04', 'TalkTalk-5G',   clat-0.0004, clon-0.0001,
                freq_hz=5220000000),
        ]
        track   = _circle_track(clat, clon, 0.0015, 10)
        records = _wardriver_obs(aps, track, rng, freq_hz=5180000000)
        out = DIR_WD / 'london_5ghz.jsonl'
        _write(out, records)
        assert out.stat().st_size > 0

    def test_newyork_mixed(self):
        """NYC — 8 APs (mix 2.4 + 5 GHz), 4×4 grid track."""
        rng  = random.Random(3)
        clat, clon = 40.7128, -74.0060
        aps  = [
            _ap('cc:dd:ee:03:00:01', 'NETGEAR-2G',   clat+0.0003, clon+0.0002),
            _ap('cc:dd:ee:03:00:02', 'NETGEAR-5G',   clat+0.0003, clon+0.0002,
                freq_hz=5745000000),
            _ap('cc:dd:ee:03:00:03', 'xfinitywifi',  clat-0.0002, clon+0.0005),
            _ap('cc:dd:ee:03:00:04', 'SpectrumSetup-2.4',
                clat+0.0001, clon-0.0003),
            _ap('cc:dd:ee:03:00:05', 'SpectrumSetup-5',
                clat+0.0001, clon-0.0003, freq_hz=5500000000),
            _ap('cc:dd:ee:03:00:06', 'FiOS-G1100',   clat-0.0004, clon-0.0001),
            _ap('cc:dd:ee:03:00:07', 'ASUS_RT-AX88U',clat+0.0005, clon+0.0003,
                rssi_at_1m=-25.0),
            _ap('cc:dd:ee:03:00:08', 'hidden',        clat-0.0001, clon+0.0002,
                rssi_at_1m=-40.0),
        ]
        track   = _grid_track(clat, clon, 0.002, 4)
        records = _wardriver_obs(aps, track, rng)
        out = DIR_WD / 'newyork_mixed.jsonl'
        _write(out, records)
        assert len(list(out.open())) == len(aps) * len(track)

    def test_rural_sparse(self):
        """Rural France — 2 APs, widely separated, sparse track."""
        rng  = random.Random(4)
        clat, clon = 45.0500, 6.9500
        aps  = [
            _ap('dd:ee:ff:04:00:01', 'Orange-Livebox', clat+0.001, clon+0.002,
                rssi_at_1m=-28.0, n_exp=2.2),
            _ap('dd:ee:ff:04:00:02', 'SFR-4G-Box',    clat-0.003, clon-0.001,
                rssi_at_1m=-35.0, n_exp=3.0),
        ]
        track   = _circle_track(clat, clon, 0.003, 8)
        records = _wardriver_obs(aps, track, rng)
        out = DIR_WD / 'rural_sparse.jsonl'
        _write(out, records)
        assert out.stat().st_size > 0

    def test_urban_dense(self):
        """Urban dense — 15 APs, tight 2.4 GHz cluster, circular track."""
        rng  = random.Random(5)
        clat, clon = 48.8600, 2.3500
        ssids = ['BBOX-{}', 'SFR-{}', 'Free-{}', 'Livebox-{}', 'TP-Link-{}',
                 'Linksys-{}', 'Netgear-{}', 'Orange-{}', 'Bouygues-{}',
                 'Sosh-{}', 'RED-{}', 'Numericable-{}', 'UPC-{}',
                 'Arcadyan-{}', 'hidden']
        aps = []
        for i in range(15):
            dlat = rng.uniform(-0.0008, 0.0008)
            dlon = rng.uniform(-0.0008, 0.0008)
            chan  = rng.choice([1, 6, 11])
            freq  = 2412000000 + (chan - 1) * 5000000
            aps.append(_ap(
                f'ee:ff:00:05:{i:02x}:01',
                ssids[i].format(hex(rng.randint(0x1000, 0xffff))[2:].upper()),
                clat + dlat, clon + dlon,
                freq_hz=freq,
                rssi_at_1m=rng.uniform(-25.0, -35.0),
                n_exp=rng.uniform(2.2, 3.0),
            ))
        track   = _circle_track(clat, clon, 0.0008, 16)
        records = _wardriver_obs(aps, track, rng)
        out = DIR_WD / 'urban_dense.jsonl'
        _write(out, records)
        assert sum(1 for _ in out.open()) == 15 * 16

    def test_driving_route(self):
        """Linear driving route — 6 APs at different points along a 1 km road."""
        rng  = random.Random(6)
        # Road runs roughly east along 48.86° N
        road_start = (48.8600, 2.3300)
        road_end   = (48.8610, 2.3450)   # ~1 km east

        # Each AP sits just off the road at a different longitude
        aps = []
        for i in range(6):
            frac = i / 5
            lat  = road_start[0] + (road_end[0] - road_start[0]) * frac
            lon  = road_start[1] + (road_end[1] - road_start[1]) * frac
            dlat = rng.choice([-1, 1]) * rng.uniform(0.00005, 0.00015)
            aps.append(_ap(
                f'ff:00:11:06:{i:02x}:01',
                f'AP-road-{i+1}',
                lat + dlat, lon,
                rssi_at_1m=-28.0,
                n_exp=2.8,
            ))
        track   = _linear_track(*road_start, *road_end, 20)
        records = _wardriver_obs(aps, track, rng)
        out = DIR_WD / 'driving_route.jsonl'
        _write(out, records)
        assert sum(1 for _ in out.open()) == 6 * 20

    def test_single_ap_many_observers(self):
        """One AP, 30 observer positions — best-case trilateration input."""
        rng  = random.Random(7)
        clat, clon = 48.8566, 2.3522
        aps  = [_ap('00:11:22:07:00:01', 'SingleAP', clat, clon,
                    rssi_at_1m=-30.0, n_exp=2.5)]
        track   = _circle_track(clat, clon, 0.001, 30)
        records = _wardriver_obs(aps, track, rng)
        out = DIR_WD / 'single_ap_trilaterate.jsonl'
        _write(out, records)
        assert sum(1 for _ in out.open()) == 30

    def test_records_have_required_fields(self):
        """Spot-check that every wardriver file has the mandatory fields."""
        required = {'id', 'ssid', 'rssi', 'lat', 'lon', 'freq', 'protocol', 't'}
        for p in DIR_WD.glob('*.jsonl'):
            for line in p.open():
                rec = json.loads(line)
                missing = required - rec.keys()
                assert not missing, f"{p.name}: missing {missing}"


# ── TDOA / ENU sessions ───────────────────────────────────────────────────────

class TestTDOASessions:

    def test_indoor_room(self):
        """2 × 3 m indoor room, low noise, 2 sources."""
        rng = random.Random(10)
        records = (
            _antenna_records() +
            _enu_cluster(1.0,  1.5, 0.0, 30, 0.05, 'tdoa', 'src-A', rng) +
            _enu_cluster(-0.5, 0.8, 0.0, 30, 0.05, 'tdoa', 'src-B', rng)
        )
        out = DIR_TDOA / 'indoor_room.jsonl'
        _write(out, records)
        assert sum(1 for _ in out.open()) == 64  # 4 antenna + 60 source

    def test_outdoor_courtyard(self):
        """15 × 15 m courtyard, medium spread, 3 sources."""
        rng = random.Random(11)
        records = (
            _antenna_records() +
            _enu_cluster( 5.0,  4.0, 0.0, 25, 0.4, 'tdoa', 'node-1', rng) +
            _enu_cluster(-3.0,  6.0, 0.0, 25, 0.4, 'tdoa', 'node-2', rng) +
            _enu_cluster( 1.0, -5.0, 0.0, 25, 0.4, 'tdoa', 'node-3', rng)
        )
        out = DIR_TDOA / 'outdoor_courtyard.jsonl'
        _write(out, records)
        assert sum(1 for _ in out.open()) == 79  # 4 antenna + 75 source

    def test_corridor(self):
        """20 × 2 m corridor: sources spread along the long axis."""
        rng = random.Random(12)
        records = _antenna_records()
        for i, x in enumerate([-8.0, -4.0, 0.0, 4.0, 8.0]):
            records += _enu_cluster(x, rng.uniform(-0.5, 0.5), 0.0,
                                    20, 0.15, 'tdoa', f'corr-{i}', rng)
        out = DIR_TDOA / 'corridor.jsonl'
        _write(out, records)
        assert sum(1 for _ in out.open()) == 104  # 4 antenna + 100 source

    def test_high_noise(self):
        """High noise scenario — large spread, many observations."""
        rng = random.Random(13)
        records = (
            _antenna_records() +
            _enu_cluster(2.0, 2.0, 0.0, 80, 1.5, 'tdoa', 'noisy-A', rng) +
            _enu_cluster(-2.0, -2.0, 0.0, 80, 1.5, 'tdoa', 'noisy-B', rng)
        )
        out = DIR_TDOA / 'high_noise.jsonl'
        _write(out, records)
        assert sum(1 for _ in out.open()) == 164  # 4 antenna + 160 source

    def test_multi_source(self):
        """4 distinct ENU sources, moderate spread."""
        rng = random.Random(14)
        sources = [
            ('src-NE',  3.5,  3.5, 0.0),
            ('src-NW', -3.5,  3.5, 0.0),
            ('src-SE',  3.5, -3.5, 0.0),
            ('src-SW', -3.5, -3.5, 0.0),
        ]
        records = _antenna_records()
        for sid, x, y, z in sources:
            records += _enu_cluster(x, y, z, 20, 0.25, 'tdoa', sid, rng)
        out = DIR_TDOA / 'multi_source.jsonl'
        _write(out, records)
        ids = {json.loads(line)['id'] for line in out.open() if json.loads(line)['method'] == 'tdoa'}
        assert ids == {s[0] for s in sources}

    def test_z_elevated(self):
        """Sources at elevated Z (ceiling-mounted / drone)."""
        rng = random.Random(15)
        records = (
            _antenna_records() +
            _enu_cluster(0.0,  2.0, 2.5, 25, 0.1, 'tdoa', 'ceil-A', rng) +
            _enu_cluster(3.0, -1.0, 2.5, 25, 0.1, 'tdoa', 'ceil-B', rng) +
            _enu_cluster(-2.0, 0.0, 4.0, 25, 0.1, 'tdoa', 'drone',  rng)
        )
        out = DIR_TDOA / 'z_elevated.jsonl'
        _write(out, records)
        for line in out.open():
            rec = json.loads(line)
            if rec['method'] == 'tdoa':  # antenna records may be at z=0
                assert rec['z_enu'] > 0

    def test_records_have_required_fields(self):
        required = {'id', 'x_enu', 'y_enu', 'z_enu', 'rssi', 'method', 't'}
        for p in DIR_TDOA.glob('*.jsonl'):
            for line in p.open():
                rec = json.loads(line)
                missing = required - rec.keys()
                assert not missing, f"{p.name}: missing {missing}"


# ── Array sensing sessions ────────────────────────────────────────────────────

class TestSensingSessions:

    def _rand_dir(self, rng) -> list[float]:
        v = [rng.uniform(-1, 1), rng.uniform(-1, 1), 0.0]
        mag = math.sqrt(sum(x * x for x in v)) or 1.0
        return [round(x / mag, 3) for x in v]

    def test_busy_room(self):
        """Frequent presence + motion events across 2 antennas."""
        rng   = random.Random(20)
        types = ['presence', 'motion', 'motion', 'presence', 'motion']
        records = []
        t = _NOW
        for i in range(60):
            for ant in ['wlan0', 'wlan1']:
                ev_type  = rng.choice(types)
                variance = rng.uniform(0.10, 0.45)
                records.append(_sensing_event(
                    ant, ev_type, variance, self._rand_dir(rng), t))
            t += 0.5
        out = DIR_SENSING / 'busy_room.jsonl'
        _write(out, records)
        assert sum(1 for _ in out.open()) == 120

    def test_empty_corridor(self):
        """Mostly absence events, occasional low-variance motion."""
        rng  = random.Random(21)
        records = []
        t = _NOW
        for i in range(50):
            ev_type  = 'absence' if rng.random() < 0.75 else 'motion'
            variance = rng.uniform(0.01, 0.06)
            records.append(_sensing_event(
                'wlan0', ev_type, variance, self._rand_dir(rng), t))
            t += 1.0
        out = DIR_SENSING / 'empty_corridor.jsonl'
        _write(out, records)
        absence_count = sum(
            1 for line in out.open() if json.loads(line)['type'] == 'absence')
        assert absence_count >= 30

    def test_three_antenna_array(self):
        """3-antenna array: each antenna has direction vectors."""
        rng    = random.Random(22)
        antennas = ['wlan0', 'wlan1', 'wlan2']
        records  = []
        t = _NOW
        for i in range(40):
            ev_type  = rng.choice(['presence', 'motion', 'absence'])
            variance = rng.uniform(0.05, 0.35)
            for ant in antennas:
                records.append(_sensing_event(
                    ant, ev_type, variance + rng.gauss(0, 0.02),
                    self._rand_dir(rng), t))
            t += 0.25
        out = DIR_SENSING / 'three_antenna_array.jsonl'
        _write(out, records)
        ant_ids = {json.loads(line)['antenna_id'] for line in out.open()}
        assert ant_ids == set(antennas)

    def test_long_session(self):
        """150-event time series, single antenna."""
        rng = random.Random(23)
        records = []
        t = _NOW
        for i in range(150):
            # Simulate a gradual increase then decrease in variance
            phase    = math.sin(math.pi * i / 75)
            variance = 0.05 + 0.30 * max(phase, 0) + rng.gauss(0, 0.01)
            ev_type  = 'motion' if variance > 0.20 else (
                'presence' if variance > 0.08 else 'absence')
            records.append(_sensing_event(
                'wlan0', ev_type, variance, self._rand_dir(rng), t))
            t += 0.5
        out = DIR_SENSING / 'long_session.jsonl'
        _write(out, records)
        assert sum(1 for _ in out.open()) == 150

    def test_motion_sequence(self):
        """Realistic presence → motion burst → absence cycle, 2 antennas."""
        rng = random.Random(24)
        records = []
        t = _NOW
        phases = [
            ('absence',  20, 0.03, 0.01),
            ('presence', 10, 0.09, 0.02),
            ('motion',   30, 0.28, 0.08),
            ('presence', 15, 0.11, 0.02),
            ('absence',  25, 0.03, 0.01),
        ]
        for ev_type, n_events, base_var, var_noise in phases:
            for _ in range(n_events):
                for ant in ['wlan0', 'wlan1']:
                    records.append(_sensing_event(
                        ant, ev_type,
                        base_var + abs(rng.gauss(0, var_noise)),
                        self._rand_dir(rng), t))
                t += 0.4
        out = DIR_SENSING / 'motion_sequence.jsonl'
        _write(out, records)
        types_seen = {json.loads(line)['type'] for line in out.open()}
        assert types_seen == {'presence', 'motion', 'absence'}

    def test_multi_variance(self):
        """Each antenna has systematically different variance levels."""
        rng = random.Random(25)
        # wlan0: high variance (near person)
        # wlan1: medium variance
        # wlan2: low variance (far side of room)
        records = []
        t = _NOW
        profiles = [
            ('wlan0', 0.30, 0.05),
            ('wlan1', 0.15, 0.03),
            ('wlan2', 0.04, 0.01),
        ]
        for _ in range(50):
            for ant, base, noise in profiles:
                variance = base + abs(rng.gauss(0, noise))
                ev_type  = 'motion' if variance > 0.20 else (
                    'presence' if variance > 0.08 else 'absence')
                records.append(_sensing_event(
                    ant, ev_type, variance, self._rand_dir(rng), t))
            t += 0.5
        out = DIR_SENSING / 'multi_variance.jsonl'
        _write(out, records)
        # wlan0 should average higher variance than wlan2
        by_ant: dict = {}
        for line in out.open():
            rec = json.loads(line)
            by_ant.setdefault(rec['antenna_id'], []).append(rec['variance'])
        avg = {ant: sum(vs) / len(vs) for ant, vs in by_ant.items()}
        assert avg['wlan0'] > avg['wlan2']

    def test_records_have_required_fields(self):
        required = {'type', 'antenna_id', 'variance', 'direction', 't'}
        valid_types = {'presence', 'motion', 'absence'}
        for p in DIR_SENSING.glob('*.jsonl'):
            for line in p.open():
                rec = json.loads(line)
                missing = required - rec.keys()
                assert not missing, f"{p.name}: missing {missing}"
                assert rec['type'] in valid_types
                assert len(rec['direction']) == 3


# ── Cross-folder summary ──────────────────────────────────────────────────────

class TestSessionIndex:
    """Verify the full set of generated session files."""

    def test_all_subfolders_exist(self):
        for d in (DIR_WD, DIR_TDOA, DIR_SENSING):
            assert d.is_dir(), f"missing subfolder: {d}"

    def test_wardriver_session_count(self):
        files = list(DIR_WD.glob('*.jsonl'))
        assert len(files) >= 7, f"expected ≥7 wardriver sessions, got {len(files)}"

    def test_tdoa_session_count(self):
        files = list(DIR_TDOA.glob('*.jsonl'))
        assert len(files) >= 6, f"expected ≥6 TDOA sessions, got {len(files)}"

    def test_sensing_session_count(self):
        files = list(DIR_SENSING.glob('*.jsonl'))
        assert len(files) >= 6, f"expected ≥6 sensing sessions, got {len(files)}"

    def test_total_session_count(self):
        total = sum(1 for _ in TESTS_DIR.rglob('*.jsonl'))
        assert total >= 19, f"expected ≥19 total session files, got {total}"

    def test_no_empty_files(self):
        for p in TESTS_DIR.rglob('*.jsonl'):
            assert p.stat().st_size > 0, f"empty session file: {p}"
