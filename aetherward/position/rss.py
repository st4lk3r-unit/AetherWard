"""
RSS trilateration — Gauss-Newton solver.

Fits the log-distance path loss model:

    RSSI_i = A − 10·n·log₁₀(d_i)

where d_i is the distance from observer i to the transmitter and A is the
RSSI at 1 m (a proxy for transmit power + antenna gain).

Solves jointly for transmitter position (x, y in a local ENU frame) and A,
with the path loss exponent n treated as a fixed parameter.

Minimum 3 GPS-tagged observations required.  With fewer, returns None and
the caller should fall back to an RSSI-weighted centroid.
"""

from __future__ import annotations
import math

_LN10     = math.log(10.0)
_M_PER_DEG = 111_320.0          # metres per degree of latitude (WGS-84 approx)


# ── 3×3 linear solver (Cramer's rule) ────────────────────────────────────────

def _det3(m: list[list[float]]) -> float:
    return (m[0][0] * (m[1][1]*m[2][2] - m[1][2]*m[2][1])
          - m[0][1] * (m[1][0]*m[2][2] - m[1][2]*m[2][0])
          + m[0][2] * (m[1][0]*m[2][1] - m[1][1]*m[2][0]))


def _solve3(A: list[list[float]], b: list[float]) -> list[float] | None:
    d = _det3(A)
    if abs(d) < 1e-30:
        return None

    def _sub(col: int) -> list[list[float]]:
        m = [row[:] for row in A]
        for i in range(3):
            m[i][col] = b[i]
        return m

    return [_det3(_sub(i)) / d for i in range(3)]


# ── RSSI-weighted centroid (fast initialisation seed) ─────────────────────────

def rssi_centroid(
    observations: list[tuple[float, float, float]],
) -> tuple[float, float]:
    """
    Return (lat, lon) weighted by linear-scale RSSI.

    Higher RSSI → observer was closer to the transmitter → more weight.
    This is not trilateration but gives a better seed than a naive mean.
    """
    w_sum = wx = wy = 0.0
    for lat, lon, rssi in observations:
        w = 10.0 ** (rssi / 10.0)
        wx += w * lat
        wy += w * lon
        w_sum += w
    if w_sum == 0.0:
        n = len(observations)
        return (sum(o[0] for o in observations) / n,
                sum(o[1] for o in observations) / n)
    return wx / w_sum, wy / w_sum


# ── Main solver ───────────────────────────────────────────────────────────────

def rss_solve(
    observations: list[tuple[float, float, float]],
    n_exp: float = 2.5,
    max_iter: int = 60,
) -> dict | None:
    """
    RSS trilateration via Gauss-Newton least squares.

    Parameters
    ----------
    observations:
        List of (lat_deg, lon_deg, rssi_dBm) tuples — observer positions
        with the measured received signal strength.  At least 3 required.
    n_exp:
        Path loss exponent (fixed).
        2.0 = free space / line-of-sight
        2.5 = typical urban outdoor
        3.0–4.5 = suburban / light-NLOS
        Keep at default unless you have site-specific calibration data.
    max_iter:
        Gauss-Newton iteration cap.

    Returns
    -------
    dict  with keys:
        lat            — estimated transmitter latitude
        lon            — estimated transmitter longitude
        rssi_at_1m     — fitted RSSI at 1 m (dBm); proxy for TX power + gain
        n_exp          — path loss exponent used
        residual_dBm   — RMS fit residual in dB (lower = better)
        samples        — number of observations used
    or None if fewer than 3 observations or the system was singular.
    """
    if len(observations) < 3:
        return None

    # ── Local ENU frame (metres, origin = centroid of observations) ───────────
    lat_ref = sum(o[0] for o in observations) / len(observations)
    lon_ref = sum(o[1] for o in observations) / len(observations)
    cos_lat = math.cos(math.radians(lat_ref))

    def _to_m(lat: float, lon: float) -> tuple[float, float]:
        return ((lon - lon_ref) * cos_lat * _M_PER_DEG,
                (lat - lat_ref) * _M_PER_DEG)

    pts = [(_to_m(lat, lon), rssi) for lat, lon, rssi in observations]

    # ── Seed: RSSI-weighted centroid + A ≈ RSSI of closest observation ────────
    seed_lat, seed_lon = rssi_centroid(observations)
    sx, sy = _to_m(seed_lat, seed_lon)
    sa     = max(rssi for _, rssi in pts)   # A ≥ max observed RSSI

    # ── Gauss-Newton: solve for (sx, sy, sa) ─────────────────────────────────
    for _ in range(max_iter):
        JtJ = [[0.0] * 3 for _ in range(3)]
        Jtr = [0.0] * 3

        for (xi, yi), ri in pts:
            dx, dy = sx - xi, sy - yi
            d      = max(math.sqrt(dx*dx + dy*dy), 0.1)

            r_pred = sa - 10.0 * n_exp * math.log10(d)
            res    = r_pred - ri

            # ∂r_pred/∂(sx, sy, sa)
            c = -10.0 * n_exp / (d * d * _LN10)
            J = [c * dx, c * dy, 1.0]

            for r_i in range(3):
                for c_i in range(3):
                    JtJ[r_i][c_i] += J[r_i] * J[c_i]
                Jtr[r_i] -= J[r_i] * res

        delta = _solve3(JtJ, Jtr)
        if delta is None:
            break

        sx += delta[0]
        sy += delta[1]
        sa += delta[2]

        if delta[0]**2 + delta[1]**2 < 1e-4:   # < 1 cm convergence
            break

    # ── Sanity check: reject solutions far outside the observation hull ────────
    # This used to compute the exact maximum pairwise point distance, O(n²).
    # Web/bulk solving can feed hundreds of aggregated geo-cells per source and
    # thousands of sources, so the exact hull span became a pointless hot path.
    # The bbox diagonal is a safe upper-bound approximation for divergence
    # rejection and keeps each source solve O(n).
    xs = [pt[0][0] for pt in pts]
    ys = [pt[0][1] for pt in pts]
    obs_span = math.sqrt((max(xs) - min(xs))**2 + (max(ys) - min(ys))**2)
    dist_from_centroid = math.sqrt(sx*sx + sy*sy)
    if dist_from_centroid > max(obs_span * 5.0, 500.0):
        return None     # diverged — caller falls back to centroid

    # ── Final RMS residual ─────────────────────────────────────────────────────
    rms2 = 0.0
    for (xi, yi), ri in pts:
        d = max(math.sqrt((sx-xi)**2 + (sy-yi)**2), 0.1)
        rms2 += (sa - 10.0 * n_exp * math.log10(d) - ri) ** 2
    residual = math.sqrt(rms2 / len(pts))

    # ── Back to geographic coordinates ────────────────────────────────────────
    lat_ap = lat_ref + sy / _M_PER_DEG
    lon_ap = lon_ref + sx / (cos_lat * _M_PER_DEG)

    return {
        'lat':          lat_ap,
        'lon':          lon_ap,
        'rssi_at_1m':   round(sa, 1),
        'n_exp':        n_exp,
        'residual_dBm': round(residual, 2),
        'samples':      len(observations),
    }
