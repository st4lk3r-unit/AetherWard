from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Frame:
    """
    A single captured RF event — intentionally frequency-agnostic.

    Works identically for 802.11 packets, SDR IQ bursts, LoRa frames,
    or any other modality.  Backend-specific data goes in metadata.
    """
    data: bytes                        # raw frame bytes
    frequency: float                   # centre frequency, Hz
    bandwidth: float                   # capture bandwidth, Hz
    timestamp: float                   # Unix epoch, sub-second (hw timestamp when available)
    rssi: float                        # received power at antenna port, dBm
    antenna_id: str                    # originating antenna
    sample_rate: float = 0.0           # Hz, for SDR raw IQ captures
    metadata: dict = field(default_factory=dict)

    @classmethod
    def now(cls, **kwargs) -> Frame:
        return cls(timestamp=time.time(), **kwargs)

    def __repr__(self) -> str:
        return (f"Frame(freq={self.frequency/1e6:.3f} MHz, "
                f"rssi={self.rssi:.1f} dBm, ant={self.antenna_id!r}, "
                f"len={len(self.data)}B)")
