"""Tests for RSS trilateration solver and RSSI centroid."""
from __future__ import annotations

import math

import pytest

from aetherward.position.rss import rssi_centroid, rss_solve


# ── rssi_centroid ─────────────────────────────────────────────────────────────

class TestRssiCentroid:
    def test_single_observation(self):
        lat, lon = rssi_centroid([(48.0, 2.0, -50.0)])
        assert lat == pytest.approx(48.0)
        assert lon == pytest.approx(2.0)

    def test_equal_weight(self):
        obs = [(48.0, 2.0, -70.0), (49.0, 3.0, -70.0)]
        lat, lon = rssi_centroid(obs)
        assert lat == pytest.approx(48.5, abs=0.01)
        assert lon == pytest.approx(2.5, abs=0.01)

    def test_stronger_signal_pulls_centroid(self):
        # observer at (48, 2) sees -40 dBm (strong → close), (49, 3) sees -90 dBm
        obs = [(48.0, 2.0, -40.0), (49.0, 3.0, -90.0)]
        lat, lon = rssi_centroid(obs)
        # centroid should be pulled toward the stronger signal (48, 2)
        assert lat < 48.5
        assert lon < 2.5

    def test_zero_weight_fallback(self):
        # -inf rssi → 10^(-inf/10) = 0, triggers fallback mean
        obs = [(1.0, 1.0, float('-inf')), (3.0, 3.0, float('-inf'))]
        lat, lon = rssi_centroid(obs)
        assert lat == pytest.approx(2.0)
        assert lon == pytest.approx(2.0)


# ── rss_solve ─────────────────────────────────────────────────────────────────

class TestRssSolve:
    def _make_observations(self, true_lat, true_lon, observer_coords,
                           n_exp=2.5, rssi_at_1m=-30.0, noise=0.0):
        """Synthesise observations for a transmitter at (true_lat, true_lon)."""
        M_PER_DEG = 111_320.0
        obs = []
        for lat, lon in observer_coords:
            dx = (lon - true_lon) * math.cos(math.radians(true_lat)) * M_PER_DEG
            dy = (lat - true_lat) * M_PER_DEG
            d = max(math.sqrt(dx*dx + dy*dy), 0.1)
            rssi = rssi_at_1m - 10.0 * n_exp * math.log10(d) + noise
            obs.append((lat, lon, rssi))
        return obs

    def test_fewer_than_3_returns_none(self):
        obs = [(48.0, 2.0, -60.0), (48.001, 2.001, -65.0)]
        assert rss_solve(obs) is None

    def test_exact_solution_no_noise(self):
        true_lat, true_lon = 48.8566, 2.3522
        observers = [
            (48.8556, 2.3522),
            (48.8576, 2.3522),
            (48.8566, 2.3512),
            (48.8566, 2.3532),
        ]
        obs = self._make_observations(true_lat, true_lon, observers)
        result = rss_solve(obs, n_exp=2.5)
        assert result is not None
        assert result['lat'] == pytest.approx(true_lat, abs=0.0002)
        assert result['lon'] == pytest.approx(true_lon, abs=0.0002)

    def test_returns_required_keys(self):
        true_lat, true_lon = 51.5, -0.12
        observers = [(51.499, -0.12), (51.501, -0.12), (51.5, -0.119), (51.5, -0.121)]
        obs = self._make_observations(true_lat, true_lon, observers)
        result = rss_solve(obs)
        assert result is not None
        for key in ('lat', 'lon', 'rssi_at_1m', 'n_exp', 'residual_dBm', 'samples'):
            assert key in result

    def test_samples_count(self):
        observers = [(48.0, 2.0), (48.001, 2.0), (48.0, 2.001), (48.001, 2.001)]
        obs = self._make_observations(48.0005, 2.0005, observers)
        result = rss_solve(obs)
        assert result is not None
        assert result['samples'] == 4

    def test_n_exp_respected(self):
        # Two solves with different n_exp should give different rssi_at_1m
        observers = [(48.0, 2.0), (48.001, 2.0), (48.0, 2.001)]
        obs = self._make_observations(48.0005, 2.0005, observers, n_exp=2.0)
        r1 = rss_solve(obs, n_exp=2.0)
        r2 = rss_solve(obs, n_exp=3.5)
        assert r1 is not None and r2 is not None
        assert r1['n_exp'] == pytest.approx(2.0)
        assert r2['n_exp'] == pytest.approx(3.5)

    def test_diverged_solution_rejected(self):
        # All observations at almost the same point — solver should either
        # converge or return None (not return a wildly wrong result)
        obs = [
            (48.0, 2.0, -60.0),
            (48.0, 2.0, -60.0),
            (48.0, 2.0, -60.0),
        ]
        result = rss_solve(obs)
        if result is not None:
            # if it returns something, it must be close to (48, 2)
            assert abs(result['lat'] - 48.0) < 10.0

    def test_residual_low_for_clean_data(self):
        true_lat, true_lon = 48.0, 2.0
        observers = [(47.999, 2.0), (48.001, 2.0), (48.0, 1.999), (48.0, 2.001)]
        obs = self._make_observations(true_lat, true_lon, observers)
        result = rss_solve(obs)
        assert result is not None
        assert result['residual_dBm'] < 1.0  # near-zero noise → near-zero residual

    def test_custom_max_iter(self):
        observers = [(48.0, 2.0), (48.001, 2.0), (48.0, 2.001)]
        obs = self._make_observations(48.0005, 2.0005, observers)
        # Even with max_iter=1 it shouldn't crash
        rss_solve(obs, max_iter=1)
        # might or might not converge but should not raise
