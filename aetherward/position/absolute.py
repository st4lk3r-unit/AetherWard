from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum


class FixType(IntEnum):
    NONE      = 0
    FIX_2D    = 1
    FIX_3D    = 2
    DGPS      = 3
    RTK_FLOAT = 4
    RTK_FIXED = 5


# WGS84 ellipsoid constants
_WGS84_A  = 6_378_137.0
_WGS84_F  = 1 / 298.257_223_563
_WGS84_E2 = 2 * _WGS84_F - _WGS84_F ** 2


@dataclass
class AbsolutePosition:
    """
    Geodetic position in WGS84.

    This is the real-world anchor: where something is on Earth.
    Never used directly inside the TDOA solver or array geometry —
    those work exclusively in RelativePosition (local ENU frame).
    Projection between the two is always an explicit step.
    """
    lat: float                          # decimal degrees, [-90, 90]
    lon: float                          # decimal degrees, [-180, 180]
    alt: float = 0.0                    # metres above WGS84 ellipsoid
    accuracy_h: float = float('inf')    # horizontal CEP, metres
    accuracy_v: float = float('inf')    # vertical 1-sigma, metres
    timestamp: float = 0.0             # Unix epoch, sub-second
    fix_type: FixType = FixType.NONE
    num_sats: int = 0

    def is_valid(self) -> bool:
        return self.fix_type != FixType.NONE

    def distance_to(self, other: AbsolutePosition) -> float:
        """Haversine great-circle distance, metres."""
        R = 6_371_000.0
        lat1 = math.radians(self.lat)
        lat2 = math.radians(other.lat)
        dlat = math.radians(other.lat - self.lat)
        dlon = math.radians(other.lon - self.lon)
        a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
        return R * 2 * math.asin(math.sqrt(a))

    def to_ecef(self) -> tuple[float, float, float]:
        """WGS84 geodetic → ECEF (Earth-Centred Earth-Fixed), metres."""
        lat = math.radians(self.lat)
        lon = math.radians(self.lon)
        N = _WGS84_A / math.sqrt(1 - _WGS84_E2 * math.sin(lat)**2)
        x = (N + self.alt) * math.cos(lat) * math.cos(lon)
        y = (N + self.alt) * math.cos(lat) * math.sin(lon)
        z = (N * (1 - _WGS84_E2) + self.alt) * math.sin(lat)
        return x, y, z

    @classmethod
    def from_ecef(cls, x: float, y: float, z: float) -> AbsolutePosition:
        """ECEF → WGS84 geodetic (Bowring's iterative method)."""
        lon = math.atan2(y, x)
        p   = math.sqrt(x**2 + y**2)
        lat = math.atan2(z, p * (1 - _WGS84_E2))
        for _ in range(10):
            N   = _WGS84_A / math.sqrt(1 - _WGS84_E2 * math.sin(lat)**2)
            lat_new = math.atan2(z + _WGS84_E2 * N * math.sin(lat), p)
            if abs(lat_new - lat) < 1e-12:
                lat = lat_new
                break
            lat = lat_new
        N   = _WGS84_A / math.sqrt(1 - _WGS84_E2 * math.sin(lat)**2)
        coslat = math.cos(lat)
        alt = (p / coslat - N) if abs(coslat) > 1e-10 else abs(z) / math.sin(lat) - N * (1 - _WGS84_E2)
        return cls(lat=math.degrees(lat), lon=math.degrees(lon), alt=alt)

    def to_enu(self, origin: AbsolutePosition) -> tuple[float, float, float]:
        """
        Return (east, north, up) offset in metres from origin.
        Used only to convert an absolute position into the local frame
        for display or initialisation — not for solver math.
        """
        ox, oy, oz = origin.to_ecef()
        x,  y,  z  = self.to_ecef()
        dx, dy, dz = x - ox, y - oy, z - oz
        lat0 = math.radians(origin.lat)
        lon0 = math.radians(origin.lon)
        e = -math.sin(lon0)*dx + math.cos(lon0)*dy
        n = (-math.sin(lat0)*math.cos(lon0)*dx
             - math.sin(lat0)*math.sin(lon0)*dy
             + math.cos(lat0)*dz)
        u = (math.cos(lat0)*math.cos(lon0)*dx
             + math.cos(lat0)*math.sin(lon0)*dy
             + math.sin(lat0)*dz)
        return e, n, u

    def __repr__(self) -> str:
        return (f"AbsolutePosition(lat={self.lat:.6f}, lon={self.lon:.6f}, "
                f"alt={self.alt:.1f}m, fix={self.fix_type.name})")
