from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from aetherward.session import default_session_path


@dataclass
class AntennaConfig:
    id: str
    backend: str                                         # class name or plugin path
    backend_config: dict = field(default_factory=dict)
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)       # ENU offset, metres
    orientation_euler: tuple[float, float, float] = (0.0, 0.0, 0.0)  # roll/pitch/yaw, degrees
    frequency_range: tuple[float, float] = (0.0, 6e9)             # Hz
    pattern: str = 'isotropic'                           # 'isotropic', 'dipole', or .npy path
    gain_dbi: float = 0.0


@dataclass
class GPSConfig:
    backend: str = 'gpsd'          # 'gpsd', 'static', 'geoclue', 'mls', 'ip', 'none'
    host: str = 'localhost'        # gpsd host
    port: int = 2947               # gpsd port
    lat: Optional[float] = None    # StaticGPSBackend
    lon: Optional[float] = None
    alt: float = 0.0
    interface: str = ''            # MLS: WiFi interface to scan ('' = auto-detect)
    api_url: str = ''              # MLS: override endpoint URL


@dataclass
class IMUConfig:
    backend: str = 'null'          # 'null', 'serial', custom class
    device: str = ''
    baud: int = 115200
    config: dict = field(default_factory=dict)


@dataclass
class SyncConfig:
    source: str = 'software'      # 'software', 'ntp', 'pps', 'gpsdo'
    device: str = ''              # e.g. /dev/pps0


@dataclass
class AWConfig:
    mode: str = 'wardriver'
    array_id: str = 'default'
    antennas: list[AntennaConfig] = field(default_factory=list)
    gps: GPSConfig = field(default_factory=GPSConfig)
    imu: IMUConfig = field(default_factory=IMUConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    mode_config: dict = field(default_factory=dict)
    output: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> AWConfig:
        cfg = cls()
        cfg.mode      = d.get('mode', cfg.mode)
        cfg.array_id  = d.get('array_id', cfg.array_id)
        cfg.antennas  = [AntennaConfig(**a) for a in d.get('antennas', [])]
        if 'gps'  in d: cfg.gps  = GPSConfig(**d['gps'])
        if 'imu'  in d: cfg.imu  = IMUConfig(**d['imu'])
        if 'sync' in d: cfg.sync = SyncConfig(**d['sync'])
        cfg.mode_config = dict(d.get('mode_config', {}))
        cfg.output      = dict(d.get('output', {}))

        # Keep the user-facing [output] table authoritative for saved runs,
        # while preserving backward compatibility with older configs that put
        # the path directly under [mode_config].
        #
        # If no path is configured for wardriving, enable the default session
        # folder instead of silently running without a JSONL writer.  Explicit
        # output.format = "none" remains the opt-out.
        out_fmt = str(cfg.output.get('format', 'jsonl')).lower()
        if 'output_path' not in cfg.mode_config and cfg.output.get('path'):
            cfg.mode_config['output_path'] = cfg.output['path']
        elif (
            cfg.mode == 'wardriver'
            and 'output_path' not in cfg.mode_config
            and out_fmt not in ('none', 'off', 'false', 'disabled')
        ):
            cfg.mode_config['output_path'] = default_session_path(cfg.array_id, cfg.mode)
            cfg.output.setdefault('format', 'jsonl')
            cfg.output.setdefault('path', cfg.mode_config['output_path'])

        return cfg

    @classmethod
    def from_json(cls, path: str) -> AWConfig:
        import json
        with open(path) as f:
            return cls.from_dict(json.load(f))

    @classmethod
    def from_yaml(cls, path: str) -> AWConfig:
        try:
            import yaml
        except ImportError:
            raise RuntimeError("Install pyyaml: pip install pyyaml")
        with open(path) as f:
            return cls.from_dict(yaml.safe_load(f))

    @classmethod
    def from_toml(cls, path: str) -> AWConfig:
        import tomllib
        with open(path, 'rb') as f:
            return cls.from_dict(tomllib.load(f))
