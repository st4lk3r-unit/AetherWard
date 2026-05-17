from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np


@dataclass
class BackendCapabilities:
    frequency_min: float               # Hz
    frequency_max: float               # Hz
    bandwidth_max: float               # Hz
    supports_channel_hop: bool = False
    supports_csi: bool = False         # channel state information export
    supports_hw_timestamp: bool = False
    supports_tdoa_sync: bool = False   # hardware-level time sync (PPS/GPSDO)
    max_antennas: int = 1


class HardwareBackend(ABC):
    """
    Abstract RF hardware driver.

    Implementors provide capture, frequency control, and optional
    CSI / hardware-timestamp capabilities.  The mode layer never
    talks to hardware directly — everything goes through this interface.
    """

    @abstractmethod
    def initialize(self) -> None:
        """Open device, load firmware, enter monitor mode, etc."""

    @abstractmethod
    def configure(self, config: dict) -> None:
        """Apply backend-specific settings from the config file."""

    @abstractmethod
    def capabilities(self) -> BackendCapabilities: ...

    @abstractmethod
    def start_capture(self, callback: Callable) -> None:
        """
        Begin capturing frames.  callback(frame: Frame) is called from
        a background thread for each received frame.
        """

    @abstractmethod
    def stop_capture(self) -> None: ...

    @abstractmethod
    def set_frequency(self, hz: float) -> None: ...

    def set_channel(self, channel: int) -> None:
        raise NotImplementedError(f"{type(self).__name__} does not support channel selection")

    def get_csi(self) -> Optional[np.ndarray]:
        raise NotImplementedError(f"{type(self).__name__} does not export CSI")

    @abstractmethod
    def close(self) -> None: ...
