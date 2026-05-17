from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .antenna import Antenna
from ..orientation.quaternion import Orientation
from ..position.absolute import AbsolutePosition
from ..position.relative import RelativePosition


@dataclass
class AntennaArray:
    """
    A collection of antennas sharing a common local ENU frame.

    The two independent coordinate systems are kept strictly separate:

      absolute_position  — GPS anchor updated live from the GNSS receiver.
                           Represents the origin of the local frame in the real world.
      orientation        — IMU quaternion representing array heading/tilt.

    All geometry math (TDOA, array sensing) works in local ENU (RelativePosition).
    Projection to AbsolutePosition only happens explicitly via resolve_*() methods.
    """
    id: str
    antennas: list[Antenna] = field(default_factory=list)
    absolute_position: Optional[AbsolutePosition] = None   # GPS anchor, updated live
    orientation: Optional[Orientation] = None              # IMU, updated live
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._by_id: dict[str, Antenna] = {a.id: a for a in self.antennas}

    # ── Antenna management ───────────────────────────────────────────────

    def add(self, antenna: Antenna) -> None:
        self.antennas.append(antenna)
        self._by_id[antenna.id] = antenna

    def remove(self, antenna_id: str) -> None:
        self.antennas = [a for a in self.antennas if a.id != antenna_id]
        self._by_id.pop(antenna_id, None)

    def get(self, antenna_id: str) -> Optional[Antenna]:
        return self._by_id.get(antenna_id)

    @property
    def n(self) -> int:
        return len(self.antennas)

    # ── Live updates from sensors ────────────────────────────────────────

    def update_position(self, pos: AbsolutePosition) -> None:
        """Call on every GNSS fix."""
        self.absolute_position = pos

    def update_orientation(self, ori: Orientation) -> None:
        """Call on every IMU update."""
        self.orientation = ori

    # ── Coordinate projection ────────────────────────────────────────────

    def resolve_antenna_absolute(self, antenna_id: str) -> Optional[AbsolutePosition]:
        """
        Project an antenna's local position to absolute coordinates.
        Applies the array orientation (IMU) before using the GPS anchor.
        Returns None if no GPS fix is available.
        """
        ant = self.get(antenna_id)
        if ant is None or self.absolute_position is None:
            return None

        pos_vec = ant.position.as_array()
        if self.orientation is not None:
            pos_vec = self.orientation.rotate_vector(pos_vec)

        rel = RelativePosition.from_array(pos_vec, anchor=self.absolute_position)
        return rel.to_absolute()

    def geometry_matrix(self) -> np.ndarray:
        """Return (n × 3) matrix of antenna positions in local ENU, metres."""
        if not self.antennas:
            return np.empty((0, 3), dtype=np.float64)
        return np.array([a.position.as_array() for a in self.antennas], dtype=np.float64)

    def __repr__(self) -> str:
        return (f"AntennaArray(id={self.id!r}, n={self.n}, "
                f"gps={'yes' if self.absolute_position else 'no'}, "
                f"imu={'yes' if self.orientation else 'no'})")
