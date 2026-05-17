"""
Unit tests for the pure-Python Gauss-Newton TDOA solver (_tdoa_solve_py).

Geometry used throughout:
  Reference antenna at (0, 0, 0); other antennas at explicit positions.
  True source position is chosen, exact TDOAs are computed analytically,
  and the solver is expected to recover the position to high precision.

Verifies:
  - Exact synthetic TDOAs → position recovered to sub-mm accuracy.
  - Returned covariance is symmetric and positive definite.
  - Off-diagonal covariance elements are non-zero for asymmetric arrays.
  - M < 3 non-reference sensors → None returned.
  - Residual is near zero for exact TDOAs.
  - Multi-start seeding: result is consistent regardless of which seed wins.
  - Solver handles degenerate (collinear) geometry without crashing.
  - Covariance scales with timing noise: noisier TDOAs → larger covariance.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from aetherward.antenna.antenna import Antenna
from aetherward.antenna.array import AntennaArray
from aetherward.core import _tdoa_solve_py
from aetherward.orientation.quaternion import Orientation
from aetherward.position.relative import RelativePosition

C = 299_792_458.0   # m/s


# ── Test geometry builder ─────────────────────────────────────────────────────

def _make_scenario(ant_positions: list[tuple[float, float, float]],
                   source_xyz: tuple[float, float, float],
                   noise_s: float = 0.0,
                   rng: np.random.Generator | None = None):
    """
    Build an AntennaArray and the corresponding TDOA measurements for a
    source at source_xyz.  Optionally add Gaussian timing noise (seconds).
    Returns (array, measurements, true_source_array).
    """
    arr = AntennaArray(id='test')
    for i, (x, y, z) in enumerate(ant_positions):
        ant = Antenna(
            id=f'ant{i}',
            position=RelativePosition(x=x, y=y, z=z),
            orientation=Orientation.identity(),
        )
        arr.add(ant)

    src = np.array(source_xyz, dtype=np.float64)
    p0  = np.array(ant_positions[0], dtype=np.float64)
    d0  = float(np.linalg.norm(src - p0))

    measurements = []
    for i, xyz in enumerate(ant_positions):
        pi = np.array(xyz, dtype=np.float64)
        di = float(np.linalg.norm(src - pi))
        tdoa = (di - d0) / C
        if noise_s and i > 0:
            gen = rng if rng is not None else np.random.default_rng(42)
            tdoa += float(gen.normal(0.0, noise_s))
        measurements.append({
            'antenna_id': f'ant{i}',
            'tdoa':       tdoa,
            'rssi':       -60.0,
            'timestamp':  0.0,
        })

    return arr, measurements, src


# ── Standard 4-antenna square array ──────────────────────────────────────────

_SQUARE_ANTS = [(0, 0, 0), (3, 0, 0), (0, 3, 0), (0, 0, 3)]
_SOURCE_A    = (5.0, 4.0, 2.0)   # well inside the bounding sphere


class TestPositionRecovery:
    def test_exact_tdoa_recovers_source_sub_mm(self):
        arr, meas, true_src = _make_scenario(_SQUARE_ANTS, _SOURCE_A)
        result = _tdoa_solve_py(arr, meas, 'ant0')

        assert result is not None
        assert result['valid']
        pos = result['position_relative']
        recovered = np.array([pos.x, pos.y, pos.z])
        error_mm = float(np.linalg.norm(recovered - true_src)) * 1000
        assert error_mm < 1.0, f"Position error {error_mm:.3f} mm exceeds 1 mm"

    def test_residual_near_zero_for_exact_tdoa(self):
        arr, meas, _ = _make_scenario(_SQUARE_ANTS, _SOURCE_A)
        result = _tdoa_solve_py(arr, meas, 'ant0')

        assert result['residual'] < 1e-4   # < 0.1 mm residual for exact TDOAs

    def test_source_on_axis(self):
        """Source exactly on the East axis — checks no degenerate geometry."""
        arr, meas, true_src = _make_scenario(_SQUARE_ANTS, (10.0, 0.0, 0.0))
        result = _tdoa_solve_py(arr, meas, 'ant0')

        assert result is not None
        pos = result['position_relative']
        assert pos.x == pytest.approx(10.0, abs=0.001)
        assert abs(pos.y) < 0.01
        assert abs(pos.z) < 0.01

    def test_different_reference_antenna(self):
        """Solver should work regardless of which antenna is reference."""
        arr, _, true_src = _make_scenario(_SQUARE_ANTS, _SOURCE_A)
        p0 = np.array(_SQUARE_ANTS[2], dtype=np.float64)
        src = np.array(_SOURCE_A, dtype=np.float64)
        d0 = float(np.linalg.norm(src - p0))

        meas_ref2 = []
        for i, xyz in enumerate(_SQUARE_ANTS):
            pi  = np.array(xyz, dtype=np.float64)
            di  = float(np.linalg.norm(src - pi))
            tdoa = (di - d0) / C
            meas_ref2.append({'antenna_id': f'ant{i}', 'tdoa': tdoa,
                               'rssi': -60.0, 'timestamp': 0.0})

        result = _tdoa_solve_py(arr, meas_ref2, 'ant2')
        assert result is not None
        pos = result['position_relative']
        error = math.sqrt((pos.x-_SOURCE_A[0])**2 +
                          (pos.y-_SOURCE_A[1])**2 +
                          (pos.z-_SOURCE_A[2])**2)
        assert error < 0.001

    def test_asymmetric_array(self):
        """Non-square array with unequal spacing."""
        ants  = [(0, 0, 0), (5, 0, 0), (0, 2, 0), (1, 1, 4)]
        src   = (3.0, 1.5, 1.0)
        arr, meas, true_src = _make_scenario(ants, src)
        result = _tdoa_solve_py(arr, meas, 'ant0')

        assert result is not None
        pos = result['position_relative']
        error = math.sqrt((pos.x-src[0])**2 + (pos.y-src[1])**2 + (pos.z-src[2])**2)
        assert error < 0.001


# ── Insufficient sensors ──────────────────────────────────────────────────────

class TestInsufficientSensors:
    def test_zero_non_reference_sensors_returns_none(self):
        arr, meas, _ = _make_scenario(_SQUARE_ANTS, _SOURCE_A)
        ref_only = [m for m in meas if m['antenna_id'] == 'ant0']
        result = _tdoa_solve_py(arr, ref_only, 'ant0')
        assert result is None

    def test_one_non_reference_sensor_returns_none(self):
        arr, meas, _ = _make_scenario(_SQUARE_ANTS, _SOURCE_A)
        two_ants = [m for m in meas if m['antenna_id'] in ('ant0', 'ant1')]
        result = _tdoa_solve_py(arr, two_ants, 'ant0')
        assert result is None

    def test_two_non_reference_sensors_returns_none(self):
        arr, meas, _ = _make_scenario(_SQUARE_ANTS, _SOURCE_A)
        three_ants = [m for m in meas if m['antenna_id'] in ('ant0', 'ant1', 'ant2')]
        result = _tdoa_solve_py(arr, three_ants, 'ant0')
        assert result is None

    def test_three_non_reference_sensors_succeeds(self):
        """Minimum valid case: 3 non-reference = 4 total antennas."""
        arr, meas, _ = _make_scenario(_SQUARE_ANTS, _SOURCE_A)
        result = _tdoa_solve_py(arr, meas, 'ant0')
        assert result is not None
        assert result['valid']

    def test_unknown_antenna_id_filtered_out(self):
        arr, meas, _ = _make_scenario(_SQUARE_ANTS, _SOURCE_A)
        bad_meas = meas + [{'antenna_id': 'ghost', 'tdoa': 0.0,
                             'rssi': -60.0, 'timestamp': 0.0}]
        result = _tdoa_solve_py(arr, bad_meas, 'ant0')
        assert result is not None   # ghost silently ignored


# ── Covariance quality ────────────────────────────────────────────────────────

class TestCovarianceQuality:
    def test_covariance_is_symmetric(self):
        arr, meas, _ = _make_scenario(_SQUARE_ANTS, _SOURCE_A)
        result = _tdoa_solve_py(arr, meas, 'ant0')
        cov = result['position_relative'].cov
        assert cov == pytest.approx(cov.T, abs=1e-20), "Covariance is not symmetric"

    def test_covariance_is_positive_definite(self):
        arr, meas, _ = _make_scenario(_SQUARE_ANTS, _SOURCE_A)
        result = _tdoa_solve_py(arr, meas, 'ant0')
        cov = result['position_relative'].cov
        eigenvalues = np.linalg.eigvalsh(cov)
        assert np.all(eigenvalues > 0), (
            f"Covariance not positive definite; eigenvalues = {eigenvalues}"
        )

    def test_covariance_not_purely_isotropic(self):
        """
        For an asymmetric array, the covariance ellipsoid must not be a
        perfect sphere (diagonal elements must differ).
        This was broken before the fix: C solver returned diag(rms², rms², rms²).

        Exact TDOAs give sigma_tau → 0 → cov → 0, so we must use noisy TDOAs
        to produce a meaningful (non-zero) covariance before checking structure.
        """
        ants = [(0, 0, 0), (5, 0, 0), (0, 2, 0), (1, 1, 4)]
        rng  = np.random.default_rng(1)
        arr, meas, _ = _make_scenario(ants, (3.0, 1.5, 1.0), noise_s=1e-9, rng=rng)
        result = _tdoa_solve_py(arr, meas, 'ant0')
        cov = result['position_relative'].cov
        diag = np.diag(cov)
        # All-zero diagonal → sigma_tau was zero (exact TDOAs) — wrong noise_s
        assert np.any(diag > 0), "All diagonal covariance elements are zero"
        # At least two diagonal elements must differ by more than 1 %
        assert not np.allclose(diag, diag[0], rtol=0.01, atol=0), (
            "Covariance diagonal is isotropic — expected asymmetric ellipsoid"
        )

    def test_covariance_has_nonzero_off_diagonal(self):
        """
        Off-diagonal terms must be present for a non-orthogonal geometry.
        Uses 1 ns timing noise so sigma_tau is non-zero and the covariance
        matrix is non-trivially populated.
        """
        ants = [(0, 0, 0), (5, 0, 0), (0, 2, 0), (1, 1, 4)]
        rng  = np.random.default_rng(2)
        arr, meas, _ = _make_scenario(ants, (3.0, 1.5, 1.0), noise_s=1e-9, rng=rng)
        result = _tdoa_solve_py(arr, meas, 'ant0')
        cov = result['position_relative'].cov
        cov_scale = float(np.max(np.abs(cov)))
        assert cov_scale > 0, "Covariance is all-zero — 1 ns noise should produce non-trivial covariance"
        off_diag_max = max(abs(cov[i][j]) for i in range(3)
                           for j in range(3) if i != j)
        assert off_diag_max / cov_scale > 1e-6, (
            "Off-diagonal covariance negligible for asymmetric array"
        )

    def test_noisy_tdoa_yields_larger_covariance(self):
        """Timing noise → larger position uncertainty → larger covariance trace."""
        rng = np.random.default_rng(0)
        arr_clean, meas_clean, _ = _make_scenario(_SQUARE_ANTS, _SOURCE_A)
        arr_noisy, meas_noisy, _ = _make_scenario(_SQUARE_ANTS, _SOURCE_A,
                                                   noise_s=10e-9, rng=rng)

        res_clean = _tdoa_solve_py(arr_clean, meas_clean, 'ant0')
        res_noisy = _tdoa_solve_py(arr_noisy, meas_noisy, 'ant0')

        tr_clean = float(np.trace(res_clean['position_relative'].cov))
        tr_noisy = float(np.trace(res_noisy['position_relative'].cov))
        assert tr_noisy > tr_clean, (
            "Noisy TDOAs should yield larger covariance trace than exact TDOAs"
        )

    def test_covariance_source_tag_is_tdoa(self):
        from aetherward.position.relative import RelSource
        arr, meas, _ = _make_scenario(_SQUARE_ANTS, _SOURCE_A)
        result = _tdoa_solve_py(arr, meas, 'ant0')
        assert result['position_relative'].source == RelSource.TDOA


# ── Result structure ──────────────────────────────────────────────────────────

class TestResultStructure:
    def test_result_keys_present(self):
        arr, meas, _ = _make_scenario(_SQUARE_ANTS, _SOURCE_A)
        result = _tdoa_solve_py(arr, meas, 'ant0')
        for key in ('valid', 'position_relative', 'position_absolute', 'residual'):
            assert key in result

    def test_position_absolute_none_without_gps_anchor(self):
        """No GPS anchor on the array → position_absolute must be None."""
        arr, meas, _ = _make_scenario(_SQUARE_ANTS, _SOURCE_A)
        assert arr.absolute_position is None
        result = _tdoa_solve_py(arr, meas, 'ant0')
        assert result['position_absolute'] is None

    def test_position_absolute_set_with_gps_anchor(self):
        from aetherward.position.absolute import AbsolutePosition, FixType
        arr, meas, _ = _make_scenario(_SQUARE_ANTS, _SOURCE_A)
        arr.absolute_position = AbsolutePosition(
            lat=48.8566, lon=2.3522, alt=35.0, fix_type=FixType.FIX_3D
        )
        result = _tdoa_solve_py(arr, meas, 'ant0')
        abs_pos = result['position_absolute']
        assert abs_pos is not None
        assert abs_pos.lat == pytest.approx(48.8566, abs=0.01)
        assert abs_pos.lon == pytest.approx(2.3522, abs=0.01)

    def test_residual_is_float(self):
        arr, meas, _ = _make_scenario(_SQUARE_ANTS, _SOURCE_A)
        result = _tdoa_solve_py(arr, meas, 'ant0')
        assert isinstance(result['residual'], float)
        assert result['residual'] >= 0.0


# ── Multi-start robustness ────────────────────────────────────────────────────

class TestMultiStart:
    def test_result_consistent_across_runs(self):
        """Deterministic exact TDOAs must always converge to the same answer."""
        arr, meas, true_src = _make_scenario(_SQUARE_ANTS, _SOURCE_A)
        results = [_tdoa_solve_py(arr, meas, 'ant0') for _ in range(5)]

        positions = [
            np.array([r['position_relative'].x,
                      r['position_relative'].y,
                      r['position_relative'].z])
            for r in results
        ]
        for p in positions[1:]:
            assert np.allclose(p, positions[0], atol=1e-6), (
                "Multi-start gave inconsistent results across runs"
            )

    def test_handles_source_far_from_centroid(self):
        """Source 50× further than array spread — tests seed diversity."""
        ants = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)]
        src  = (200.0, 150.0, 80.0)
        arr, meas, true_src = _make_scenario(ants, src)
        result = _tdoa_solve_py(arr, meas, 'ant0')

        assert result is not None
        pos = result['position_relative']
        error = math.sqrt((pos.x-src[0])**2 + (pos.y-src[1])**2 + (pos.z-src[2])**2)
        assert error < 0.1, f"Large-distance solve error {error:.3f} m"


# ── Degenerate / edge cases ───────────────────────────────────────────────────

class TestEdgeCases:
    def test_collinear_antennas_does_not_crash(self):
        """Collinear array is geometrically degenerate — solver must not raise."""
        ants = [(0, 0, 0), (1, 0, 0), (2, 0, 0), (3, 0, 0)]
        arr, meas, _ = _make_scenario(ants, (5.0, 1.0, 0.5))
        _tdoa_solve_py(arr, meas, 'ant0')

    def test_source_very_close_to_reference_antenna(self):
        """Source nearly coincident with reference — d0 clamp prevents divide-by-zero."""
        ants = [(0, 0, 0), (3, 0, 0), (0, 3, 0), (0, 0, 3)]
        arr, meas, _ = _make_scenario(ants, (0.01, 0.01, 0.01))
        _tdoa_solve_py(arr, meas, 'ant0')

    def test_empty_measurement_list_returns_none(self):
        arr, _, _ = _make_scenario(_SQUARE_ANTS, _SOURCE_A)
        result = _tdoa_solve_py(arr, [], 'ant0')
        assert result is None

    def test_reference_antenna_not_in_array_uses_first(self):
        """Unknown reference_id falls back to first antenna."""
        arr, meas, true_src = _make_scenario(_SQUARE_ANTS, _SOURCE_A)
        result = _tdoa_solve_py(arr, meas, 'nonexistent_ref')
        assert result is not None
