from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..antenna.array import AntennaArray
    from ..signal.frame import Frame


class ScanMode(ABC):
    """
    Base class for all AetherWard operating modes.

    A mode owns the capture lifecycle (start/stop) and interprets
    incoming frames.  It has no knowledge of how frames are physically
    acquired — that belongs to the hardware backend.
    """
    name: str = 'base'

    def __init__(self, array: AntennaArray, config: dict):
        self.array  = array
        self.config = config
        self._running = False

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def on_frame(self, frame: Frame) -> None:
        """Called for every captured frame (may arrive from any thread)."""

    @property
    def running(self) -> bool:
        return self._running
