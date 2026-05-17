"""
AetherWard C core bridge.

Performance-critical paths delegate to libaw (the shared C library) when
available.  Every function has a pure-Python fallback so the framework runs
without building the C library — useful for development, testing, and
platforms where compiling is impractical.
"""
from __future__ import annotations

import ctypes
from pathlib import Path
from typing import Optional

_lib: Optional[ctypes.CDLL] = None


# ── C ABI types (mirror core/include/aw_types.h + aw_tdoa.h) ────────────────

class _AwAbsPos(ctypes.Structure):
    _fields_ = [
        ('lat',        ctypes.c_double),
        ('lon',        ctypes.c_double),
        ('alt',        ctypes.c_double),
        ('accuracy_h', ctypes.c_float),
        ('accuracy_v', ctypes.c_float),
        ('timestamp',  ctypes.c_double),
        ('fix_type',   ctypes.c_uint8),
        ('num_sats',   ctypes.c_uint8),
    ]


class _AwRelPos(ctypes.Structure):
    _fields_ = [
        ('x',          ctypes.c_double),
        ('y',          ctypes.c_double),
        ('z',          ctypes.c_double),
        ('cov',        ctypes.c_double * 9),   # 3×3 row-major
        ('timestamp',  ctypes.c_double),
        ('source',     ctypes.c_uint8),
        ('has_anchor', ctypes.c_int),
        ('anchor',     _AwAbsPos),
    ]


class _AwTdoaAntenna(ctypes.Structure):
    _fields_ = [
        ('position', _AwRelPos),
        ('id',       ctypes.c_char * 64),
    ]


class _AwTdoaMeas(ctypes.Structure):
    _fields_ = [
        ('timestamp',  ctypes.c_double),
        ('tdoa',       ctypes.c_double),
        ('rssi',       ctypes.c_float),
        ('antenna_id', ctypes.c_char * 64),
    ]


class _AwTdoaResult(ctypes.Structure):
    _fields_ = [
        ('position', _AwRelPos),
        ('residual', ctypes.c_float),
        ('valid',    ctypes.c_int),
        ('n_meas',   ctypes.c_int),
    ]


def _wire_signatures(lib: ctypes.CDLL) -> None:
    lib.aw_tdoa_init.restype  = ctypes.c_void_p
    lib.aw_tdoa_init.argtypes = [
        ctypes.POINTER(_AwTdoaAntenna),
        ctypes.c_int,
        ctypes.c_char_p,
    ]
    lib.aw_tdoa_destroy.restype  = None
    lib.aw_tdoa_destroy.argtypes = [ctypes.c_void_p]
    lib.aw_tdoa_solve.restype  = _AwTdoaResult
    lib.aw_tdoa_solve.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_AwTdoaMeas),
        ctypes.c_int,
    ]


def _load_lib() -> Optional[ctypes.CDLL]:
    candidates = [
        Path(__file__).parent.parent / 'build' / 'libaw.so',
        Path(__file__).parent.parent / 'build' / 'libaw.dylib',
        Path('/usr/local/lib/libaw.so'),
        Path('/usr/lib/libaw.so'),
    ]
    for p in candidates:
        if p.exists():
            try:
                lib = ctypes.CDLL(str(p))
                _wire_signatures(lib)
                return lib
            except OSError:
                continue
    return None


try:
    _lib = _load_lib()
except Exception:
    _lib = None


def c_available() -> bool:
    """True when the C core (libaw) is loaded and usable."""
    return _lib is not None


# ── TDOA solver ──────────────────────────────────────────────────────────────

def tdoa_solve(array, measurements: list[dict],
               reference_id: str) -> Optional[dict]:
    """
    Solve for signal-source position using TDOA measurements.

    measurements: list of dicts with keys:
        antenna_id  str     antenna that captured the frame
        tdoa        float   seconds relative to the reference antenna
        rssi        float   dBm
        timestamp   float   Unix epoch

    Returns dict with:
        valid               bool
        position_relative   RelativePosition   (local ENU, cov set)
        position_absolute   AbsolutePosition   (None if no GPS anchor)
        residual            float              RMS fit error, metres
    or None on failure.
    """
    if _lib is not None:
        result = _tdoa_solve_c(array, measurements, reference_id)
        if result is not None:
            return result

    return _tdoa_solve_py(array, measurements, reference_id)


def _tdoa_solve_c(array, measurements: list[dict],
                  reference_id: str) -> Optional[dict]:
    """Call aw_tdoa_solve via the ctypes bridge."""
    import numpy as np
    from .position.relative import RelativePosition, RelSource

    assert _lib is not None

    ants = array.antennas
    ant_arr = (_AwTdoaAntenna * len(ants))()
    for i, ant in enumerate(ants):
        p   = ant.position
        rel = _AwRelPos()
        rel.x, rel.y, rel.z = p.x, p.y, p.z
        rel.source = int(p.source)
        flat = p.cov.flatten()
        for j in range(9):
            rel.cov[j] = 0.0 if not (flat[j] == flat[j]) else float(flat[j])
        ant_arr[i].position = rel
        ant_arr[i].id = ant.id.encode()[:63]

    ctx = _lib.aw_tdoa_init(ant_arr, len(ants), reference_id.encode())
    if not ctx:
        return None

    try:
        meas_arr = (_AwTdoaMeas * len(measurements))()
        for i, m in enumerate(measurements):
            meas_arr[i].timestamp  = float(m.get('timestamp', 0.0))
            meas_arr[i].tdoa       = float(m['tdoa'])
            meas_arr[i].rssi       = float(m.get('rssi', 0.0))
            meas_arr[i].antenna_id = m['antenna_id'].encode()[:63]

        res: _AwTdoaResult = _lib.aw_tdoa_solve(ctx, meas_arr, len(measurements))
    finally:
        _lib.aw_tdoa_destroy(ctx)

    if not res.valid:
        return None

    p          = res.position
    cov_matrix = np.array(list(p.cov), dtype=np.float64).reshape(3, 3)
    rel        = RelativePosition(
        x=p.x, y=p.y, z=p.z,
        cov=cov_matrix,
        source=RelSource.TDOA,
        anchor=array.absolute_position,
    )
    return {
        'valid':             True,
        'position_relative': rel,
        'position_absolute': rel.to_absolute(),
        'residual':          float(res.residual),
    }


def _tdoa_solve_py(array, measurements: list[dict],
                   reference_id: str,
                   max_iter: int = 80,
                   tol: float = 1e-7) -> Optional[dict]:
    """
    Gauss-Newton iterative TDOA solver.

    Minimises Σ (τ_i^pred − τ_i^meas)² directly over source position in the
    local ENU frame.  No extra unknown — works correctly with ≥3 non-reference
    sensors without requiring an overdetermined system.

    Multi-start: centroid of all antenna positions + 6 axis-displaced seeds.

    Covariance of the solution is estimated from the Jacobian at convergence:
        Cov(s) = (sigma_tau² / dof) · (J'J)⁻¹,  dof = max(1, M − 3)
    where sigma_tau = RMS timing residual = best_rms / C.
    """
    import numpy as np
    from .position.relative import RelativePosition, RelSource

    C = 299_792_458.0  # m/s

    ref_ant = array.get(reference_id)
    if ref_ant is None and array.antennas:
        ref_ant = array.antennas[0]
    if ref_ant is None:
        return None

    p0 = ref_ant.position.as_array()

    sensors: list[np.ndarray] = []
    tdoas:   list[float]      = []
    for meas in measurements:
        if meas['antenna_id'] == ref_ant.id:
            continue
        ant = array.get(meas['antenna_id'])
        if ant is None:
            continue
        sensors.append(ant.position.as_array())
        tdoas.append(float(meas['tdoa']))

    M = len(sensors)
    if M < 3:
        return None

    P = np.array(sensors)   # (M, 3)
    T = np.array(tdoas)     # (M,)

    def _gn(s0: np.ndarray) -> tuple[np.ndarray, float]:
        s = s0.copy()
        for _ in range(max_iter):
            d0 = max(float(np.linalg.norm(s - p0)), 1e-9)
            di = np.maximum(np.linalg.norm(P - s, axis=1), 1e-9)
            r  = (di - d0) / C - T
            J  = (1.0 / C) * ((s - P) / di[:, None] - (s - p0)[None, :] / d0)
            try:
                delta = np.linalg.solve(J.T @ J, -(J.T @ r))
            except np.linalg.LinAlgError:
                break
            s = s + delta
            if float(np.linalg.norm(delta)) < tol:
                break
        d0  = max(float(np.linalg.norm(s - p0)), 1e-9)
        di  = np.maximum(np.linalg.norm(P - s, axis=1), 1e-9)
        rms = float(np.sqrt(np.mean(((di - d0) / C - T) ** 2))) * C
        return s, rms

    centroid = np.mean(np.vstack([p0, P]), axis=0)
    spread   = float(np.max(np.linalg.norm(P - p0, axis=1))) * 2 or 10.0
    seeds    = [centroid] + [
        centroid + np.array(d, dtype=np.float64) * spread
        for d in [(1,0,0), (-1,0,0), (0,1,0), (0,-1,0), (0,0,1), (0,0,-1)]
    ]
    best_s, best_rms = None, float('inf')
    for seed in seeds:
        s, rms = _gn(np.asarray(seed, dtype=np.float64))
        if rms < best_rms:
            best_s, best_rms = s, rms

    if best_s is None:
        return None

    # Covariance from final Jacobian: Cov(s) = (sigma_tau² / dof) · (J'J)⁻¹
    d0f = max(float(np.linalg.norm(best_s - p0)), 1e-9)
    dif = np.maximum(np.linalg.norm(P - best_s, axis=1), 1e-9)
    Jf  = (1.0 / C) * ((best_s - P) / dif[:, None] - (best_s - p0)[None, :] / d0f)
    try:
        sigma_tau2 = (best_rms / C) ** 2
        cov_matrix = (sigma_tau2 / max(1, M - 3)) * np.linalg.inv(Jf.T @ Jf)
    except np.linalg.LinAlgError:
        cov_matrix = np.full((3, 3), float('inf'))

    rel = RelativePosition(
        x=float(best_s[0]), y=float(best_s[1]), z=float(best_s[2]),
        cov=cov_matrix,
        source=RelSource.TDOA,
        anchor=array.absolute_position,
    )
    return {
        'valid':             True,
        'position_relative': rel,
        'position_absolute': rel.to_absolute(),
        'residual':          best_rms,
    }
