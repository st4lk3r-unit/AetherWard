from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from ..orientation.quaternion import Orientation, OriSource


@dataclass
class IMUReading:
    """Raw sensor values from an IMU at one instant."""
    acceleration: tuple[float, float, float]             # m/s², body XYZ
    gyroscope: tuple[float, float, float]                # rad/s, body XYZ
    magnetometer: Optional[tuple[float, float, float]] = None  # µT
    orientation: Optional[Orientation] = None            # fusion output if provided
    temperature: Optional[float] = None                  # °C
    timestamp: float = 0.0


class IMUBackend(ABC):
    """Abstract IMU driver."""

    @abstractmethod
    def initialize(self) -> None: ...

    @abstractmethod
    def read(self) -> Optional[IMUReading]: ...

    @abstractmethod
    def close(self) -> None: ...


class NullIMUBackend(IMUBackend):
    """
    No-op IMU for setups without motion sensing.
    Returns identity orientation so the array is assumed level and north-aligned.
    """

    def initialize(self) -> None:
        pass

    def read(self) -> IMUReading:
        import time
        return IMUReading(
            acceleration=(0.0, 0.0, 9.81),
            gyroscope=(0.0, 0.0, 0.0),
            orientation=Orientation.identity(),
            timestamp=time.time(),
        )

    def close(self) -> None:
        pass


class SerialIMUBackend(IMUBackend):
    """
    Generic UART/serial IMU backend.
    Expects a JSON line per reading: {"ax":..,"ay":..,"az":..,"gx":..,"gy":..,"gz":..,"qw":..,...}
    """

    def __init__(self, port: str, baud: int = 115200):
        self._port = port
        self._baud = baud
        self._ser  = None

    def initialize(self) -> None:
        try:
            import serial
            self._ser = serial.Serial(self._port, self._baud, timeout=1.0)
        except ImportError:
            raise RuntimeError("Install pyserial: pip install pyserial")

    def read(self) -> Optional[IMUReading]:
        if self._ser is None:
            return None
        try:
            import json
            import time
            line = self._ser.readline().decode().strip()
            d = json.loads(line)
            ori = None
            if all(k in d for k in ('qw', 'qx', 'qy', 'qz')):
                ori = Orientation(
                    w=d['qw'], x=d['qx'], y=d['qy'], z=d['qz'],
                    source=OriSource.IMU,
                    timestamp=time.time(),
                )
            return IMUReading(
                acceleration=(d.get('ax', 0.0), d.get('ay', 0.0), d.get('az', 9.81)),
                gyroscope=(d.get('gx', 0.0), d.get('gy', 0.0), d.get('gz', 0.0)),
                orientation=ori,
                timestamp=time.time(),
            )
        except Exception:
            return None

    def close(self) -> None:
        if self._ser:
            self._ser.close()
            self._ser = None
