from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from ..orientation.quaternion import Orientation
from ..position.relative import RelativePosition
from .pattern import RadiationPattern

if TYPE_CHECKING:
    from ..hardware.backend import HardwareBackend


@dataclass
class Antenna:
    """
    A single RF antenna descriptor.

    position   — where it sits in the array's local ENU frame (metres).
    orientation — which way it points, expressed as a quaternion
                  rotating from body frame to local ENU frame.
    pattern    — radiation pattern; used to weight TDOA measurements and
                 estimate source bearing.
    backend    — pluggable hardware driver; None for passive/geometric-only use.
    """
    id: str
    position: RelativePosition
    orientation: Orientation
    pattern: RadiationPattern = field(default_factory=RadiationPattern.isotropic)
    frequency_range: tuple[float, float] = (0.0, float('inf'))   # Hz
    backend: Optional[HardwareBackend] = None
    metadata: dict = field(default_factory=dict)

    def covers_frequency(self, hz: float) -> bool:
        return self.frequency_range[0] <= hz <= self.frequency_range[1]

    def gain_toward(self, direction_enu: tuple[float, float, float]) -> float:
        """
        Return antenna gain (dBi) toward a unit vector in the local ENU frame.
        Converts the direction to azimuth/elevation relative to antenna boresight.
        """
        import math
        import numpy as np
        # Rotate direction from ENU into antenna body frame (conjugate rotation)
        d = np.asarray(direction_enu, dtype=np.float64)
        d_body = self.orientation.conjugate().rotate_vector(d)
        az  = math.degrees(math.atan2(d_body[0], d_body[1]))   # East/North → azimuth
        el  = math.degrees(math.asin(float(np.clip(d_body[2], -1, 1))))
        return self.pattern.gain_at(az % 360, el)

    def __repr__(self) -> str:
        return (f"Antenna(id={self.id!r}, pos={self.position}, "
                f"pattern={self.pattern.type.name})")
