from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .observation import Observation
    from ..position.absolute import AbsolutePosition
    from ..position.relative import RelativePosition


@dataclass
class SignalProperties:
    """
    Intrinsic properties of a signal type.
    Frequency-agnostic — same model for WiFi, LoRa, BLE, etc.
    """
    frequency: float                    # Hz
    bandwidth: float = 0.0              # Hz
    modulation: Optional[str] = None    # 'OFDM', 'LoRa', 'FHSS', etc.
    protocol: Optional[str] = None      # 'WiFi', 'BT', 'LoRa', 'ADS-B', etc.
    identifier: Optional[str] = None    # SSID, MAC, ICAO, device ID, etc.
    power_estimate: Optional[float] = None   # estimated TX power, dBm


@dataclass
class SignalSource:
    """
    An inferred RF emitter, built up from Observations over time.

    position_relative — result of the TDOA solver in local ENU frame.
                        This is the solver's native output.
    position_absolute — projection of position_relative using the GPS anchor,
                        or a direct GPS tag when available.
                        Always set explicitly — never assumed.
    """
    signal: SignalProperties
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    observations: list[Observation] = field(default_factory=list)
    position_relative: Optional[RelativePosition] = None
    position_absolute: Optional[AbsolutePosition] = None
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)

    def add_observation(self, obs: Observation) -> None:
        self.observations.append(obs)
        self.last_seen = obs.frame.timestamp

    @property
    def observation_count(self) -> int:
        return len(self.observations)

    @property
    def mean_rssi(self) -> Optional[float]:
        if not self.observations:
            return None
        return sum(o.rssi for o in self.observations) / len(self.observations)

    @property
    def max_rssi(self) -> Optional[float]:
        if not self.observations:
            return None
        return max(o.rssi for o in self.observations)

    def __repr__(self) -> str:
        ident = self.signal.identifier or self.id[:8]
        return (f"SignalSource({ident!r}, freq={self.signal.frequency/1e6:.3f} MHz, "
                f"obs={self.observation_count}, rssi={self.mean_rssi})")
