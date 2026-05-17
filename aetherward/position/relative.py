from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, Optional

import numpy as np

if TYPE_CHECKING:
    from .absolute import AbsolutePosition


class RelSource(IntEnum):
    IMU     = 0
    ENCODER = 1
    MANUAL  = 2
    TDOA    = 3


@dataclass
class RelativePosition:
    """
    Position in a local East-North-Up (ENU) Cartesian frame, metres.

    This is where all geometry computation lives: inter-antenna offsets,
    TDOA solver inputs and outputs, IMU dead-reckoning, array layout.
    It has nothing to do with real-world coordinates until an anchor is
    attached and to_absolute() is called — and that is always deliberate.

    The covariance matrix describes uncertainty in the ENU plane (3×3,
    row-major, metres²).  Defaults to infinite uncertainty.
    """
    x: float = 0.0    # East,  metres
    y: float = 0.0    # North, metres
    z: float = 0.0    # Up,    metres
    cov: np.ndarray = field(
        default_factory=lambda: np.full((3, 3), float('inf'))
    )
    timestamp: float = 0.0
    source: RelSource = RelSource.MANUAL
    anchor: Optional[AbsolutePosition] = None

    def distance_to(self, other: RelativePosition) -> float:
        return math.sqrt(
            (self.x - other.x)**2 +
            (self.y - other.y)**2 +
            (self.z - other.z)**2
        )

    def as_array(self) -> np.ndarray:
        return np.array([self.x, self.y, self.z], dtype=np.float64)

    def to_absolute(self) -> Optional[AbsolutePosition]:
        """
        Project this ENU point to absolute coordinates via the anchor.
        Returns None if no anchor is set.
        """
        if self.anchor is None:
            return None
        from .absolute import AbsolutePosition
        import math
        ox, oy, oz = self.anchor.to_ecef()
        lat0 = math.radians(self.anchor.lat)
        lon0 = math.radians(self.anchor.lon)
        # ENU → ECEF rotation matrix (transpose of ECEF→ENU)
        dx = (-math.sin(lon0) * self.x
              - math.sin(lat0) * math.cos(lon0) * self.y
              + math.cos(lat0) * math.cos(lon0) * self.z)
        dy = ( math.cos(lon0) * self.x
              - math.sin(lat0) * math.sin(lon0) * self.y
              + math.cos(lat0) * math.sin(lon0) * self.z)
        dz = (math.cos(lat0) * self.y + math.sin(lat0) * self.z)
        return AbsolutePosition.from_ecef(ox + dx, oy + dy, oz + dz)

    @classmethod
    def from_array(cls, arr: np.ndarray, **kwargs) -> RelativePosition:
        return cls(x=float(arr[0]), y=float(arr[1]), z=float(arr[2]), **kwargs)

    @classmethod
    def origin(cls) -> RelativePosition:
        return cls(x=0.0, y=0.0, z=0.0, cov=np.zeros((3, 3)))

    def __repr__(self) -> str:
        return (f"RelativePosition(x={self.x:.3f}m, y={self.y:.3f}m, "
                f"z={self.z:.3f}m, src={self.source.name})")
