from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

import numpy as np


class PatternType(Enum):
    ISOTROPIC = auto()
    DIPOLE    = auto()
    PATCH     = auto()
    YAGI      = auto()
    CUSTOM    = auto()


@dataclass
class RadiationPattern:
    """
    Antenna radiation pattern.

    For isotropic and standard types, only gain_dbi is used.
    For CUSTOM, pattern_data is a 2-D array of shape (az_steps, el_steps)
    containing gain in dBi, with azimuth in [0, 360) and elevation in [-90, 90].
    """
    type: PatternType = PatternType.ISOTROPIC
    gain_dbi: float = 0.0
    pattern_data: Optional[np.ndarray] = None   # (az_steps, el_steps), dBi

    def gain_at(self, azimuth_deg: float, elevation_deg: float) -> float:
        """
        Gain in dBi at a given look angle.
        Falls back to peak gain when no pattern data is available.
        """
        if self.pattern_data is None or self.type == PatternType.ISOTROPIC:
            return self.gain_dbi

        n_az, n_el = self.pattern_data.shape
        az_idx = (azimuth_deg % 360) / 360 * n_az
        el_idx = (elevation_deg + 90) / 180 * n_el

        az_i = int(az_idx) % n_az
        el_i = int(np.clip(el_idx, 0, n_el - 1))
        az_f = az_idx - int(az_idx)
        el_f = el_idx - int(el_idx)

        az_i1 = (az_i + 1) % n_az
        el_i1 = min(el_i + 1, n_el - 1)

        g = (self.pattern_data[az_i,  el_i ] * (1-az_f) * (1-el_f)
           + self.pattern_data[az_i1, el_i ] *    az_f  * (1-el_f)
           + self.pattern_data[az_i,  el_i1] * (1-az_f) *    el_f
           + self.pattern_data[az_i1, el_i1] *    az_f  *    el_f)
        return float(g)

    @classmethod
    def isotropic(cls, gain_dbi: float = 0.0) -> RadiationPattern:
        return cls(type=PatternType.ISOTROPIC, gain_dbi=gain_dbi)

    @classmethod
    def dipole(cls, gain_dbi: float = 2.15) -> RadiationPattern:
        return cls(type=PatternType.DIPOLE, gain_dbi=gain_dbi)

    @classmethod
    def from_file(cls, path: str) -> RadiationPattern:
        """Load a pattern from a NumPy .npy file (az_steps × el_steps, dBi)."""
        data = np.load(path)
        return cls(type=PatternType.CUSTOM,
                   gain_dbi=float(np.max(data)),
                   pattern_data=data)
