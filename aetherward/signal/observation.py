from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

import numpy as np

if TYPE_CHECKING:
    from .frame import Frame
    from ..position.absolute import AbsolutePosition


@dataclass
class Observation:
    """
    A Frame in context: measured properties at a specific antenna.

    rssi     — may differ from frame.rssi after antenna-gain calibration.
    tdoa     — seconds relative to the reference antenna (None in wardriver mode).
    csi      — complex Channel State Information matrix if hardware exposes it.
    doppler  — Doppler shift estimate, Hz (when available).
    array_absolute — snapshot of the array GPS position at capture time.
                     Separate from the TDOA/geometry math; used only for tagging.
    """
    frame: Frame
    antenna_id: str
    rssi: float
    tdoa: Optional[float] = None
    csi: Optional[np.ndarray] = None
    doppler: Optional[float] = None
    array_absolute: Optional[AbsolutePosition] = None
    metadata: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        return (f"Observation(ant={self.antenna_id!r}, rssi={self.rssi:.1f} dBm, "
                f"tdoa={self.tdoa}, ts={self.frame.timestamp:.6f})")
