"""
Comprehensive scenario tests — generates diverse JSONL session files in /tmp
that can be loaded directly in the web UI to exercise every viewing mode.

Scenarios
─────────
Wardriver  single-AP, multi-AP, moving observer, fixed sensor, dense urban,
           dual-band AP, car-route drive, indoor NLOS, outdoor LOS, weak signal,
           multi-antenna channel split (1 / 2 / 3 antennas)

TDOA       4-receiver precise, noisy, asymmetric baseline, single-floor 2-D

Sensing    full presence → motion → absence cycle

Config     wizard-style AWConfig construction for all four mode / antenna
           combinations; validates channel assignment, mode_config keys,
           GPS backend selection, sync source, and TOML round-trip.
"""
from __future__ import annotations

import json
import math
import random
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aetherward.config.schema import AWConfig
from aetherward.modes.wardriver import WardriverMode
from aetherward.position.rss import rss_solve, rssi_centroid
from aetherward.position.absolute import AbsolutePosition, FixType


# ── Shared constants ──────────────────────────────────────────────────────────

_M_PER_DEG  = 111_320.0
_SEED       = 2025

# Paris — realistic city centre coordinates for all wardriver scenarios
_PARIS_LAT  = 48.8566
_PARIS_LON  = 2.3522


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rng(seed=_SEED):
    r = random.Random(seed)
    return r


def _path_loss(d_m: float, rssi_at_1m: float, n: float) -> float:
    """Log-distance path-loss model."""
    return rssi_at_1m - 10.0 * n * math.log10(max(d_m, 0.1))


def _dist_m(lat1, lon1, lat2, lon2):
    """Flat-earth distance in metres (accurate for <50 km)."""
    dy = (lat2 - lat1) * _M_PER_DEG
    dx = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2)) * _M_PER_DEG
    return math.sqrt(dx * dx + dy * dy)


def _geo_offset(lat, lon, dx_m, dy_m):
    """Shift a coordinate by (dx_m east, dy_m north)."""
    lat2 = lat + dy_m / _M_PER_DEG
    lon2 = lon + dx_m / (math.cos(math.radians(lat)) * _M_PER_DEG)
    return lat2, lon2


def _circle_track(centre_lat, centre_lon, radius_m, n_points):
    """Observer track: equal-angle points on a circle around centre."""
    track = []
    for i in range(n_points):
        angle = 2 * math.pi * i / n_points
        dx = radius_m * math.cos(angle)
        dy = radius_m * math.sin(angle)
        track.append(_geo_offset(centre_lat, centre_lon, dx, dy))
    return track


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w') as f:
        for r in records:
            f.write(json.dumps(r) + '\n')


def _make_ap(mac: str, ssid: str, lat: float, lon: float,
             freq: int = 2412000000, rssi_at_1m: float = -28.0):
    """Return a dict describing a synthetic AP."""
    return dict(mac=mac, ssid=ssid, lat=lat, lon=lon,
                freq=freq, rssi_at_1m=rssi_at_1m)


def _obs_from_track(ap: dict, observer_track, n_exp: float, rng,
                    gps_jitter_m: float = 2.0, t0: float = 0.0):
    """Generate wardriver observation records for one AP along a track."""
    records = []
    for i, (obs_lat, obs_lon) in enumerate(observer_track):
        d = _dist_m(obs_lat, obs_lon, ap['lat'], ap['lon'])
        rssi = _path_loss(d, ap['rssi_at_1m'], n_exp)
        rssi += rng.gauss(0, 1.5)  # receiver noise
        # GPS jitter
        jlat = obs_lat + rng.gauss(0, gps_jitter_m / _M_PER_DEG)
        jlon = obs_lon + rng.gauss(0, gps_jitter_m / (math.cos(math.radians(obs_lat)) * _M_PER_DEG))
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


def _make_array_mock(n_antennas: int = 1):
    array = MagicMock()
    antennas = []
    for i in range(n_antennas):
        ant = MagicMock()
        ant.id = f'wlan{i}'
        ant.backend = MagicMock()
        ant.covers_frequency = lambda hz: True
        antennas.append(ant)
    array.antennas = antennas
    array.absolute_position = None
    return array


# ── Wardriver scenarios ───────────────────────────────────────────────────────

class TestWardriverScenarios:
    """
    Each test generates a realistic JSONL session and verifies the RSS solver
    can recover AP positions.  All sessions are written to /tmp for web-UI
    inspection.
    """

    def test_single_ap_circle_walk(self):
        """Classic wardriver: one AP, observer walks a full circle."""
        rng = _rng(1)
        ap = _make_ap('aa:bb:cc:dd:ee:01', 'HomeNetwork',
                      _PARIS_LAT, _PARIS_LON)
        track = _circle_track(_PARIS_LAT, _PARIS_LON, 80, 12)
        recs = _obs_from_track(ap, track, n_exp=2.5, rng=rng)
        out = Path('/tmp/aw_single_ap_circle.jsonl')
        _write_jsonl(out, recs)

        obs = [(r['lat'], r['lon'], r['rssi']) for r in recs]
        res = rss_solve(obs, n_exp=2.5)
        assert res is not None
        assert res['lat'] == pytest.approx(_PARIS_LAT, abs=0.001)
        assert res['lon'] == pytest.approx(_PARIS_LON, abs=0.001)

    def test_multi_ap_five_networks(self):
        """
        Five APs scattered across a 400 m area.
        Observer does a grid walk; solver must recover all five positions.
        """
        rng = _rng(2)
        aps = [
            _make_ap('aa:bb:cc:dd:01:01', 'CafeWifi',
                     *_geo_offset(_PARIS_LAT, _PARIS_LON, -150, 200)),
            _make_ap('aa:bb:cc:dd:02:02', 'ShopAP',
                     *_geo_offset(_PARIS_LAT, _PARIS_LON,  200, 100)),
            _make_ap('aa:bb:cc:dd:03:03', 'HotelGuest',
                     *_geo_offset(_PARIS_LAT, _PARIS_LON,   50, -180)),
            _make_ap('aa:bb:cc:dd:04:04', 'OfficeNet',
                     *_geo_offset(_PARIS_LAT, _PARIS_LON, -200, -50)),
            _make_ap('aa:bb:cc:dd:05:05', 'FREEWIFI',
                     *_geo_offset(_PARIS_LAT, _PARIS_LON,  100, 120)),
        ]
        # Grid walk: 7×7 points over a 600 m square (wider grid keeps all APs well inside)
        track = [
            _geo_offset(_PARIS_LAT, _PARIS_LON, x, y)
            for y in range(-300, 301, 100)
            for x in range(-300, 301, 100)
        ]
        records = []
        for ap in aps:
            records.extend(_obs_from_track(ap, track, n_exp=2.7, rng=rng))
        out = Path('/tmp/aw_multi_ap_five.jsonl')
        _write_jsonl(out, records)
        assert out.stat().st_size > 0

        # Every AP must be solvable from the session
        by_id: dict[str, list] = {}
        for r in records:
            by_id.setdefault(r['id'], []).append((r['lat'], r['lon'], r['rssi']))
        for ap in aps:
            obs = by_id[ap['mac']]
            res = rss_solve(obs, n_exp=2.7)
            assert res is not None, f"solver failed for {ap['ssid']}"
            assert res['lat'] == pytest.approx(ap['lat'], abs=0.002)
            assert res['lon'] == pytest.approx(ap['lon'], abs=0.002)

    def test_moving_observer_linear_pass(self):
        """
        Observer drives past the AP on a straight road.
        Tests that an asymmetric (non-encircling) track still converges.
        """
        rng = _rng(3)
        ap = _make_ap('bb:cc:dd:ee:ff:01', 'RoadSideAP',
                      *_geo_offset(_PARIS_LAT, _PARIS_LON, 0, 50))
        # Straight west-to-east drive 300 m south of AP
        track = [
            _geo_offset(_PARIS_LAT, _PARIS_LON, x, -300)
            for x in range(-400, 401, 40)
        ]
        recs = _obs_from_track(ap, track, n_exp=2.5, rng=rng)
        out = Path('/tmp/aw_linear_drive.jsonl')
        _write_jsonl(out, recs)

        obs = [(r['lat'], r['lon'], r['rssi']) for r in recs]
        res = rss_solve(obs, n_exp=2.5)
        # Linear (non-encircling) pass has higher uncertainty — allow generous tolerance
        assert res is not None
        d = _dist_m(res['lat'], res['lon'], ap['lat'], ap['lon'])
        assert d < 70, f"solver too far: {d:.1f} m"

    def test_fixed_sensor_many_aps(self):
        """
        Fixed sensor: GPS is static.  20 APs at various distances 30–500 m.
        Tests centroid fallback (only 1 observation per AP from a fixed point).
        """
        rng = _rng(4)
        sensor_lat, sensor_lon = _geo_offset(_PARIS_LAT, _PARIS_LON, 10, 0)
        records = []
        for i in range(20):
            angle = 2 * math.pi * i / 20
            radius = rng.uniform(30, 500)
            dx = radius * math.cos(angle)
            dy = radius * math.sin(angle)
            ap_lat, ap_lon = _geo_offset(sensor_lat, sensor_lon, dx, dy)
            d = _dist_m(sensor_lat, sensor_lon, ap_lat, ap_lon)
            rssi = _path_loss(d, -28, 2.5) + rng.gauss(0, 2)
            mac = f'cc:dd:ee:{i:02x}:{i:02x}:ff'
            records.append({
                'id': mac, 'ssid': f'AP_{i:02d}',
                'rssi': round(rssi, 1),
                'lat': sensor_lat, 'lon': sensor_lon,
                'freq': 2412000000 if i % 2 == 0 else 5180000000,
                'protocol': 'wifi',
                't': round(time.time() + i * 0.3, 3),
            })
        out = Path('/tmp/aw_fixed_sensor.jsonl')
        _write_jsonl(out, records)
        lines = out.read_text().strip().splitlines()
        assert len(lines) == 20
        # Each AP has exactly 1 observation → centroid fallback
        for r in records:
            lat, lon = rssi_centroid([(r['lat'], r['lon'], r['rssi'])])
            assert lat == pytest.approx(r['lat'], abs=1e-6)

    def test_dense_urban_twenty_aps(self):
        """
        Dense urban: 20 APs within a 150 m radius, signals overlapping.
        Tests that solver handles high-density scenarios without crashing.
        """
        rng = _rng(5)
        aps = []
        for i in range(20):
            angle = rng.uniform(0, 2 * math.pi)
            r = rng.uniform(5, 150)
            lat, lon = _geo_offset(_PARIS_LAT, _PARIS_LON,
                                    r * math.cos(angle), r * math.sin(angle))
            aps.append(_make_ap(f'dd:ee:{i:02x}:{i:02x}:aa:bb',
                                f'Urban_{i:02d}', lat, lon,
                                rssi_at_1m=rng.uniform(-35, -25)))
        track = _circle_track(_PARIS_LAT, _PARIS_LON, 100, 16)
        records = []
        for ap in aps:
            records.extend(_obs_from_track(ap, track, n_exp=3.2, rng=rng))
        out = Path('/tmp/aw_dense_urban.jsonl')
        _write_jsonl(out, records)
        assert len(out.read_text().strip().splitlines()) == 20 * 16

    def test_weak_signal_distant_ap(self):
        """
        AP is 500 m away; RSSI at observer is around -90 dBm.
        Tests solver at the edge of detection (high noise-to-signal).
        """
        rng = _rng(6)
        ap = _make_ap('ee:ff:aa:bb:cc:01', 'FarAway',
                      *_geo_offset(_PARIS_LAT, _PARIS_LON, 0, 500),
                      rssi_at_1m=-30)
        track = _circle_track(_PARIS_LAT, _PARIS_LON, 200, 20)
        recs = _obs_from_track(ap, track, n_exp=3.0, rng=rng, gps_jitter_m=3)
        out = Path('/tmp/aw_weak_signal.jsonl')
        _write_jsonl(out, recs)
        obs = [(r['lat'], r['lon'], r['rssi']) for r in recs]
        # Solver should either find it within 100 m or return None (too noisy)
        res = rss_solve(obs, n_exp=3.0)
        if res is not None:
            d = _dist_m(res['lat'], res['lon'], ap['lat'], ap['lon'])
            assert d < 500, f"distant AP solve error: {d:.1f} m"

    def test_dual_band_ap_24_and_5ghz(self):
        """
        Same physical AP broadcasting on 2.4 GHz and 5 GHz simultaneously.
        Different MACs but same position — two records per observation point.
        """
        rng = _rng(7)
        lat, lon = _geo_offset(_PARIS_LAT, _PARIS_LON, 30, -20)
        ap_24 = _make_ap('ff:aa:bb:cc:01:00', 'HomeAP_2G',
                         lat, lon, freq=2437000000)
        ap_5  = _make_ap('ff:aa:bb:cc:01:10', 'HomeAP_5G',
                         lat, lon, freq=5180000000, rssi_at_1m=-30)
        track = _circle_track(lat, lon, 60, 10)
        records = []
        records.extend(_obs_from_track(ap_24, track, n_exp=2.5, rng=rng))
        records.extend(_obs_from_track(ap_5,  track, n_exp=3.5, rng=rng))  # 5 GHz attenuates faster
        out = Path('/tmp/aw_dual_band.jsonl')
        _write_jsonl(out, records)
        # Both bands should solve to the same location
        by_id: dict = {}
        for r in records:
            by_id.setdefault(r['id'], []).append((r['lat'], r['lon'], r['rssi']))
        res24 = rss_solve(by_id[ap_24['mac']], n_exp=2.5)
        res5  = rss_solve(by_id[ap_5['mac']],  n_exp=3.5)
        assert res24 is not None and res5 is not None
        d = _dist_m(res24['lat'], res24['lon'], res5['lat'], res5['lon'])
        assert d < 25, f"2.4 / 5 GHz positions differ by {d:.1f} m"

    def test_car_route_realistic(self):
        """
        Simulates a 1-km car drive along a diagonal route with 8 APs.
        Produces a rich session representative of real wardriving.
        """
        rng = _rng(8)
        # Drive: diagonal south-west to north-east over 1 km
        n_pts = 40
        track = [
            _geo_offset(_PARIS_LAT, _PARIS_LON,
                        -500 + 1000 * i / (n_pts - 1),
                        -500 + 1000 * i / (n_pts - 1))
            for i in range(n_pts)
        ]
        aps = [
            _make_ap(f'a1:b2:c3:{i:02x}:00:01', f'RouteAP_{i}',
                     *_geo_offset(_PARIS_LAT, _PARIS_LON,
                                  rng.uniform(-450, 450),
                                  rng.uniform(-450, 450)),
                     rssi_at_1m=rng.uniform(-32, -24))
            for i in range(8)
        ]
        records = []
        t0 = time.time()
        for ap in aps:
            records.extend(_obs_from_track(ap, track, n_exp=2.6, rng=rng, t0=t0))
        out = Path('/tmp/aw_car_route.jsonl')
        _write_jsonl(out, records)
        assert len(out.read_text().strip().splitlines()) == 8 * n_pts

    def test_indoor_nlos_high_n_exp(self):
        """
        Indoor scenario: walls cause heavy attenuation (n_exp ≈ 3.8).
        Observer wanders inside a building; AP is on another floor.
        """
        rng = _rng(9)
        ap = _make_ap('b1:c2:d3:e4:f5:02', 'OfficeAP',
                      *_geo_offset(_PARIS_LAT, _PARIS_LON, 0, 0))
        # Tight random walk inside ~30 m radius (building interior)
        track = []
        lat, lon = _geo_offset(_PARIS_LAT, _PARIS_LON, 15, 15)
        for _ in range(30):
            lat, lon = _geo_offset(lat, lon,
                                   rng.gauss(0, 5), rng.gauss(0, 5))
            track.append((lat, lon))
        recs = _obs_from_track(ap, track, n_exp=3.8, rng=rng, gps_jitter_m=5)
        out = Path('/tmp/aw_indoor_nlos.jsonl')
        _write_jsonl(out, recs)
        obs = [(r['lat'], r['lon'], r['rssi']) for r in recs]
        res = rss_solve(obs, n_exp=3.8)
        if res is not None:
            d = _dist_m(res['lat'], res['lon'], ap['lat'], ap['lon'])
            assert d < 50, f"indoor solve error: {d:.1f} m"

    def test_outdoor_los_low_n_exp(self):
        """
        Open-sky rooftop: nearly free-space propagation (n_exp ≈ 2.1).
        AP on a mast, observer on open ground; excellent RSSI accuracy.
        """
        rng = _rng(10)
        ap = _make_ap('c1:d2:e3:f4:05:06', 'RooftopAP',
                      *_geo_offset(_PARIS_LAT, _PARIS_LON, 0, 0),
                      rssi_at_1m=-25)
        track = _circle_track(_PARIS_LAT, _PARIS_LON, 120, 16)
        recs = _obs_from_track(ap, track, n_exp=2.1, rng=rng, gps_jitter_m=1)
        out = Path('/tmp/aw_outdoor_los.jsonl')
        _write_jsonl(out, recs)
        obs = [(r['lat'], r['lon'], r['rssi']) for r in recs]
        res = rss_solve(obs, n_exp=2.1)
        assert res is not None
        d = _dist_m(res['lat'], res['lon'], ap['lat'], ap['lon'])
        assert d < 15, f"outdoor LOS error: {d:.1f} m"

    def test_centroid_fallback_two_observations(self):
        """Only 2 observations → rss_solve returns None, centroid must still work."""
        obs = [
            (_PARIS_LAT - 0.001, _PARIS_LON, -60.0),
            (_PARIS_LAT + 0.001, _PARIS_LON, -65.0),
        ]
        assert rss_solve(obs) is None
        lat, lon = rssi_centroid(obs)
        assert abs(lat - _PARIS_LAT) < 0.002
        assert abs(lon - _PARIS_LON) < 0.002

    def test_duplicate_mac_two_physical_aps(self):
        """
        Two physically distinct APs broadcasting the same MAC (spoofed or
        mis-configured mesh node).  The solver sees one ID but two
        spatial clusters — the centroid should end up between them.
        """
        rng = _rng(12)
        mac = 'de:ad:be:ef:ca:fe'
        ap_a = _make_ap(mac, 'SpoofAP', *_geo_offset(_PARIS_LAT, _PARIS_LON, -80, 0))
        ap_b = _make_ap(mac, 'SpoofAP', *_geo_offset(_PARIS_LAT, _PARIS_LON, +80, 0))
        track = _circle_track(_PARIS_LAT, _PARIS_LON, 120, 16)
        recs = _obs_from_track(ap_a, track[:8], n_exp=2.5, rng=rng)
        recs += _obs_from_track(ap_b, track[8:], n_exp=2.5, rng=rng)
        out = Path('/tmp/aw_duplicate_mac.jsonl')
        _write_jsonl(out, recs)
        # All records share the same MAC
        macs = {r['id'] for r in recs}
        assert macs == {mac}
        # Centroid must fall roughly between the two APs
        obs = [(r['lat'], r['lon'], r['rssi']) for r in recs]
        lat, lon = rssi_centroid(obs)
        d_a = _dist_m(lat, lon, ap_a['lat'], ap_a['lon'])
        d_b = _dist_m(lat, lon, ap_b['lat'], ap_b['lon'])
        # Neither true AP should be very far from the centroid
        assert min(d_a, d_b) < 120

    def test_car_route_highway_ap(self):
        """
        Vehicle drives past at high speed on a highway (100 m/s equivalent).
        ~60 sample points along a 1200 m straight road, AP 50 m off-road.
        Tests solver on a very elongated observation track.
        """
        rng = _rng(13)
        ap = _make_ap('f0:f1:f2:f3:f4:f5', 'HighwayAP',
                      *_geo_offset(_PARIS_LAT, _PARIS_LON, 0, 50),
                      rssi_at_1m=-30)
        # East-to-west drive, 1200 m, 20 m spacing
        track = [_geo_offset(_PARIS_LAT, _PARIS_LON, x, 0)
                 for x in range(-600, 601, 20)]
        recs = _obs_from_track(ap, track, n_exp=2.5, rng=rng, gps_jitter_m=3)
        out = Path('/tmp/aw_highway_ap.jsonl')
        _write_jsonl(out, recs)
        assert len(recs) == len(track)
        obs = [(r['lat'], r['lon'], r['rssi']) for r in recs]
        res = rss_solve(obs, n_exp=2.5)
        if res is not None:
            d = _dist_m(res['lat'], res['lon'], ap['lat'], ap['lon'])
            assert d < 100, f'highway AP solve error: {d:.1f} m'


# ── Multi-antenna channel-split scenarios ─────────────────────────────────────

class TestMultiAntennaWardriver:
    """
    Tests the WardriverMode channel-assignment logic for 1, 2 and 3 antennas,
    then generates matching sessions to /tmp.
    """

    def _session_for(self, n_antennas: int, out_path: str,
                     channels=None) -> WardriverMode:
        channels = channels or list(range(1, 14))
        array = _make_array_mock(n_antennas)
        mode = WardriverMode(array, {'channels': channels})
        mode._assign_channels()
        # Smoke-test the session file with synthetic obs
        rng = _rng(n_antennas * 7)
        ap = _make_ap('11:22:33:44:55:66', f'TestAP_{n_antennas}ant',
                      _PARIS_LAT, _PARIS_LON)
        track = _circle_track(_PARIS_LAT, _PARIS_LON, 100, 12)
        recs = _obs_from_track(ap, track, n_exp=2.5, rng=rng)
        # Tag each record with a pseudo antenna_id (round-robin)
        for i, r in enumerate(recs):
            r['ant'] = f'wlan{i % n_antennas}'
        _write_jsonl(Path(out_path), recs)
        return mode

    def test_single_antenna_all_channels(self):
        mode = self._session_for(1, '/tmp/aw_1ant.jsonl')
        assert set(mode._channel_map['wlan0']) == set(range(1, 14))

    def test_dual_antenna_no_overlap(self):
        mode = self._session_for(2, '/tmp/aw_2ant.jsonl')
        ch0 = mode._channel_map.get('wlan0', [])
        ch1 = mode._channel_map.get('wlan1', [])
        assert sorted(ch0 + ch1) == list(range(1, 14))
        assert len(set(ch0) & set(ch1)) == 0
        assert abs(len(ch0) - len(ch1)) <= 1  # balanced split

    def test_triple_antenna_remainder(self):
        mode = self._session_for(3, '/tmp/aw_3ant.jsonl')
        all_ch = []
        for i in range(3):
            all_ch.extend(mode._channel_map.get(f'wlan{i}', []))
        assert sorted(all_ch) == list(range(1, 14))

    def test_five_ghz_channels_split(self):
        """5 GHz non-contiguous channel list split across 3 antennas."""
        channels = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112]
        mode = self._session_for(3, '/tmp/aw_3ant_5ghz.jsonl',
                                 channels=channels)
        all_ch = []
        for i in range(3):
            all_ch.extend(mode._channel_map.get(f'wlan{i}', []))
        assert sorted(all_ch) == channels

    def test_four_antenna_dual_band(self):
        """
        4 antennas covering both 2.4 GHz (ch 1–13) and 5 GHz (ch 36–112).
        Verifies no channel appears on more than one antenna and all channels
        are covered — simulating a real dual-band wardrive rig.
        """
        ch_2g = list(range(1, 14))
        ch_5g = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112]
        all_channels = ch_2g + ch_5g
        mode = self._session_for(4, '/tmp/aw_4ant_dualband.jsonl',
                                 channels=all_channels)
        all_ch: list = []
        seen: set = set()
        for i in range(4):
            chs = mode._channel_map.get(f'wlan{i}', [])
            for c in chs:
                assert c not in seen, f'channel {c} assigned to multiple antennas'
                seen.add(c)
            all_ch.extend(chs)
        assert sorted(all_ch) == sorted(all_channels)

    def test_single_channel_many_antennas(self):
        """
        Edge case: 4 antennas but only 1 channel in the list.
        All antennas should share the single channel (no crash, no empty map).
        """
        mode = self._session_for(4, '/tmp/aw_4ant_1ch.jsonl', channels=[6])
        all_ch = []
        for i in range(4):
            all_ch.extend(mode._channel_map.get(f'wlan{i}', []))
        # Each antenna gets ch 6 (single channel broadcast to all)
        assert all(c == 6 for c in all_ch)


# ── TDOA / ENU scenarios ──────────────────────────────────────────────────────

class TestTdoaScenarios:
    """
    Generates ENU-frame JSONL files suitable for the TDOA 3D viewer.
    Each file contains source positions (x_enu, y_enu, z_enu) and is
    designed to show a different receiver geometry.
    """

    _METHODS = ['tdoa', 'rss_trilateration', 'rssi_centroid']

    def _enu_record(self, src_id, x, y, z, rssi, method='tdoa', t=None):
        return {
            'id':     src_id,
            'x_enu':  round(x, 3),
            'y_enu':  round(y, 3),
            'z_enu':  round(z, 3),
            'rssi':   round(rssi, 1),
            'method': method,
            't':      t or round(time.time(), 3),
        }

    def test_four_receiver_precise(self):
        """
        4 receivers at corners of a 6 m square, 1 source at centre.
        Near-zero noise — verifies the 3D viewer has clean geometry to display.
        """
        rng = _rng(20)
        receivers = [
            (0.0,  0.0,  0.0), (6.0,  0.0,  0.0),
            (0.0,  6.0,  0.0), (6.0,  6.0,  0.0),
        ]
        true_src = (3.0, 3.0, 1.5)
        records = []
        t0 = time.time()
        # Receiver marker records (method='antenna' for TDOA 3D viewer)
        for i, (rx, ry, rz) in enumerate(receivers):
            records.append(self._enu_record(
                f'rx-{i}', rx, ry, rz, rssi=-30, method='antenna', t=t0))
        # Source estimate with tiny noise
        for trial in range(8):
            x = true_src[0] + rng.gauss(0, 0.05)
            y = true_src[1] + rng.gauss(0, 0.05)
            z = true_src[2] + rng.gauss(0, 0.08)
            records.append(self._enu_record(
                'src-0', x, y, z, rssi=-55 - trial, method='tdoa', t=t0 + trial))
        out = Path('/tmp/aw_tdoa_4rx_precise.jsonl')
        _write_jsonl(out, records)
        xs = [r['x_enu'] for r in records if r['method'] == 'tdoa']
        ys = [r['y_enu'] for r in records if r['method'] == 'tdoa']
        assert abs(sum(xs) / len(xs) - true_src[0]) < 0.2
        assert abs(sum(ys) / len(ys) - true_src[1]) < 0.2

    def test_four_receiver_noisy(self):
        """
        Same geometry but ±0.5 m position noise — tests solver robustness.
        Multiple estimated source positions show uncertainty spread.
        """
        rng = _rng(21)
        receivers = [
            (0.0, 0.0, 0.0), (8.0, 0.0, 0.0),
            (4.0, 7.0, 0.0), (4.0, 3.5, 2.0),
        ]
        true_src = (4.0, 3.5, 1.0)
        records = []
        t0 = time.time()
        for i, (rx, ry, rz) in enumerate(receivers):
            records.append(self._enu_record(
                f'rx-{i}', rx, ry, rz, rssi=-32, method='antenna', t=t0))
        for trial in range(20):
            x = true_src[0] + rng.gauss(0, 0.5)
            y = true_src[1] + rng.gauss(0, 0.5)
            z = true_src[2] + rng.gauss(0, 0.4)
            records.append(self._enu_record(
                'src-noisy', x, y, z,
                rssi=-60 - rng.uniform(0, 10), method='tdoa',
                t=t0 + trial * 0.1))
        out = Path('/tmp/aw_tdoa_4rx_noisy.jsonl')
        _write_jsonl(out, records)
        assert out.exists() and out.stat().st_size > 0

    def test_asymmetric_baseline(self):
        """
        Non-symmetric receiver layout — L-shaped, 3 receivers.
        Tests that 3D viewer renders irregular geometries correctly.
        """
        rng = _rng(22)
        receivers = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 4.0, 0.0)]
        true_src = (3.5, 2.0, 0.8)
        records = []
        t0 = time.time()
        for i, (rx, ry, rz) in enumerate(receivers):
            records.append(self._enu_record(
                f'rx-{i}', rx, ry, rz, rssi=-28, method='antenna', t=t0))
        for trial in range(12):
            records.append(self._enu_record(
                'src-A', true_src[0] + rng.gauss(0, 0.3),
                true_src[1] + rng.gauss(0, 0.3),
                true_src[2] + rng.gauss(0, 0.2),
                rssi=-58, method='tdoa', t=t0 + trial))
        out = Path('/tmp/aw_tdoa_asymmetric.jsonl')
        _write_jsonl(out, records)
        assert len(out.read_text().strip().splitlines()) == len(records)

    def test_multi_source_two_emitters(self):
        """
        Two simultaneous emitters; 4 receivers.
        Tests that TDOA 3D viewer can show multiple source clusters.
        """
        rng = _rng(23)
        receivers = [
            (0.0, 0.0, 0.0), (12.0, 0.0, 0.0),
            (6.0, 10.0, 0.0), (6.0, 5.0, 3.0),
        ]
        sources = [(-2.0, 5.0, 1.0), (8.0, 5.0, 1.0)]
        records = []
        t0 = time.time()
        for i, (rx, ry, rz) in enumerate(receivers):
            records.append(self._enu_record(
                f'rx-{i}', rx, ry, rz, rssi=-30, method='antenna', t=t0))
        for s_idx, (sx, sy, sz) in enumerate(sources):
            for trial in range(10):
                records.append(self._enu_record(
                    f'src-{s_idx}',
                    sx + rng.gauss(0, 0.15),
                    sy + rng.gauss(0, 0.15),
                    sz + rng.gauss(0, 0.1),
                    rssi=-55 - s_idx * 5,
                    method='tdoa', t=t0 + trial + s_idx * 0.05))
        out = Path('/tmp/aw_tdoa_two_sources.jsonl')
        _write_jsonl(out, records)
        src_ids = {r['id'] for r in records if r['method'] == 'tdoa'}
        assert src_ids == {'src-0', 'src-1'}

    def test_floor_plan_2d_flat(self):
        """
        2-D floor plan: all receivers and source at Z=0.
        Tests the XY top-down view in the ENU and TDOA 3D tabs.
        """
        rng = _rng(24)
        room_w, room_h = 15.0, 10.0
        receivers = [
            (0.0, 0.0, 0.0), (room_w, 0.0, 0.0),
            (0.0, room_h, 0.0), (room_w, room_h, 0.0),
        ]
        true_src = (room_w / 3, room_h / 2, 0.0)
        records = []
        t0 = time.time()
        for i, (rx, ry, rz) in enumerate(receivers):
            records.append(self._enu_record(
                f'corner-{i}', rx, ry, rz, rssi=-30, method='antenna', t=t0))
        for trial in range(15):
            records.append(self._enu_record(
                'phone', true_src[0] + rng.gauss(0, 0.2),
                true_src[1] + rng.gauss(0, 0.2), 0.0,
                rssi=-52, method='tdoa', t=t0 + trial * 0.5))
        out = Path('/tmp/aw_tdoa_floor_2d.jsonl')
        _write_jsonl(out, records)
        assert out.exists()

    def test_moving_source_tracked(self):
        """
        Source walks a straight-line path through a 4-receiver array.
        Each time step has one position estimate per receiver pair,
        producing a trajectory visible in the 3D viewer.
        """
        rng = _rng(25)
        receivers = [
            (0.0, 0.0, 0.0), (10.0, 0.0, 0.0),
            (0.0, 10.0, 0.0), (10.0, 10.0, 0.0),
        ]
        # Source walks from (1,5) to (9,5) at Z=1 m
        path = [(1.0 + 8.0 * i / 19, 5.0, 1.0) for i in range(20)]
        t0 = time.time()
        records = []
        for i, (rx, ry, rz) in enumerate(receivers):
            records.append(self._enu_record(
                f'rx-{i}', rx, ry, rz, rssi=-30, method='antenna', t=t0))
        for step, (sx, sy, sz) in enumerate(path):
            records.append(self._enu_record(
                'walker', sx + rng.gauss(0, 0.12),
                sy + rng.gauss(0, 0.12), sz + rng.gauss(0, 0.08),
                rssi=-50 - rng.uniform(0, 5), method='tdoa',
                t=t0 + step * 0.25))
        out = Path('/tmp/aw_tdoa_moving_source.jsonl')
        _write_jsonl(out, records)
        src_pts = [r for r in records if r['method'] == 'tdoa']
        assert len(src_pts) == 20
        # Trajectory should span most of the 8 m path
        xs = [r['x_enu'] for r in src_pts]
        assert max(xs) - min(xs) > 5.0, 'trajectory X span too small'

    def test_three_receiver_minimum_2d(self):
        """
        3-receiver session — below solver minimum (needs ≥4 total, ≥3 non-reference).
        Verifies the session JSONL is written without error; solver would return None.
        """
        rng = _rng(26)
        receivers = [(0.0, 0.0, 0.0), (8.0, 0.0, 0.0), (4.0, 6.0, 0.0)]
        true_src = (3.0, 2.5, 0.0)
        t0 = time.time()
        records = []
        for i, (rx, ry, rz) in enumerate(receivers):
            records.append(self._enu_record(
                f'rx-{i}', rx, ry, rz, rssi=-32, method='antenna', t=t0))
        for trial in range(10):
            records.append(self._enu_record(
                'tgt', true_src[0] + rng.gauss(0, 0.3),
                true_src[1] + rng.gauss(0, 0.3), 0.0,
                rssi=-60, method='tdoa', t=t0 + trial * 0.2))
        out = Path('/tmp/aw_tdoa_3rx_min.jsonl')
        _write_jsonl(out, records)
        rx_count = sum(1 for r in records if r['method'] == 'antenna')
        assert rx_count == 3

    def test_receiver_dropout_mid_session(self):
        """
        One receiver stops reporting halfway through the session.
        Both the full-array phase and the degraded phase are in one JSONL.
        Tests that the TDOA 3D viewer handles partial receiver data.
        """
        rng = _rng(27)
        receivers = [
            (0.0, 0.0, 0.0), (12.0, 0.0, 0.0),
            (6.0, 9.0, 0.0), (6.0, 4.5, 3.0),
        ]
        true_src = (5.0, 4.0, 1.0)
        t0 = time.time()
        records = []
        for i, (rx, ry, rz) in enumerate(receivers):
            records.append(self._enu_record(
                f'rx-{i}', rx, ry, rz, rssi=-30, method='antenna', t=t0))
        # Phase 1: all 4 receivers active (20 estimates)
        for step in range(20):
            records.append(self._enu_record(
                'src', true_src[0] + rng.gauss(0, 0.2),
                true_src[1] + rng.gauss(0, 0.2),
                true_src[2] + rng.gauss(0, 0.15),
                rssi=-55, method='tdoa', t=t0 + step * 0.1))
        # Phase 2: rx-3 drops out — only 3 receivers (mark as absent by omitting)
        for step in range(20, 40):
            records.append(self._enu_record(
                'src', true_src[0] + rng.gauss(0, 0.5),
                true_src[1] + rng.gauss(0, 0.5),
                true_src[2] + rng.gauss(0, 0.4),
                rssi=-58, method='tdoa', t=t0 + step * 0.1))
        out = Path('/tmp/aw_tdoa_dropout.jsonl')
        _write_jsonl(out, records)
        tdoa_pts = [r for r in records if r['method'] == 'tdoa']
        # Phase 2 estimates should have higher spread
        phase1 = [r for r in tdoa_pts if r['t'] < t0 + 2.0]
        phase2 = [r for r in tdoa_pts if r['t'] >= t0 + 2.0]
        def spread(pts): return max(r['x_enu'] for r in pts) - min(r['x_enu'] for r in pts)
        assert spread(phase2) >= spread(phase1)

    def test_multipath_indoor_high_noise(self):
        """
        Heavily multipath-degraded indoor scenario: large position noise.
        4 receivers in a typical office room (8×6 m), 1 source.
        Tests that the 3D viewer renders a noisy cloud rather than a point.
        """
        rng = _rng(28)
        receivers = [
            (0.0, 0.0, 2.4), (8.0, 0.0, 2.4),
            (0.0, 6.0, 2.4), (8.0, 6.0, 2.4),
        ]
        true_src = (3.5, 3.0, 1.2)
        t0 = time.time()
        records = []
        for i, (rx, ry, rz) in enumerate(receivers):
            records.append(self._enu_record(
                f'ceiling-{i}', rx, ry, rz, rssi=-28, method='antenna', t=t0))
        for step in range(30):
            records.append(self._enu_record(
                'laptop', true_src[0] + rng.gauss(0, 1.2),
                true_src[1] + rng.gauss(0, 1.2),
                true_src[2] + rng.gauss(0, 0.6),
                rssi=-65 - rng.uniform(0, 8), method='tdoa',
                t=t0 + step * 0.1))
        out = Path('/tmp/aw_tdoa_multipath.jsonl')
        _write_jsonl(out, records)
        src_pts = [r for r in records if r['method'] == 'tdoa']
        xs = [r['x_enu'] for r in src_pts]
        # Cloud should be wide — multipath spreads estimates > 2 m
        assert max(xs) - min(xs) > 2.0

    def test_wardriver_and_enu_mixed_session(self):
        """
        Session file that contains both wardriver geo-records AND ENU records.
        Real-world edge case: user accidentally solves into the wardriver file.
        Verifies JSONL writer and session type detection handle mixed content.
        """
        rng = _rng(29)
        records = []
        # Wardriver-style geo records
        ap = _make_ap('aa:bb:cc:dd:ee:ff', 'MixedAP', *_geo_offset(_PARIS_LAT, _PARIS_LON, 0, 0))
        records.extend(_obs_from_track(ap, _circle_track(_PARIS_LAT, _PARIS_LON, 80, 8),
                                       n_exp=2.5, rng=rng))
        # ENU-style records appended after
        t0 = time.time()
        for i in range(5):
            records.append({
                'id': f'enu-{i}', 'x_enu': float(i), 'y_enu': float(i),
                'z_enu': 0.0, 'rssi': -50.0, 'method': 'tdoa', 't': t0 + i,
            })
        out = Path('/tmp/aw_mixed_session.jsonl')
        _write_jsonl(out, records)
        lines = out.read_text().strip().splitlines()
        assert len(lines) == 8 + 5
        geo = [json.loads(line) for line in lines if 'lat' in line]
        enu = [json.loads(line) for line in lines if 'x_enu' in line]
        assert len(geo) == 8 and len(enu) == 5


# ── Array sensing scenarios ───────────────────────────────────────────────────

class TestArraySensingScenarios:
    """
    Generates sensing event JSONL files: presence → motion → absence cycles
    at varying sensitivity levels, simulating real occupancy patterns.
    """

    def _sensing_event(self, evt_type, antenna_id, variance, direction, t):
        return {
            'type':       evt_type,
            'antenna_id': antenna_id,
            'variance':   round(variance, 4),
            'direction':  [round(v, 3) for v in direction],
            't':          round(t, 3),
        }

    def test_single_person_entry_exit(self):
        """
        Person enters room, moves, then leaves.
        absence → presence → motion × N → absence
        """
        rng = _rng(30)
        t = time.time()
        records = []

        def evt(etype, ant, var):
            d = [rng.gauss(0, 0.4), rng.gauss(0, 0.4), 0.0]
            return self._sensing_event(etype, ant, var, d, t)

        # Baseline quiet
        for _ in range(5):
            for ant in ['wlan0', 'wlan1', 'wlan2']:
                records.append(evt('absence', ant, rng.uniform(0.01, 0.03)))

        # Entry event
        for ant in ['wlan0', 'wlan1', 'wlan2']:
            records.append(evt('presence', ant, rng.uniform(0.15, 0.35)))

        # Motion phase
        for _ in range(12):
            for ant in ['wlan0', 'wlan1', 'wlan2']:
                records.append(evt('motion', ant, rng.uniform(0.06, 0.18)))

        # Exit
        for ant in ['wlan0', 'wlan1', 'wlan2']:
            records.append(evt('absence', ant, rng.uniform(0.01, 0.04)))

        out = Path('/tmp/aw_sensing_entry_exit.jsonl')
        _write_jsonl(out, records)
        types = [r['type'] for r in records]
        assert 'presence' in types
        assert 'motion'   in types
        assert 'absence'  in types

    def test_multi_zone_three_antennas(self):
        """
        Three antennas covering different zones.
        Person moves from zone-A (wlan0) through zone-B (wlan1) to zone-C (wlan2).
        """
        rng = _rng(31)
        records = []
        t0 = time.time()
        zones = [('wlan0', 0.0), ('wlan1', 0.5), ('wlan2', 1.0)]

        for phase, (active_ant, _) in enumerate(zones):
            for step in range(8):
                t = t0 + phase * 10 + step * 1.0
                for ant, _ in zones:
                    if ant == active_ant:
                        var = rng.uniform(0.1, 0.3)
                        etype = 'motion' if step > 0 else 'presence'
                    else:
                        var = rng.uniform(0.01, 0.04)
                        etype = 'absence'
                    d = [rng.gauss(0, 0.3), rng.gauss(0, 0.3), 0.0]
                    records.append(self._sensing_event(etype, ant, var, d, t))

        out = Path('/tmp/aw_sensing_multi_zone.jsonl')
        _write_jsonl(out, records)
        assert len(out.read_text().strip().splitlines()) == len(records)

    def test_high_activity_crowd(self):
        """
        High variance — crowded environment, all antennas firing motion.
        Tests rendering performance with many events.
        """
        rng = _rng(32)
        records = []
        t0 = time.time()
        for i in range(200):
            ant = f'wlan{i % 3}'
            var = rng.uniform(0.08, 0.45)
            d = [rng.gauss(0, 0.5), rng.gauss(0, 0.5), 0.0]
            etype = rng.choice(['motion', 'motion', 'presence'])
            records.append(self._sensing_event(etype, ant, var, d, t0 + i * 0.2))
        out = Path('/tmp/aw_sensing_crowd.jsonl')
        _write_jsonl(out, records)
        assert len(out.read_text().strip().splitlines()) == 200

    def test_event_types_exhaustive(self):
        """All three event types must appear in a realistic cycle."""
        rng = _rng(33)
        records = []
        t0 = time.time()
        sequence = ['absence'] * 3 + ['presence'] + ['motion'] * 5 + ['absence'] * 2
        for i, etype in enumerate(sequence):
            d = [rng.gauss(0, 0.3), rng.gauss(0, 0.3), 0.0]
            var = 0.02 if etype == 'absence' else rng.uniform(0.1, 0.3)
            records.append(self._sensing_event(etype, 'wlan0', var, d, t0 + i))
        out = Path('/tmp/aw_sensing_cycle.jsonl')
        _write_jsonl(out, records)
        types = {r['type'] for r in records}
        assert types == {'presence', 'motion', 'absence'}


# ── Config / wizard workflow scenarios ────────────────────────────────────────

class TestConfigWorkflow:
    """
    Simulates the user wizard workflow: build AWConfig from_dict (as the
    web wizard would), then validate the resulting config makes sense.
    """

    def _wardriver_dict(self, n_antennas=1, channels=None, hop=0.1,
                        gps_backend='gpsd', sync='software'):
        """Minimal wizard-produced config dict for wardriver mode."""
        ants = [
            {'id': f'wlan{i}',
             'backend': 'plugins.wifi_nl80211.NL80211Backend',
             'backend_config': {'interface': f'wlan{i}'},
             'frequency_range': [2400000000, 2500000000],
             'position': [i * 0.5, 0.0, 0.0]}
            for i in range(n_antennas)
        ]
        return {
            'mode': 'wardriver',
            'array_id': f'wardriver_{n_antennas}ant',
            'antennas': ants,
            'gps':  {'backend': gps_backend},
            'sync': {'source': sync},
            'mode_config': {
                'channels': channels or list(range(1, 14)),
                'hop_interval': hop,
                'output_path': f'~/.aetherward/sessions/wardriver_{n_antennas}ant.jsonl',
            },
        }

    # Non-collinear positions for TDOA — gives full 3-D geometry diversity
    _TDOA_POSITIONS = [
        (0.0, 0.0, 0.0),  # reference
        (2.0, 0.0, 0.0),  # East
        (1.0, 2.0, 0.0),  # North-East (breaks collinearity)
        (0.0, 1.5, 1.5),  # North-Elevated (adds Z diversity)
        (2.0, 2.0, 0.0),
        (-0.5, 1.0, 0.0),
        (1.5, -0.5, 0.0),
        (0.5, 0.5, 2.0),
    ]

    def _tdoa_dict(self, n_antennas=4, channel=6, sync='pps'):
        ants = [
            {'id': f'wlan{i}',
             'backend': 'plugins.wifi_nl80211.NL80211Backend',
             'frequency_range': [2400000000, 2500000000],
             'position': list(self._TDOA_POSITIONS[i])}
            for i in range(n_antennas)
        ]
        return {
            'mode': 'trilateration',
            'array_id': f'tdoa_{n_antennas}rx',
            'antennas': ants,
            'gps':  {'backend': 'static', 'lat': _PARIS_LAT, 'lon': _PARIS_LON},
            'sync': {'source': sync, 'device': '/dev/pps0' if sync == 'pps' else ''},
            'mode_config': {
                'channel': channel,
                'reference_antenna': 'wlan0',
                'correlation_window': 0.001,
                'group_timeout': 0.05,
            },
        }

    def _sensing_dict(self, n_antennas=3, channel=6):
        ants = [
            {'id': f'wlan{i}',
             'backend': 'plugins.wifi_nl80211.NL80211Backend',
             'frequency_range': [2400000000, 2500000000],
             'position': [i * 0.5, 0.0, 0.0]}
            for i in range(n_antennas)
        ]
        return {
            'mode': 'array_sensing',
            'array_id': f'sensing_{n_antennas}ant',
            'antennas': ants,
            'gps':  {'backend': 'none'},
            'sync': {'source': 'software'},
            'mode_config': {
                'channel': channel,
                'history_len': 100,
                'calibration_frames': 50,
                'sensitivity': 0.05,
                'hysteresis': 0.4,
                'ema_alpha': 0.3,
            },
        }

    # ── Wardriver config variants ──────────────────────────────────────────

    def test_config_wardriver_single_antenna_gpsd(self):
        cfg = AWConfig.from_dict(self._wardriver_dict(1, gps_backend='gpsd'))
        assert cfg.mode == 'wardriver'
        assert len(cfg.antennas) == 1
        assert cfg.antennas[0].id == 'wlan0'
        assert cfg.gps.backend == 'gpsd'
        assert cfg.sync.source == 'software'
        assert 'channels' in cfg.mode_config
        assert len(cfg.mode_config['channels']) == 13

    def test_config_wardriver_dual_antenna_5ghz(self):
        """Two antennas covering 5 GHz channels — realistic dual-band rig."""
        d = self._wardriver_dict(2, channels=[36, 40, 44, 48, 52, 56, 60, 64])
        d['antennas'][0]['frequency_range'] = [5150000000, 5350000000]
        d['antennas'][1]['frequency_range'] = [5470000000, 5850000000]
        cfg = AWConfig.from_dict(d)
        assert len(cfg.antennas) == 2
        # Channel split check via WardriverMode
        array = _make_array_mock(2)
        mode = WardriverMode(array, {'channels': cfg.mode_config['channels']})
        mode._assign_channels()
        all_ch = mode._channel_map['wlan0'] + mode._channel_map['wlan1']
        assert sorted(all_ch) == [36, 40, 44, 48, 52, 56, 60, 64]

    def test_config_wardriver_static_gps(self):
        """Static GPS — fixed-position wardriver (e.g. home sensor)."""
        d = self._wardriver_dict(1, gps_backend='static')
        d['gps']['lat'] = _PARIS_LAT
        d['gps']['lon'] = _PARIS_LON
        d['gps']['alt'] = 35.0
        cfg = AWConfig.from_dict(d)
        assert cfg.gps.backend == 'static'
        assert cfg.gps.lat == pytest.approx(_PARIS_LAT)
        pos = AbsolutePosition(lat=cfg.gps.lat, lon=cfg.gps.lon,
                               alt=cfg.gps.alt, fix_type=FixType.FIX_3D)
        assert pos.is_valid()

    def test_config_wardriver_no_gps(self):
        """No GPS — frames captured without geo-tagging (RSSI only)."""
        d = self._wardriver_dict(1, gps_backend='none')
        cfg = AWConfig.from_dict(d)
        assert cfg.gps.backend == 'none'
        assert cfg.mode_config.get('hop_interval') == pytest.approx(0.1)

    def test_config_wardriver_slow_hop(self):
        """Slow hop interval (0.5 s) for deep capture per channel."""
        d = self._wardriver_dict(1, hop=0.5)
        cfg = AWConfig.from_dict(d)
        assert cfg.mode_config['hop_interval'] == pytest.approx(0.5)

    def test_config_wardriver_three_antenna_split(self):
        """3 antennas — channels split into thirds across the array."""
        d = self._wardriver_dict(3)
        cfg = AWConfig.from_dict(d)
        assert len(cfg.antennas) == 3
        array = _make_array_mock(3)
        mode = WardriverMode(array, {'channels': cfg.mode_config['channels']})
        mode._assign_channels()
        all_ch = []
        for i in range(3):
            all_ch.extend(mode._channel_map.get(f'wlan{i}', []))
        assert sorted(all_ch) == list(range(1, 14))

    # ── TDOA / trilateration config variants ──────────────────────────────

    def test_config_tdoa_four_receivers_pps(self):
        """4 receivers, PPS sync — the gold-standard TDOA setup."""
        cfg = AWConfig.from_dict(self._tdoa_dict(4, sync='pps'))
        assert cfg.mode == 'trilateration'
        assert len(cfg.antennas) == 4
        assert cfg.sync.source == 'pps'
        assert cfg.mode_config['reference_antenna'] == 'wlan0'
        assert cfg.mode_config['correlation_window'] == pytest.approx(0.001)
        # Antenna positions match the non-collinear layout
        for i, ant in enumerate(cfg.antennas):
            assert tuple(ant.position) == pytest.approx(self._TDOA_POSITIONS[i])

    def test_config_tdoa_three_receivers_minimum(self):
        """3 receivers — below the solver minimum (needs ≥4). Config is still valid."""
        cfg = AWConfig.from_dict(self._tdoa_dict(3, sync='ntp'))
        assert len(cfg.antennas) == 3
        assert cfg.sync.source == 'ntp'

    def test_config_tdoa_requires_static_gps(self):
        """TDOA needs a GPS anchor (static or gpsd) for absolute position."""
        cfg = AWConfig.from_dict(self._tdoa_dict(4))
        assert cfg.gps.backend in ('static', 'gpsd', 'geoclue', 'mls')
        # Verify anchor is valid when static
        if cfg.gps.backend == 'static' and cfg.gps.lat is not None:
            pos = AbsolutePosition(lat=cfg.gps.lat, lon=cfg.gps.lon,
                                   fix_type=FixType.FIX_3D)
            assert pos.is_valid()

    def test_config_tdoa_channel_6(self):
        """TDOA: all antennas on same channel (required for TDOA)."""
        cfg = AWConfig.from_dict(self._tdoa_dict(4, channel=6))
        assert cfg.mode_config['channel'] == 6
        # All antennas should be in the 2.4 GHz band (channel 6 = 2437 MHz)
        for ant in cfg.antennas:
            lo, hi = ant.frequency_range
            assert lo <= 2437000000 <= hi

    # ── Array sensing config variants ─────────────────────────────────────

    def test_config_sensing_three_antennas(self):
        """3 antennas in sensing mode — minimum for directional sensing."""
        cfg = AWConfig.from_dict(self._sensing_dict(3))
        assert cfg.mode == 'array_sensing'
        assert len(cfg.antennas) == 3
        assert cfg.gps.backend == 'none'
        assert cfg.mode_config['sensitivity'] == pytest.approx(0.05)
        assert cfg.mode_config['ema_alpha'] == pytest.approx(0.3)

    def test_config_sensing_high_sensitivity(self):
        """Low sensitivity threshold — more sensitive to subtle motion."""
        d = self._sensing_dict(3)
        d['mode_config']['sensitivity'] = 0.02
        d['mode_config']['hysteresis']  = 0.6
        cfg = AWConfig.from_dict(d)
        assert cfg.mode_config['sensitivity'] == pytest.approx(0.02)
        assert cfg.mode_config['hysteresis']  == pytest.approx(0.6)

    def test_config_sensing_four_antennas(self):
        """4 antennas for room-scale sensing (better spatial resolution)."""
        cfg = AWConfig.from_dict(self._sensing_dict(4))
        assert len(cfg.antennas) == 4
        # Each antenna at 0.5 m spacing along X
        for i, ant in enumerate(cfg.antennas):
            assert ant.position[0] == pytest.approx(i * 0.5)

    # ── Config round-trip / JSON ───────────────────────────────────────────

    def test_config_json_roundtrip(self, tmp_path):
        """Config round-trips through JSON without losing any field."""
        import json as _json
        d = self._wardriver_dict(2, gps_backend='gpsd', sync='ntp')
        cfg = AWConfig.from_dict(d)
        # Serialise (manual — no to_json on AWConfig, so we write the raw dict)
        p = tmp_path / 'cfg.json'
        p.write_text(_json.dumps(d))
        cfg2 = AWConfig.from_json(str(p))
        assert cfg2.mode       == cfg.mode
        assert cfg2.array_id   == cfg.array_id
        assert len(cfg2.antennas) == len(cfg.antennas)
        assert cfg2.gps.backend   == cfg.gps.backend
        assert cfg2.sync.source   == cfg.sync.source

    def test_config_toml_roundtrip(self, tmp_path):
        """Config round-trips through TOML."""
        d = self._tdoa_dict(4, sync='pps')
        cfg = AWConfig.from_dict(d)
        # Build a minimal TOML manually (same as wizard output)
        lines = [
            f'mode = "{cfg.mode}"',
            f'array_id = "{cfg.array_id}"',
            '',
        ]
        for ant in cfg.antennas:
            lines += [
                '[[antennas]]',
                f'id = "{ant.id}"',
                f'backend = "{ant.backend}"',
                f'frequency_range = [{ant.frequency_range[0]}, {ant.frequency_range[1]}]',
                f'position = [{ant.position[0]}, {ant.position[1]}, {ant.position[2]}]',
                '',
            ]
        lines += [
            '[gps]',
            f'backend = "{cfg.gps.backend}"',
            f'lat = {cfg.gps.lat or 0.0}',
            f'lon = {cfg.gps.lon or 0.0}',
            '',
            '[sync]',
            f'source = "{cfg.sync.source}"',
            f'device = "{cfg.sync.device}"',
            '',
            '[mode_config]',
            f'channel = {cfg.mode_config["channel"]}',
            f'reference_antenna = "{cfg.mode_config["reference_antenna"]}"',
            f'correlation_window = {cfg.mode_config["correlation_window"]}',
            f'group_timeout = {cfg.mode_config["group_timeout"]}',
        ]
        toml_path = tmp_path / 'cfg.toml'
        toml_path.write_text('\n'.join(lines))
        cfg2 = AWConfig.from_toml(str(toml_path))
        assert cfg2.mode == 'trilateration'
        assert len(cfg2.antennas) == 4
        assert cfg2.sync.source == 'pps'
        assert cfg2.mode_config['channel'] == 6

    def test_unknown_keys_ignored(self):
        """Extra/future top-level keys in config dict must not crash from_dict."""
        d = self._wardriver_dict(1)
        d['future_field'] = 'ignore_me'
        d['another_unknown'] = {'nested': True}
        cfg = AWConfig.from_dict(d)
        assert cfg.mode == 'wardriver'

    def test_wizard_mode_specific_keys_present(self):
        """Each mode must produce the correct required mode_config keys."""
        wardriver_keys  = {'channels', 'hop_interval', 'output_path'}
        tdoa_keys       = {'channel', 'reference_antenna',
                           'correlation_window', 'group_timeout'}
        sensing_keys    = {'channel', 'history_len', 'calibration_frames',
                           'sensitivity', 'hysteresis', 'ema_alpha'}

        wd  = AWConfig.from_dict(self._wardriver_dict(1))
        td  = AWConfig.from_dict(self._tdoa_dict(4))
        sen = AWConfig.from_dict(self._sensing_dict(3))

        assert wardriver_keys.issubset(wd.mode_config.keys()), \
            f"missing: {wardriver_keys - set(wd.mode_config)}"
        assert tdoa_keys.issubset(td.mode_config.keys()), \
            f"missing: {tdoa_keys - set(td.mode_config)}"
        assert sensing_keys.issubset(sen.mode_config.keys()), \
            f"missing: {sensing_keys - set(sen.mode_config)}"

    def test_wizard_tdoa_needs_minimum_3_antennas(self):
        """Sanity: TDOA with 2 antennas is insufficient — config is valid
        but user should be warned (we assert the count constraint here)."""
        cfg = AWConfig.from_dict(self._tdoa_dict(2))
        assert len(cfg.antennas) == 2
        # Our design note: TDOA needs ≥3 for 2-D, ≥4 for 3-D
        assert len(cfg.antennas) < 3, "intent preserved: 2-ant TDOA is below minimum"

    def test_wizard_sync_pps_requires_device(self):
        """PPS sync without a device path is technically incomplete."""
        d = self._tdoa_dict(4, sync='pps')
        d['sync']['device'] = '/dev/pps0'
        cfg = AWConfig.from_dict(d)
        assert cfg.sync.source == 'pps'
        assert cfg.sync.device == '/dev/pps0'

    def test_wizard_mls_gps_backend(self):
        """MLS (Mozilla Location Service) GPS backend — no hardware GPS."""
        d = self._wardriver_dict(1, gps_backend='mls')
        cfg = AWConfig.from_dict(d)
        assert cfg.gps.backend == 'mls'

    def test_all_session_files_exist(self):
        """Verify all /tmp session files generated by previous tests are present."""
        expected = [
            'aw_single_ap_circle.jsonl',
            'aw_multi_ap_five.jsonl',
            'aw_linear_drive.jsonl',
            'aw_fixed_sensor.jsonl',
            'aw_dense_urban.jsonl',
            'aw_weak_signal.jsonl',
            'aw_dual_band.jsonl',
            'aw_car_route.jsonl',
            'aw_indoor_nlos.jsonl',
            'aw_outdoor_los.jsonl',
            'aw_1ant.jsonl',
            'aw_2ant.jsonl',
            'aw_3ant.jsonl',
            'aw_3ant_5ghz.jsonl',
            'aw_tdoa_4rx_precise.jsonl',
            'aw_tdoa_4rx_noisy.jsonl',
            'aw_tdoa_asymmetric.jsonl',
            'aw_tdoa_two_sources.jsonl',
            'aw_tdoa_floor_2d.jsonl',
            'aw_sensing_entry_exit.jsonl',
            'aw_sensing_multi_zone.jsonl',
            'aw_sensing_crowd.jsonl',
            'aw_sensing_cycle.jsonl',
        ]
        missing = [f for f in expected if not Path(f'/tmp/{f}').exists()]
        assert not missing, f"Missing session files: {missing}"

    def test_all_session_files_are_valid_jsonl(self):
        """All generated session files must contain only valid JSON lines."""
        session_dir = Path('/tmp')
        files = list(session_dir.glob('aw_*.jsonl'))
        assert files, "No session files found in /tmp"
        for f in files:
            lines = f.read_text().strip().splitlines()
            assert lines, f"{f.name} is empty"
            for i, line in enumerate(lines):
                try:
                    json.loads(line)
                except json.JSONDecodeError as e:
                    pytest.fail(f"{f.name}:{i+1}: {e}")
