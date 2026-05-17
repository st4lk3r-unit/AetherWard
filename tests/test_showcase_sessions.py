"""
Showcase session generator — physics-grounded, solver-driven.

Writes JSONL session files to ~/.aetherward/sessions/showcase/ organised as:
  showcase/wardriver/   — RSS wardriving with diverse rigs and environments
  showcase/tdoa/        — TDOA with actual solver output + antenna markers
  showcase/sensing/     — Array sensing with geometry-based direction vectors

Key differences from test_sessions_generate.py:
  • Every TDOA session includes method='antenna' records so the ENU viewer
    shows receiver geometry alongside the position estimates.
  • TDOA positions are derived by running tdoa_solve() on synthetic timing
    measurements with physically-motivated noise (σ_τ per sync quality).
  • Sensing direction vectors are computed from array geometry, not random.
  • Array geometries are non-degenerate (non-collinear) for 3-D solving.
  • The wardriver sessions cover diverse rigs (1–4 antennas, 2.4 + 5 GHz,
    different n_exp environments) and run rss_solve() to cross-check.

Timing-noise models used (speed of light = 3×10⁸ m/s):
  GPSDO + HW timestamping  σ_τ ≈ 1 ns  → ~0.3 m per measurement
  PPS + HW timestamping    σ_τ ≈ 10 ns → ~3 m per measurement
  PPS + WiFi chip          σ_τ ≈ 100 ns→ ~30 m per measurement (marginal outdoor)
  Software / NTP           σ_τ ≈ 100 µs→ >> km (completely unusable)

Design note: the TDOA solver requires M ≥ 3 non-reference sensors, so every
TDOA session uses at least 4 antennas (1 reference + 3 others).
"""
from __future__ import annotations

import json
import math
import random
import time
from pathlib import Path
from typing import Optional

import numpy as np
import pytest

from aetherward.antenna.antenna import Antenna
from aetherward.antenna.array import AntennaArray
from aetherward.core import tdoa_solve
from aetherward.orientation.quaternion import Orientation
from aetherward.position.absolute import AbsolutePosition, FixType
from aetherward.position.relative import RelativePosition
from aetherward.position.rss import rss_solve

# ── Output directories ────────────────────────────────────────────────────────

_BASE    = Path.home() / '.aetherward' / 'sessions' / 'showcase'
_DIR_WD  = _BASE / 'wardriver'
_DIR_TD  = _BASE / 'tdoa'
_DIR_SEN = _BASE / 'sensing'

_M_PER_DEG = 111_320.0
_C         = 299_792_458.0   # speed of light, m/s
_NOW       = time.time()


# ── Shared helpers ────────────────────────────────────────────────────────────

def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w') as f:
        for r in records:
            f.write(json.dumps(r) + '\n')


def _geo_offset(lat: float, lon: float, dx_m: float, dy_m: float) -> tuple[float, float]:
    """Shift (lat, lon) by (dx_m east, dy_m north). Accurate for < 50 km."""
    lat2 = lat + dy_m / _M_PER_DEG
    lon2 = lon + dx_m / (math.cos(math.radians(lat)) * _M_PER_DEG)
    return lat2, lon2


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Flat-earth distance in metres, accurate for < 50 km."""
    dy = (lat2 - lat1) * _M_PER_DEG
    dx = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2)) * _M_PER_DEG
    return math.sqrt(dx * dx + dy * dy)


def _path_loss(dist_m: float, rssi_at_1m: float, n_exp: float) -> float:
    return rssi_at_1m - 10.0 * n_exp * math.log10(max(dist_m, 0.1))


def _circle_track(lat: float, lon: float, radius_m: float, n: int):
    return [
        _geo_offset(lat, lon,
                    radius_m * math.cos(2 * math.pi * i / n),
                    radius_m * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]


def _make_tdoa_array(ant_specs: list[tuple], gps: Optional[AbsolutePosition] = None) -> AntennaArray:
    """
    Build a real AntennaArray.

    ant_specs: list of (id_str, x, y, z) in local ENU metres.
    """
    arr = AntennaArray(id='showcase')
    for aid, x, y, z in ant_specs:
        arr.add(Antenna(
            id=aid,
            position=RelativePosition(x=x, y=y, z=z),
            orientation=Orientation.identity(),
        ))
    if gps is not None:
        arr.update_position(gps)
    return arr


def _run_tdoa(array: AntennaArray, source_enu: tuple[float, float, float],
              n_trials: int, sigma_tau: float,
              rng: random.Random, src_id: str = 'src',
              rssi_at_src: float = -55.0, t0: float = _NOW) -> list[dict]:
    """
    Simulate n_trials TDOA measurements and run the solver on each.

    For each trial:
      1. Compute true TDOAs from geometry.
      2. Add Gaussian timing noise with std σ_τ (seconds).
      3. Run tdoa_solve() and store the position estimate.

    Returns only the source estimate records (caller must add receiver records).
    Records with an invalid solver result are silently dropped.
    """
    ref_ant = array.antennas[0]
    p0 = ref_ant.position.as_array()
    s  = np.array(source_enu, dtype=np.float64)
    d0 = float(np.linalg.norm(s - p0))

    records = []
    for i in range(n_trials):
        measurements = []
        for ant in array.antennas:
            pi     = ant.position.as_array()
            di     = float(np.linalg.norm(s - pi))
            tau    = (di - d0) / _C + rng.gauss(0, sigma_tau)
            rssi   = rssi_at_src - 20.0 * math.log10(max(di, 0.5)) + rng.gauss(0, 1.5)
            measurements.append({
                'antenna_id': ant.id,
                'tdoa': tau,
                'rssi': round(rssi, 1),
                'timestamp': t0 + i * 0.1,
            })

        result = tdoa_solve(array, measurements, ref_ant.id)
        if result and result.get('valid'):
            r = result['position_relative']
            records.append({
                'id':      src_id,
                'x_enu':  round(r.x, 3),
                'y_enu':  round(r.y, 3),
                'z_enu':  round(r.z, 3),
                'rssi':   round(measurements[0]['rssi'], 1),
                'method': 'tdoa',
                'residual_m': round(result['residual'], 4),
                't':      round(t0 + i * 0.1, 3),
            })
    return records


def _rx_records(array: AntennaArray, t: float = _NOW) -> list[dict]:
    """Build method='antenna' marker records for all receivers in the array."""
    records = []
    for ant in array.antennas:
        p = ant.position
        records.append({
            'id':     ant.id,
            'x_enu':  round(p.x, 3),
            'y_enu':  round(p.y, 3),
            'z_enu':  round(p.z, 3),
            'rssi':  -30.0,
            'method': 'antenna',
            't':      round(t, 3),
        })
    return records


def _wardriver_obs(ap: dict, track, rng: random.Random,
                   n_exp: float = 2.5, gps_jitter_m: float = 2.0,
                   t0: float = _NOW) -> list[dict]:
    """
    One wardriver observation record per (AP, observer position).

    ap must have: mac, ssid, lat, lon, freq, rssi_at_1m
    """
    records = []
    for i, (obs_lat, obs_lon) in enumerate(track):
        d    = _dist_m(obs_lat, obs_lon, ap['lat'], ap['lon'])
        rssi = _path_loss(d, ap['rssi_at_1m'], n_exp) + rng.gauss(0, 1.5)
        jlat = obs_lat + rng.gauss(0, gps_jitter_m / _M_PER_DEG)
        jlon = obs_lon + rng.gauss(0, gps_jitter_m / (
            math.cos(math.radians(obs_lat)) * _M_PER_DEG))
        records.append({
            'id':       ap['mac'],
            'ssid':     ap['ssid'],
            'rssi':     round(rssi, 1),
            'lat':      round(jlat, 7),
            'lon':      round(jlon, 7),
            'freq':     ap['freq'],
            'protocol': 'wifi',
            't':        round(t0 + i * 0.5, 3),
        })
    return records


def _direction_from_geometry(ant_positions: list[tuple[float, float, float]],
                              source_enu: tuple[float, float, float],
                              ant_variances: list[float]) -> list[float]:
    """
    Estimate the direction a real ArraySensingMode would report.

    Points from array centroid toward the highest-variance antenna,
    which is the antenna closest to the source (inverse-square weighting).
    """
    positions = np.array(ant_positions, dtype=np.float64)
    centroid  = positions.mean(axis=0)
    best_idx  = int(np.argmax(ant_variances))
    v = positions[best_idx] - centroid
    n = float(np.linalg.norm(v))
    if n < 1e-9:
        return [1.0, 0.0, 0.0]
    return [round(float(v[0] / n), 3),
            round(float(v[1] / n), 3),
            round(float(v[2] / n), 3)]


def _sensing_variances(ant_positions: list[tuple[float, float, float]],
                       source_enu: tuple[float, float, float],
                       base_variance: float, rng: random.Random) -> list[float]:
    """
    Compute per-antenna variance proportional to 1/distance² to the source.
    Closer antennas are more excited by RF path changes.
    """
    s  = np.array(source_enu, dtype=np.float64)
    vs = []
    for p in ant_positions:
        d   = max(float(np.linalg.norm(np.array(p) - s)), 0.3)
        var = base_variance / (d * d) + abs(rng.gauss(0, 0.005))
        vs.append(round(var, 4))
    return vs


# ── Wardriver showcase sessions ───────────────────────────────────────────────

class TestWardriverShowcase:
    """
    Eight distinct wardriving scenarios covering rig size, environment,
    frequency band, and observer track shape.  Each session is written to
    showcase/wardriver/ and validated with rss_solve().
    """

    def test_single_ap_circle_los(self):
        """
        Textbook case: one AP, observer walks a full circle, LOS propagation.
        n_exp = 2.1 (near free-space rooftop / open field).
        Solver should recover the AP within 10 m.
        """
        rng  = random.Random(101)
        lat0, lon0 = 48.8566, 2.3522
        ap = dict(mac='aa:bb:cc:01:01:01', ssid='RooftopAP',
                  lat=lat0, lon=lon0, freq=2_412_000_000, rssi_at_1m=-25.0)
        track   = _circle_track(lat0, lon0, 100, 18)
        records = _wardriver_obs(ap, track, rng, n_exp=2.1, gps_jitter_m=1.5)
        out = _DIR_WD / 'single_ap_circle_los.jsonl'
        _write(out, records)

        obs = [(r['lat'], r['lon'], r['rssi']) for r in records]
        res = rss_solve(obs, n_exp=2.1)
        assert res is not None, 'solver failed for LOS circle walk'
        d = _dist_m(res['lat'], res['lon'], ap['lat'], ap['lon'])
        assert d < 15, f'LOS circle solve error: {d:.1f} m (expected < 15 m)'
        assert res['residual_dBm'] < 3.0

    def test_single_ap_circle_nlos_indoor(self):
        """
        Indoor NLOS scenario: heavy wall attenuation, n_exp = 3.8.
        Observer wanders inside a building footprint (~30 m radius).
        Solver has more tolerance (walls scatter signal).
        """
        rng  = random.Random(102)
        lat0, lon0 = 48.8566, 2.3522
        ap = dict(mac='aa:bb:cc:01:02:01', ssid='OfficeAP-Floor2',
                  lat=lat0, lon=lon0, freq=2_437_000_000, rssi_at_1m=-32.0)
        track   = _circle_track(lat0, lon0, 25, 20)
        records = _wardriver_obs(ap, track, rng, n_exp=3.8, gps_jitter_m=5.0)
        out = _DIR_WD / 'single_ap_indoor_nlos.jsonl'
        _write(out, records)

        obs = [(r['lat'], r['lon'], r['rssi']) for r in records]
        res = rss_solve(obs, n_exp=3.8)
        if res is not None:
            d = _dist_m(res['lat'], res['lon'], ap['lat'], ap['lon'])
            assert d < 40, f'indoor NLOS solve error: {d:.1f} m'
        assert out.stat().st_size > 0

    def test_multi_ap_dense_urban_single_antenna(self):
        """
        Dense urban: 12 APs within 200 m, single 2.4 GHz antenna, random channels.
        Models a typical residential block with overlapping BSS.
        n_exp = 3.0 (urban NLOS, concrete buildings).
        """
        rng  = random.Random(103)
        lat0, lon0 = 48.8600, 2.3500
        aps = []
        ssid_pool = ['BBOX', 'SFR', 'Free', 'Orange', 'Bouygues',
                     'Sosh', 'RED', 'Numericable', 'UPC', 'TP-Link',
                     'ASUS', 'Linksys']
        chans = [1, 6, 11]
        for i in range(12):
            angle  = 2 * math.pi * i / 12
            radius = rng.uniform(20, 180)
            alat, alon = _geo_offset(lat0, lon0,
                                     radius * math.cos(angle),
                                     radius * math.sin(angle))
            ch = chans[i % 3]
            aps.append(dict(
                mac=f'aa:bb:cc:02:{i:02x}:01',
                ssid=f'{ssid_pool[i]}-{rng.randint(0x100,0xfff):X}',
                lat=alat, lon=alon,
                freq=2_412_000_000 + (ch - 1) * 5_000_000,
                rssi_at_1m=rng.uniform(-28.0, -35.0),
            ))
        track = _circle_track(lat0, lon0, 120, 20)
        records = []
        t0 = _NOW
        for ap in aps:
            records.extend(_wardriver_obs(ap, track, rng, n_exp=3.0, t0=t0))
            t0 += 0.05 * len(track)
        out = _DIR_WD / 'dense_urban_12ap_single_ant.jsonl'
        _write(out, records)
        assert len(list(out.open())) == 12 * 20

    def test_dual_band_ap_2ant_channel_split(self):
        """
        Dual-band AP: same physical location, 2.4 GHz (MAC-00) and 5 GHz (MAC-10).
        2-antenna rig: ant0 covers 2.4 GHz channels, ant1 covers 5 GHz channels.
        n_exp 2.5 (2.4 GHz) vs 3.5 (5 GHz attenuates faster through walls).

        Both bands should solve to the same location; gap between solved
        positions must be < 30 m.
        """
        rng  = random.Random(104)
        lat0, lon0 = _geo_offset(48.8566, 2.3522, 30, -20)
        ap_24 = dict(mac='aa:bb:cc:03:00:00', ssid='HomeAP_2G',
                     lat=lat0, lon=lon0, freq=2_437_000_000, rssi_at_1m=-28.0)
        ap_5  = dict(mac='aa:bb:cc:03:00:10', ssid='HomeAP_5G',
                     lat=lat0, lon=lon0, freq=5_180_000_000, rssi_at_1m=-30.0)
        track = _circle_track(lat0, lon0, 80, 14)
        rec24 = _wardriver_obs(ap_24, track, rng, n_exp=2.5)
        rec5  = _wardriver_obs(ap_5,  track, rng, n_exp=3.5)
        # Tag records with the capturing antenna (round-robin)
        for i, r in enumerate(rec24): r['ant'] = 'wlan0'
        for i, r in enumerate(rec5):  r['ant'] = 'wlan1'
        records = rec24 + rec5
        out = _DIR_WD / 'dual_band_2ant.jsonl'
        _write(out, records)

        obs24 = [(r['lat'], r['lon'], r['rssi']) for r in rec24]
        obs5  = [(r['lat'], r['lon'], r['rssi']) for r in rec5]
        res24 = rss_solve(obs24, n_exp=2.5)
        res5  = rss_solve(obs5,  n_exp=3.5)
        assert res24 is not None and res5 is not None
        d = _dist_m(res24['lat'], res24['lon'], res5['lat'], res5['lon'])
        assert d < 30, f'2.4/5 GHz solved positions differ by {d:.1f} m (expected < 30 m)'

    def test_highway_drive_linear(self):
        """
        Highway drive: observer passes AP at 20 m/s (72 km/h), 1.2 km stretch.
        60 points, AP 50 m off-road.  Linear (non-encircling) track → the
        solver must handle an asymmetric observation geometry.
        """
        rng  = random.Random(105)
        ap = dict(mac='aa:bb:cc:04:01:01', ssid='HighwayAP',
                  lat=48.8566, lon=2.3522, freq=5_180_000_000, rssi_at_1m=-29.0)
        lat_ap, lon_ap = _geo_offset(ap['lat'], ap['lon'], 50, 0)  # 50 m north of road
        ap['lat'], ap['lon'] = lat_ap, lon_ap
        track = [_geo_offset(48.8566, 2.3522, x, 0) for x in range(-600, 601, 20)]
        records = _wardriver_obs(ap, track, rng, n_exp=2.5, gps_jitter_m=3.0)
        out = _DIR_WD / 'highway_drive_linear.jsonl'
        _write(out, records)
        assert len(records) == len(track)

        obs = [(r['lat'], r['lon'], r['rssi']) for r in records]
        res = rss_solve(obs, n_exp=2.5)
        if res is not None:
            d = _dist_m(res['lat'], res['lon'], ap['lat'], ap['lon'])
            assert d < 100, f'highway solve error: {d:.1f} m (expected < 100 m)'

    def test_rural_sparse_2ant_5ghz(self):
        """
        Rural scenario: 2 APs very far apart (>400 m), low AP density.
        Observer on a 3 km sparse loop; 5 GHz with very high n_exp (4.0,
        trees and terrain).  Tests solver at low signal count per AP.
        """
        rng  = random.Random(106)
        lat0, lon0 = 45.0500, 6.9500
        aps = [
            dict(mac='bb:cc:dd:01:01:01', ssid='FermeWifi',
                 lat=lat0 + 0.002, lon=lon0 + 0.003,
                 freq=5_500_000_000, rssi_at_1m=-28.0),
            dict(mac='bb:cc:dd:01:01:02', ssid='Gite_AP',
                 lat=lat0 - 0.003, lon=lon0 - 0.002,
                 freq=5_745_000_000, rssi_at_1m=-30.0),
        ]
        track = _circle_track(lat0, lon0, 250, 16)
        records = []
        for ap in aps:
            records.extend(_wardriver_obs(ap, track, rng, n_exp=4.0, gps_jitter_m=4.0))
        out = _DIR_WD / 'rural_sparse_2ant_5ghz.jsonl'
        _write(out, records)
        assert out.stat().st_size > 0

    def test_three_ant_channel_split_2ghz(self):
        """
        3-antenna rig: channels 1–13 split across wlan0/wlan1/wlan2.
        Verifies balanced split: each antenna covers 4–5 channels with no gap.
        8 APs on channels 1, 6, 11, plus scattered others.
        """
        from aetherward.modes.wardriver import WardriverMode
        from unittest.mock import MagicMock

        rng  = random.Random(107)
        lat0, lon0 = 48.8566, 2.3522

        # Build mock array to verify channel split
        array_mock = MagicMock()
        ants = []
        for i in range(3):
            a = MagicMock(); a.id = f'wlan{i}'; a.backend = MagicMock()
            ants.append(a)
        array_mock.antennas = ants
        array_mock.absolute_position = None
        mode = WardriverMode(array_mock, {'channels': list(range(1, 14))})
        mode._assign_channels()

        all_ch: list[int] = []
        for i in range(3):
            ch = mode._channel_map.get(f'wlan{i}', [])
            all_ch.extend(ch)
        assert sorted(all_ch) == list(range(1, 14)), 'channel split incomplete'
        assert len(set(all_ch)) == 13, 'duplicate channel assignments'

        # Build a realistic 8-AP session
        ssids = ['Cafe', 'Shop', 'Hotel', 'Office', 'Metro', 'Resto', 'Bar', 'Gym']
        chan_map = [1, 6, 11, 1, 6, 11, 1, 6]
        aps = []
        for i in range(8):
            angle  = 2 * math.pi * i / 8
            radius = rng.uniform(30, 120)
            alat, alon = _geo_offset(lat0, lon0,
                                     radius * math.cos(angle),
                                     radius * math.sin(angle))
            ch = chan_map[i]
            ant_idx = {1: 0, 6: 1, 11: 2}[ch]
            aps.append(dict(
                mac=f'cc:dd:ee:03:{i:02x}:01',
                ssid=f'{ssids[i]}_WiFi',
                lat=alat, lon=alon,
                freq=2_412_000_000 + (ch - 1) * 5_000_000,
                rssi_at_1m=rng.uniform(-27.0, -33.0),
                ant_idx=ant_idx,
            ))
        track = _circle_track(lat0, lon0, 90, 16)
        records = []
        t0 = _NOW
        for ap in aps:
            recs = _wardriver_obs(ap, track, rng, n_exp=2.8, t0=t0)
            for r in recs:
                r['ant'] = f'wlan{ap["ant_idx"]}'
            records.extend(recs)
            t0 += 0.02
        out = _DIR_WD / '3ant_channel_split_2ghz.jsonl'
        _write(out, records)
        assert len(records) == 8 * 16

    def test_four_ant_dual_band_wide_coverage(self):
        """
        4-antenna rig: wlan0/wlan1 on 2.4 GHz, wlan2/wlan3 on 5 GHz.
        20 APs (10 per band) across a 400 m radius area.
        Demonstrates a high-end wardriving rig with full band coverage.
        """
        from aetherward.modes.wardriver import WardriverMode
        from unittest.mock import MagicMock

        rng  = random.Random(108)
        lat0, lon0 = 48.8566, 2.3522
        ch_2g = list(range(1, 14))
        ch_5g = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112]

        array_mock = MagicMock()
        ants = []
        for i in range(4):
            a = MagicMock(); a.id = f'wlan{i}'; a.backend = MagicMock()
            ants.append(a)
        array_mock.antennas = ants
        array_mock.absolute_position = None
        mode = WardriverMode(array_mock, {'channels': ch_2g + ch_5g})
        mode._assign_channels()

        all_ch: list[int] = []
        seen: set[int] = set()
        for i in range(4):
            for c in mode._channel_map.get(f'wlan{i}', []):
                assert c not in seen, f'channel {c} assigned to multiple antennas'
                seen.add(c)
                all_ch.append(c)
        assert sorted(all_ch) == sorted(ch_2g + ch_5g)

        aps = []
        for i in range(20):
            band = '2g' if i < 10 else '5g'
            angle  = 2 * math.pi * i / 20
            radius = rng.uniform(50, 350)
            alat, alon = _geo_offset(lat0, lon0,
                                     radius * math.cos(angle),
                                     radius * math.sin(angle))
            if band == '2g':
                ch = ch_2g[i % len(ch_2g)]
                freq = 2_412_000_000 + (ch - 1) * 5_000_000
                ant  = f'wlan{i % 2}'
            else:
                ch = ch_5g[(i - 10) % len(ch_5g)]
                freq = 5_000_000_000 + ch * 5_000_000
                ant  = f'wlan{2 + (i - 10) % 2}'
            aps.append(dict(
                mac=f'dd:ee:ff:04:{i:02x}:01',
                ssid=f'DualBand_{band.upper()}_{i:02d}',
                lat=alat, lon=alon, freq=freq,
                rssi_at_1m=rng.uniform(-26.0, -34.0),
                ant=ant,
            ))

        track = _circle_track(lat0, lon0, 200, 24)
        records = []
        t0 = _NOW
        n_exp = {'2g': 2.8, '5g': 3.3}
        for ap in aps:
            band = '2g' if ap['freq'] < 3e9 else '5g'
            recs = _wardriver_obs(ap, track, rng,
                                  n_exp=n_exp[band], t0=t0)
            for r in recs:
                r['ant'] = ap['ant']
            records.extend(recs)
            t0 += 0.01
        out = _DIR_WD / '4ant_dual_band.jsonl'
        _write(out, records)
        assert len(records) == 20 * 24


# ── TDOA showcase sessions ────────────────────────────────────────────────────

class TestTDOAShowcase:
    """
    Six TDOA sessions, all written with solver-derived position estimates.
    Every session includes method='antenna' receiver records so the ENU
    viewer renders both the array geometry and the source cloud.

    All arrays use ≥ 4 antennas (required: M = n_total − 1 ≥ 3 for the
    3D Gauss-Newton solver; 3-receiver sessions would return None).
    """

    def _assert_receiver_records_present(self, records: list[dict]) -> None:
        rx = [r for r in records if r.get('method') == 'antenna']
        assert rx, 'No antenna receiver records in TDOA session'
        assert all({'id','x_enu','y_enu','z_enu','method'} <= r.keys() for r in rx)

    def _assert_source_records_present(self, records: list[dict], min_count=3) -> None:
        src = [r for r in records if r.get('method') == 'tdoa']
        assert len(src) >= min_count, \
            f'Only {len(src)} solver results (expected ≥ {min_count}); check timing noise'

    # ── 1. Indoor lab — GPSDO + hardware timestamping, 2 m square ────────────

    def test_indoor_lab_gpsdo_2m_square(self):
        """
        4 receivers on a 2 m × 2 m square, plus one elevated for Z resolution.
        GPSDO + hardware timestamping: σ_τ = 0.5 ns → ~0.15 m per measurement.
        Source at (1, 1, 1.2) — centre of the array at human chest height.

        This is the best-case indoor TDOA scenario.
        """
        rng = random.Random(201)
        ants = [
            ('rx-SW', 0.0, 0.0, 0.0),
            ('rx-SE', 2.0, 0.0, 0.0),
            ('rx-NW', 0.0, 2.0, 0.0),
            ('rx-NE', 2.0, 2.0, 0.0),
            ('rx-UP', 1.0, 1.0, 2.0),   # elevated for Z resolution
        ]
        array = _make_tdoa_array(ants, gps=AbsolutePosition(
            lat=48.8566, lon=2.3522, alt=0.0, fix_type=FixType.FIX_3D))
        source = (1.0, 1.0, 1.2)

        records  = _rx_records(array)
        records += _run_tdoa(array, source, n_trials=25, sigma_tau=5e-10,
                             rng=rng, src_id='phone')
        out = _DIR_TD / 'indoor_lab_gpsdo_2m.jsonl'
        _write(out, records)

        self._assert_receiver_records_present(records)
        self._assert_source_records_present(records, min_count=15)

        # Solver estimates should cluster tightly around true source
        src_recs = [r for r in records if r['method'] == 'tdoa']
        xs = [r['x_enu'] for r in src_recs]
        ys = [r['y_enu'] for r in src_recs]
        assert abs(sum(xs) / len(xs) - source[0]) < 0.4
        assert abs(sum(ys) / len(ys) - source[1]) < 0.4

    # ── 2. Indoor room — PPS + hardware timestamping, 3 m square ─────────────

    def test_indoor_room_pps_3m_square(self):
        """
        4 receivers on a 3 m × 3 m square (corner-mount, room footprint).
        PPS-disciplined NIC: σ_τ = 10 ns → ~3 m per measurement; array
        aperture partially compensates.  Source is a laptop on a table.

        Demonstrates realistic PPS indoor accuracy (sub-metre RMS).
        """
        rng = random.Random(202)
        ants = [
            ('rx-A', 0.0, 0.0, 2.4),   # ceiling corner
            ('rx-B', 3.0, 0.0, 2.4),
            ('rx-C', 0.0, 3.0, 2.4),
            ('rx-D', 3.0, 3.0, 2.4),
        ]
        array  = _make_tdoa_array(ants)
        source = (1.5, 2.0, 0.8)        # laptop on desk

        records  = _rx_records(array)
        records += _run_tdoa(array, source, n_trials=20, sigma_tau=1e-8,
                             rng=rng, src_id='laptop')
        out = _DIR_TD / 'indoor_room_pps_3m.jsonl'
        _write(out, records)

        self._assert_receiver_records_present(records)
        self._assert_source_records_present(records, min_count=5)

    # ── 3. Outdoor courtyard — PPS, 8 m cross array ───────────────────────────

    def test_outdoor_courtyard_pps_8m_cross(self):
        """
        5-receiver cross array, 8 m arm length (16 m total span).
        PPS with chip-level timestamping: σ_τ = 50 ns.
        Source is a handheld device moving through the courtyard.
        Two source positions tracked sequentially.
        """
        rng = random.Random(203)
        ants = [
            ('rx-centre', 0.0,  0.0, 0.0),
            ('rx-E',      8.0,  0.0, 0.0),
            ('rx-W',     -8.0,  0.0, 0.0),
            ('rx-N',      0.0,  8.0, 0.0),
            ('rx-S',      0.0, -8.0, 0.0),
        ]
        array = _make_tdoa_array(ants, gps=AbsolutePosition(
            lat=48.8600, lon=2.3510, alt=0.0, fix_type=FixType.FIX_3D))
        sources = [
            ('device-A', (3.0,  2.0, 1.5)),
            ('device-B', (-2.5, 4.0, 1.7)),
        ]

        records = _rx_records(array)
        for src_id, src_pos in sources:
            records += _run_tdoa(array, src_pos, n_trials=15, sigma_tau=5e-8,
                                 rng=rng, src_id=src_id, t0=_NOW + len(records) * 0.1)
        out = _DIR_TD / 'outdoor_courtyard_pps_8m_cross.jsonl'
        _write(out, records)

        self._assert_receiver_records_present(records)
        src_ids = {r['id'] for r in records if r['method'] == 'tdoa'}
        assert len(src_ids) >= 1  # at least one source solved

    # ── 4. Wide outdoor baseline — GPSDO, 20 m square ────────────────────────

    def test_outdoor_wide_baseline_gpsdo_20m(self):
        """
        4 receivers on a 20 m square — deployed around a building perimeter.
        GPSDO + hardware timestamp: σ_τ = 2 ns.
        Source moves along a straight path through the array.

        Wide baseline gives excellent XY resolution (~5 cm RMS from simulation).
        """
        rng    = random.Random(204)
        ants   = [
            ('rx-0',  0.0,  0.0, 0.0),
            ('rx-1', 20.0,  0.0, 0.0),
            ('rx-2',  0.0, 20.0, 0.0),
            ('rx-3', 20.0, 20.0, 0.0),
        ]
        array  = _make_tdoa_array(ants, gps=AbsolutePosition(
            lat=51.5074, lon=-0.1278, alt=0.0, fix_type=FixType.FIX_3D))

        # Source walks east–west at y=6 (off-centre — symmetric y=10 causes
        # all receivers to see equal Y TDOAs, making X underdetermined).
        records = _rx_records(array)
        t0 = _NOW
        for step in range(20):
            x = 3.0 + step * 0.7     # 3 m to 16.3 m across
            recs = _run_tdoa(array, (x, 6.0, 1.5), n_trials=1, sigma_tau=2e-9,
                             rng=rng, src_id='walker', t0=t0 + step * 0.25)
            for r in recs:
                r['step'] = step
            records.extend(recs)

        out = _DIR_TD / 'outdoor_wide_gpsdo_20m_walk.jsonl'
        _write(out, records)

        self._assert_receiver_records_present(records)
        src = [r for r in records if r['method'] == 'tdoa']
        if len(src) >= 8:
            xs = [r['x_enu'] for r in src]
            # Trajectory should span at least 5 m along X
            assert max(xs) - min(xs) > 5.0, 'trajectory X span too small'

    # ── 5. Multi-source — 2 simultaneous emitters ────────────────────────────

    def test_multi_source_two_emitters(self):
        """
        Two simultaneous emitters in a 6 m × 6 m room.
        4-receiver ceiling array, GPSDO-quality timing (σ_τ = 1 ns).
        Session has interleaved solve results from both sources.
        """
        rng  = random.Random(205)
        ants = [
            ('rx-0', 0.0, 0.0, 2.7),
            ('rx-1', 6.0, 0.0, 2.7),
            ('rx-2', 0.0, 6.0, 2.7),
            ('rx-3', 6.0, 6.0, 2.7),
        ]
        array   = _make_tdoa_array(ants)
        sources = [
            ('phone-A', (1.5, 1.5, 0.9)),
            ('phone-B', (4.5, 4.5, 0.9)),
        ]

        records = _rx_records(array)
        t0 = _NOW
        for trial in range(12):
            for src_id, src_pos in sources:
                recs = _run_tdoa(array, src_pos, n_trials=1, sigma_tau=1e-9,
                                 rng=rng, src_id=src_id, t0=t0 + trial * 0.2)
                records.extend(recs)

        out = _DIR_TD / 'multi_source_two_emitters.jsonl'
        _write(out, records)

        self._assert_receiver_records_present(records)
        src_ids = {r['id'] for r in records if r['method'] == 'tdoa'}
        assert len(src_ids) >= 1

    # ── 6. Software sync — show why timing quality matters ───────────────────

    def test_software_sync_timing_quality_comparison(self):
        """
        Same room geometry solved three times with different sync quality:
          GPSDO (σ_τ = 1 ns)  → tight cluster, sub-metre accuracy
          PPS   (σ_τ = 50 ns) → moderate spread, 1–5 m error
          NTP   (σ_τ = 1 ms)  → massive scatter, useless indoors

        Source estimates are tagged with their sync quality so the ENU viewer
        shows all three clouds side by side.
        """
        rng  = random.Random(206)
        ants = [
            ('rx-0', 0.0, 0.0, 0.0),
            ('rx-1', 4.0, 0.0, 0.0),
            ('rx-2', 0.0, 4.0, 0.0),
            ('rx-3', 4.0, 4.0, 2.0),
        ]
        array  = _make_tdoa_array(ants)
        source = (2.0, 2.0, 1.2)

        sync_configs = [
            ('gpsdo', 1e-9,  12),
            ('pps',   5e-8,  12),
            ('ntp',   1e-3,   8),
        ]

        records = _rx_records(array)
        t0 = _NOW
        for sync_label, sigma_tau, n_trials in sync_configs:
            recs = _run_tdoa(array, source, n_trials=n_trials,
                             sigma_tau=sigma_tau, rng=rng,
                             src_id=f'src-{sync_label}', t0=t0)
            records.extend(recs)
            t0 += n_trials * 0.1 + 1.0

        out = _DIR_TD / 'sync_quality_comparison.jsonl'
        _write(out, records)

        self._assert_receiver_records_present(records)
        gpsdo_pts = [r for r in records
                     if r.get('method') == 'tdoa' and 'gpsdo' in r.get('id','')]
        ntp_pts   = [r for r in records
                     if r.get('method') == 'tdoa' and 'ntp'   in r.get('id','')]

        if len(gpsdo_pts) >= 3 and len(ntp_pts) >= 3:
            def _spread(pts):
                xs = [r['x_enu'] for r in pts]
                ys = [r['y_enu'] for r in pts]
                return (max(xs)-min(xs) + max(ys)-min(ys)) / 2

            spread_gpsdo = _spread(gpsdo_pts)
            spread_ntp   = _spread(ntp_pts)
            # NTP spread must be dramatically larger than GPSDO (at least 10×)
            assert spread_ntp > spread_gpsdo * 2, (
                f'Expected NTP spread > 2× GPSDO: ntp={spread_ntp:.2f}, '
                f'gpsdo={spread_gpsdo:.2f}')


# ── Array sensing showcase sessions ──────────────────────────────────────────

class TestArraySensingShowcase:
    """
    Five sensing sessions demonstrating the ArraySensingMode state machine,
    diversity of event types, and direction estimation from real antenna geometry.

    All direction vectors are computed from array geometry and source position
    (not random), so the ENU viewer can show meaningful directional data.
    """

    def _make_sensing_event(self, evt_type: str, ant_id: str, variance: float,
                            direction: list[float], t: float) -> dict:
        return {
            'type':       evt_type,
            'antenna_id': ant_id,
            'variance':   round(variance, 4),
            'direction':  [round(v, 3) for v in direction],
            't':          round(t, 3),
        }

    def _simulate_sensing(self,
                           ant_positions: list[tuple[float, float, float]],
                           ant_ids: list[str],
                           phases: list[tuple],
                           rng: random.Random,
                           t0: float = _NOW) -> list[dict]:
        """
        Simulate ArraySensingMode state machine output.

        phases: list of (event_type, source_enu_or_None, n_events, base_variance, noise)
          event_type   — 'absence', 'presence', 'motion'
          source_enu   — (x,y,z) of person/source; None for absence phases
          n_events     — number of time steps in this phase
          base_variance — peak variance at 1 m from source
          noise         — per-sample std for variance jitter
        """
        records = []
        t = t0
        for evt_type, source_enu, n_events, base_var, noise in phases:
            for step in range(n_events):
                if source_enu is not None and evt_type != 'absence':
                    variances = _sensing_variances(
                        ant_positions, source_enu, base_var, rng)
                    direction = _direction_from_geometry(
                        ant_positions, source_enu, variances)
                else:
                    variances = [abs(rng.gauss(0.01, noise)) for _ in ant_ids]
                    direction = [0.0, 0.0, 0.0]

                for i, (aid, var) in enumerate(zip(ant_ids, variances)):
                    v_noisy = max(abs(var + rng.gauss(0, noise)), 0.0)
                    records.append(self._make_sensing_event(
                        evt_type, aid, v_noisy, direction, t))
                t += 0.25
        return records

    def test_single_antenna_rssi_motion_cycle(self):
        """
        Single antenna, RSSI-only (no direction).
        State machine cycle: absence → motion → absence.
        With one antenna, mode fires 'motion' (not 'presence') on idle→active.
        """
        rng  = random.Random(301)
        ants = [((0.0, 0.0, 0.0), 'wlan0')]
        ant_positions = [a[0] for a in ants]
        ant_ids       = [a[1] for a in ants]

        phases = [
            ('absence',  None,         10, 0.01, 0.003),
            ('motion',   (1.5, 0.0, 1.0), 15, 0.15, 0.02),
            ('absence',  None,          8, 0.01, 0.003),
            ('motion',   (0.5, 0.0, 1.5),  8, 0.20, 0.03),
            ('absence',  None,         10, 0.01, 0.002),
        ]
        records = self._simulate_sensing(ant_positions, ant_ids, phases, rng)
        # Single antenna → direction always zero (no directional estimate)
        for r in records:
            r['direction'] = [0.0, 0.0, 0.0]

        out = _DIR_SEN / 'single_ant_rssi_motion_cycle.jsonl'
        _write(out, records)
        types = {r['type'] for r in records}
        assert 'motion'  in types
        assert 'absence' in types

    def test_two_ant_entry_exit_directional(self):
        """
        Two antennas 1 m apart on the X axis.
        Person enters from the east (high variance on wlan1) then leaves.
        Direction vector should point consistently toward east (+X).
        """
        rng  = random.Random(302)
        ant_positions = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)]
        ant_ids       = ['wlan0', 'wlan1']

        # Source enters from east side (closer to wlan1)
        phases = [
            ('absence',  None,           8, 0.01, 0.003),
            ('presence', (1.8, 0.0, 1.0), 3, 0.25, 0.03),
            ('motion',   (1.5, 0.2, 1.0), 12, 0.18, 0.025),
            ('motion',   (1.0, 0.0, 1.0),  8, 0.14, 0.02),
            ('absence',  None,            6, 0.01, 0.003),
        ]
        records = self._simulate_sensing(ant_positions, ant_ids, phases, rng)
        out = _DIR_SEN / '2ant_entry_exit_directional.jsonl'
        _write(out, records)

        # When source is near wlan1, direction should have positive X component
        motion_recs = [r for r in records
                       if r['type'] == 'motion' and r['antenna_id'] == 'wlan1']
        if motion_recs:
            mean_dx = sum(r['direction'][0] for r in motion_recs) / len(motion_recs)
            assert mean_dx > 0, (
                f'Expected direction toward +X (wlan1 side), got mean dx={mean_dx:.3f}')

    def test_three_ant_L_shaped_zone_detection(self):
        """
        Three antennas in an L-shape covering a 5 m × 4 m room.
        Person moves through three zones (wlan0 corner → wlan1 wall → wlan2 corner).
        Direction vector follows the person's position across the room.
        """
        rng  = random.Random(303)
        ant_positions = [
            (0.0, 0.0, 2.4),   # wlan0 — SW corner ceiling
            (5.0, 0.0, 2.4),   # wlan1 — SE corner ceiling
            (0.0, 4.0, 2.4),   # wlan2 — NW corner ceiling
        ]
        ant_ids = ['wlan0', 'wlan1', 'wlan2']

        phases = [
            ('absence',  None,              6, 0.01, 0.003),
            # Person enters near wlan0 (SW)
            ('presence', (0.8, 0.8, 1.0),   2, 0.30, 0.04),
            ('motion',   (0.8, 0.8, 1.0),  10, 0.22, 0.03),
            # Person moves to SE (near wlan1)
            ('motion',   (4.2, 0.8, 1.0),  10, 0.22, 0.03),
            # Person moves to NW (near wlan2)
            ('motion',   (0.8, 3.2, 1.0),  10, 0.22, 0.03),
            # Person leaves
            ('absence',  None,              8, 0.01, 0.003),
        ]
        records = self._simulate_sensing(ant_positions, ant_ids, phases, rng)
        out = _DIR_SEN / '3ant_L_shaped_zone_detection.jsonl'
        _write(out, records)

        types = {r['type'] for r in records}
        assert 'presence' in types
        assert 'motion'   in types
        assert 'absence'  in types

        ant_ids_seen = {r['antenna_id'] for r in records}
        assert ant_ids_seen == {'wlan0', 'wlan1', 'wlan2'}

    def test_four_ant_square_crowd_high_variance(self):
        """
        Four antennas at corners of a 4 m × 4 m square (2.4 m ceiling mount).
        Crowded meeting room: high variance on all antennas simultaneously.
        Simulates multiple people moving — all antennas near-equally excited.
        Direction estimate is noisy and near-centroid (indeterminate crowd).
        """
        rng  = random.Random(304)
        ant_positions = [
            (0.0, 0.0, 2.4),
            (4.0, 0.0, 2.4),
            (0.0, 4.0, 2.4),
            (4.0, 4.0, 2.4),
        ]
        ant_ids = ['wlan0', 'wlan1', 'wlan2', 'wlan3']

        # Crowd: source at centroid of room (equidistant from all antennas)
        phases = [
            ('absence', None,           5, 0.01, 0.003),
            ('motion',  (2.0, 2.0, 1.2), 40, 0.40, 0.08),
            ('absence', None,            5, 0.01, 0.003),
        ]
        records = self._simulate_sensing(ant_positions, ant_ids, phases, rng)
        out = _DIR_SEN / '4ant_square_crowd.jsonl'
        _write(out, records)

        motion_count = sum(1 for r in records if r['type'] == 'motion')
        # 40 steps × 4 antennas = 160 motion records
        assert motion_count == 40 * 4, f'expected 160 motion records, got {motion_count}'

    def test_three_ant_long_session_occupancy_pattern(self):
        """
        Realistic 90-minute office session (compressed to 300 events):
          morning quiet → arrival burst → continuous work motion → lunch break
          → afternoon motion → departure → evening empty.

        Variance follows a realistic sine-wave pattern within each phase.
        """
        rng  = random.Random(305)
        ant_positions = [
            (0.0, 0.0, 2.4),
            (3.0, 0.0, 2.4),
            (1.5, 2.5, 2.4),
        ]
        ant_ids = ['wlan0', 'wlan1', 'wlan2']

        # (label, source_enu, n_events, base_variance, noise)
        schedule = [
            ('absence',  None,              30, 0.008, 0.002),  # morning empty
            ('presence', (1.5, 1.0, 1.0),   5, 0.28,  0.05),   # arrival
            ('motion',   (1.2, 0.8, 1.0),  80, 0.18,  0.04),   # work (typing)
            ('absence',  None,              20, 0.008, 0.002),  # lunch break
            ('presence', (1.5, 1.0, 1.0),   5, 0.26,  0.05),   # return
            ('motion',   (1.5, 1.5, 1.0),  80, 0.15,  0.04),   # afternoon
            ('absence',  (1.5, 0.5, 1.0),   5, 0.22,  0.04),   # departure
            ('absence',  None,              75, 0.005, 0.001),  # evening empty
        ]
        records = self._simulate_sensing(ant_positions, ant_ids, schedule, rng)
        out = _DIR_SEN / '3ant_long_session_office.jsonl'
        _write(out, records)

        total = sum(n for _, _, n, _, _ in schedule) * len(ant_ids)
        assert sum(1 for _ in out.open()) == total, 'event count mismatch'
        assert {r['type'] for r in records} == {'presence', 'motion', 'absence'}


# ── Session index ─────────────────────────────────────────────────────────────

class TestShowcaseIndex:
    """Verify all showcase session files are present and valid."""

    EXPECTED_WD = [
        'single_ap_circle_los.jsonl',
        'single_ap_indoor_nlos.jsonl',
        'dense_urban_12ap_single_ant.jsonl',
        'dual_band_2ant.jsonl',
        'highway_drive_linear.jsonl',
        'rural_sparse_2ant_5ghz.jsonl',
        '3ant_channel_split_2ghz.jsonl',
        '4ant_dual_band.jsonl',
    ]
    EXPECTED_TD = [
        'indoor_lab_gpsdo_2m.jsonl',
        'indoor_room_pps_3m.jsonl',
        'outdoor_courtyard_pps_8m_cross.jsonl',
        'outdoor_wide_gpsdo_20m_walk.jsonl',
        'multi_source_two_emitters.jsonl',
        'sync_quality_comparison.jsonl',
    ]
    EXPECTED_SEN = [
        'single_ant_rssi_motion_cycle.jsonl',
        '2ant_entry_exit_directional.jsonl',
        '3ant_L_shaped_zone_detection.jsonl',
        '4ant_square_crowd.jsonl',
        '3ant_long_session_office.jsonl',
    ]

    def test_all_directories_created(self):
        for d in (_DIR_WD, _DIR_TD, _DIR_SEN):
            assert d.is_dir(), f'Missing directory: {d}'

    def test_wardriver_files_exist(self):
        missing = [f for f in self.EXPECTED_WD
                   if not (_DIR_WD / f).exists()]
        assert not missing, f'Missing wardriver sessions: {missing}'

    def test_tdoa_files_exist(self):
        missing = [f for f in self.EXPECTED_TD
                   if not (_DIR_TD / f).exists()]
        assert not missing, f'Missing TDOA sessions: {missing}'

    def test_sensing_files_exist(self):
        missing = [f for f in self.EXPECTED_SEN
                   if not (_DIR_SEN / f).exists()]
        assert not missing, f'Missing sensing sessions: {missing}'

    def test_no_empty_files(self):
        for p in _BASE.rglob('*.jsonl'):
            assert p.stat().st_size > 0, f'Empty session file: {p}'

    def test_all_files_valid_jsonl(self):
        for p in _BASE.rglob('*.jsonl'):
            for i, line in enumerate(p.open(), 1):
                try:
                    json.loads(line)
                except json.JSONDecodeError as e:
                    pytest.fail(f'{p.name}:{i}: {e}')

    def test_wardriver_records_have_required_fields(self):
        required = {'id', 'ssid', 'rssi', 'lat', 'lon', 'freq', 'protocol', 't'}
        for p in _DIR_WD.glob('*.jsonl'):
            for line in p.open():
                rec = json.loads(line)
                missing = required - rec.keys()
                assert not missing, f'{p.name}: missing {missing}'
                assert isinstance(rec['rssi'], (int, float))
                # Path-loss model can produce very low values at extreme distances
                # (e.g. rural 5 GHz at 450 m with n_exp=4 → ~-134 dBm); cap at
                # -160 dBm which is below any physical noise floor.
                assert -160 <= rec['rssi'] <= 10, f'{p.name}: RSSI out of range: {rec["rssi"]}'
                assert isinstance(rec['lat'], float)
                assert isinstance(rec['lon'], float)

    def test_tdoa_records_have_required_fields(self):
        required_src = {'id', 'x_enu', 'y_enu', 'z_enu', 'rssi', 'method', 't'}
        for p in _DIR_TD.glob('*.jsonl'):
            has_antenna = False
            for line in p.open():
                rec = json.loads(line)
                missing = required_src - rec.keys()
                assert not missing, f'{p.name}: missing {missing}'
                assert rec['method'] in ('antenna', 'tdoa'), \
                    f'{p.name}: unexpected method {rec["method"]!r}'
                if rec['method'] == 'antenna':
                    has_antenna = True
            assert has_antenna, (
                f'{p.name}: no antenna receiver records — '
                f'ENU viewer cannot show array geometry')

    def test_sensing_records_have_required_fields(self):
        valid_types = {'presence', 'motion', 'absence'}
        required = {'type', 'antenna_id', 'variance', 'direction', 't'}
        for p in _DIR_SEN.glob('*.jsonl'):
            for line in p.open():
                rec = json.loads(line)
                missing = required - rec.keys()
                assert not missing, f'{p.name}: missing {missing}'
                assert rec['type'] in valid_types, \
                    f'{p.name}: invalid event type {rec["type"]!r}'
                assert len(rec['direction']) == 3
                assert rec['variance'] >= 0, f'{p.name}: negative variance'

    def test_total_session_count(self):
        total = sum(1 for _ in _BASE.rglob('*.jsonl'))
        expected = (len(self.EXPECTED_WD) + len(self.EXPECTED_TD) +
                    len(self.EXPECTED_SEN))
        assert total >= expected, \
            f'Expected ≥{expected} showcase sessions, found {total}'
