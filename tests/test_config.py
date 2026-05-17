"""Tests for config schema parsing — AWConfig, AntennaConfig, GPS/IMU/SyncConfig."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aetherward.config.schema import (
    AntennaConfig, AWConfig, GPSConfig, IMUConfig, SyncConfig,
)


# ── AWConfig.from_dict ────────────────────────────────────────────────────────

class TestFromDict:
    def test_defaults(self):
        cfg = AWConfig.from_dict({})
        assert cfg.mode == 'wardriver'
        assert cfg.array_id == 'default'
        assert cfg.antennas == []
        assert isinstance(cfg.gps, GPSConfig)
        assert isinstance(cfg.imu, IMUConfig)
        assert isinstance(cfg.sync, SyncConfig)

    def test_mode_override(self):
        cfg = AWConfig.from_dict({'mode': 'trilateration'})
        assert cfg.mode == 'trilateration'

    def test_array_id(self):
        cfg = AWConfig.from_dict({'array_id': 'roof-array'})
        assert cfg.array_id == 'roof-array'

    def test_antennas_parsed(self):
        d = {'antennas': [
            {'id': 'wlan0', 'backend': 'plugins.wifi_nl80211.NL80211Backend'},
            {'id': 'wlan1', 'backend': 'plugins.wifi_nl80211.NL80211Backend',
             'gain_dbi': 3.0},
        ]}
        cfg = AWConfig.from_dict(d)
        assert len(cfg.antennas) == 2
        assert cfg.antennas[0].id == 'wlan0'
        assert cfg.antennas[1].gain_dbi == 3.0

    def test_gps_section(self):
        cfg = AWConfig.from_dict({'gps': {'backend': 'static', 'lat': 48.85, 'lon': 2.35}})
        assert cfg.gps.backend == 'static'
        assert cfg.gps.lat == pytest.approx(48.85)
        assert cfg.gps.lon == pytest.approx(2.35)

    def test_imu_section(self):
        cfg = AWConfig.from_dict({'imu': {'backend': 'serial', 'device': '/dev/ttyUSB0', 'baud': 9600}})
        assert cfg.imu.backend == 'serial'
        assert cfg.imu.device == '/dev/ttyUSB0'
        assert cfg.imu.baud == 9600

    def test_sync_section(self):
        cfg = AWConfig.from_dict({'sync': {'source': 'pps', 'device': '/dev/pps0'}})
        assert cfg.sync.source == 'pps'
        assert cfg.sync.device == '/dev/pps0'

    def test_mode_config(self):
        mc = {'channels': [1, 6, 11], 'hop_interval': 0.2}
        cfg = AWConfig.from_dict({'mode_config': mc})
        assert cfg.mode_config['channels'] == [1, 6, 11]
        assert cfg.mode_config['hop_interval'] == pytest.approx(0.2)

    def test_output(self):
        cfg = AWConfig.from_dict({'output': {'format': 'jsonl', 'path': '/tmp/out.jsonl'}})
        assert cfg.output['format'] == 'jsonl'

    def test_unknown_keys_ignored(self):
        # extra keys in top-level dict shouldn't crash
        cfg = AWConfig.from_dict({'future_key': 'x', 'mode': 'wardriver'})
        assert cfg.mode == 'wardriver'


# ── AWConfig.from_json ────────────────────────────────────────────────────────

class TestFromJson:
    def test_roundtrip(self, tmp_path):
        data = {
            'mode': 'wardriver',
            'array_id': 'test',
            'antennas': [{'id': 'wlan0', 'backend': 'null'}],
            'gps': {'backend': 'none'},
        }
        p = tmp_path / 'cfg.json'
        p.write_text(json.dumps(data))
        cfg = AWConfig.from_json(str(p))
        assert cfg.mode == 'wardriver'
        assert cfg.antennas[0].id == 'wlan0'
        assert cfg.gps.backend == 'none'

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            AWConfig.from_json(str(tmp_path / 'nonexistent.json'))


# ── AWConfig.from_toml ────────────────────────────────────────────────────────

class TestFromToml:
    TOML = """\
mode = "trilateration"
array_id = "lab-array"

[[antennas]]
id = "wlan0"
backend = "plugins.wifi_nl80211.NL80211Backend"
backend_config = {interface = "wlan0"}
frequency_range = [2400000000, 2500000000]
position = [0.0, 0.0, 0.0]
orientation_euler = [0.0, 0.0, 0.0]
gain_dbi = 0.0

[[antennas]]
id = "wlan1"
backend = "plugins.wifi_nl80211.NL80211Backend"
backend_config = {interface = "wlan1"}
frequency_range = [2400000000, 2500000000]
position = [0.5, 0.0, 0.0]
orientation_euler = [0.0, 0.0, 0.0]
gain_dbi = 0.0

[gps]
backend = "static"
lat = 48.856600
lon = 2.352200
alt = 35.0

[sync]
source = "pps"
device = "/dev/pps0"

[mode_config]
channel = 6
reference_antenna = "wlan0"
correlation_window = 0.001
group_timeout = 0.05

[output]
format = "jsonl"
path = "~/.aetherward/sessions/session.jsonl"
"""

    def test_parse_toml(self, tmp_path):
        p = tmp_path / 'cfg.toml'
        p.write_bytes(self.TOML.encode())
        cfg = AWConfig.from_toml(str(p))
        assert cfg.mode == 'trilateration'
        assert cfg.array_id == 'lab-array'
        assert len(cfg.antennas) == 2
        assert tuple(cfg.antennas[1].position) == (0.5, 0.0, 0.0)
        assert cfg.gps.lat == pytest.approx(48.856600)
        assert cfg.sync.source == 'pps'
        assert cfg.mode_config['channel'] == 6


# ── AntennaConfig defaults ────────────────────────────────────────────────────

class TestAntennaConfig:
    def test_defaults(self):
        a = AntennaConfig(id='x', backend='null')
        assert a.position == (0.0, 0.0, 0.0)
        assert a.orientation_euler == (0.0, 0.0, 0.0)
        assert a.gain_dbi == 0.0
        assert a.pattern == 'isotropic'
        assert a.backend_config == {}

    def test_custom_position(self):
        a = AntennaConfig(id='x', backend='null', position=(1.0, 2.0, 3.0))
        assert a.position == (1.0, 2.0, 3.0)

    def test_frequency_range(self):
        a = AntennaConfig(id='x', backend='null',
                          frequency_range=(2400e6, 2500e6))
        assert a.frequency_range[0] == pytest.approx(2400e6)
        assert a.frequency_range[1] == pytest.approx(2500e6)


# ── GPSConfig defaults ────────────────────────────────────────────────────────

class TestGPSConfig:
    def test_defaults(self):
        g = GPSConfig()
        assert g.backend == 'gpsd'
        assert g.host == 'localhost'
        assert g.port == 2947
        assert g.lat is None
        assert g.lon is None
        assert g.alt == 0.0

    def test_static(self):
        g = GPSConfig(backend='static', lat=51.5, lon=-0.12, alt=10.0)
        assert g.lat == pytest.approx(51.5)
        assert g.lon == pytest.approx(-0.12)


# ── SyncConfig ────────────────────────────────────────────────────────────────

class TestSyncConfig:
    def test_defaults(self):
        s = SyncConfig()
        assert s.source == 'software'
        assert s.device == ''

    def test_pps(self):
        s = SyncConfig(source='pps', device='/dev/pps0')
        assert s.source == 'pps'
        assert s.device == '/dev/pps0'
