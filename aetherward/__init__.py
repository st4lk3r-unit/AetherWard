"""
AetherWard (AW) — RF observation framework.

Hardware-agnostic.  Frequency-agnostic.  Mode-agnostic.
"""

__version__ = '0.3.1'

from .position.absolute import AbsolutePosition, FixType
from .position.relative import RelativePosition, RelSource
from .orientation.quaternion import Orientation, OriSource
from .antenna.antenna import Antenna
from .antenna.array import AntennaArray
from .antenna.pattern import RadiationPattern, PatternType
from .signal.frame import Frame
from .signal.observation import Observation
from .signal.source import SignalSource, SignalProperties
from .hardware.backend import HardwareBackend, BackendCapabilities
from .hardware.gps import (GPSBackend, GPSDBackend, StaticGPSBackend,
                            GeoclueBackend, MozillaLBSBackend, IPGeolocationBackend)
from .hardware.imu import IMUBackend, IMUReading, NullIMUBackend, SerialIMUBackend
from .modes.wardriver import WardriverMode
from .modes.trilateration import TrilaterationMode
from .modes.array_sensing import ArraySensingMode, SensingEvent

MODES: dict = {
    'wardriver':     WardriverMode,
    'trilateration': TrilaterationMode,
    'array_sensing': ArraySensingMode,
}

__all__ = [
    '__version__',
    'AbsolutePosition', 'FixType',
    'RelativePosition', 'RelSource',
    'Orientation', 'OriSource',
    'Antenna', 'AntennaArray',
    'RadiationPattern', 'PatternType',
    'Frame', 'Observation',
    'SignalSource', 'SignalProperties',
    'HardwareBackend', 'BackendCapabilities',
    'GPSBackend', 'GPSDBackend', 'StaticGPSBackend',
    'GeoclueBackend', 'MozillaLBSBackend', 'IPGeolocationBackend',
    'IMUBackend', 'IMUReading', 'NullIMUBackend', 'SerialIMUBackend',
    'WardriverMode', 'TrilaterationMode', 'ArraySensingMode', 'SensingEvent',
    'MODES',
]
