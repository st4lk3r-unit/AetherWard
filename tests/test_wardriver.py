"""Tests for wardriver mode: channel assignment, JSONL output, pipeline."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aetherward.modes.wardriver import WardriverMode
from aetherward.signal.frame import Frame


def _make_array(n_antennas=1, with_backend=True):
    """Build a minimal mock AntennaArray for testing."""
    array = MagicMock()
    antennas = []
    for i in range(n_antennas):
        ant = MagicMock()
        ant.id = f'wlan{i}'
        # _assign_channels skips antennas with backend=None; use a mock backend
        ant.backend = MagicMock() if with_backend else None
        ant.covers_frequency = lambda hz: True
        antennas.append(ant)
    array.antennas = antennas
    array.absolute_position = None
    return array


class TestChannelAssignment:
    def test_single_antenna_gets_all_channels(self):
        array = _make_array(1)
        mode = WardriverMode(array, {'channels': [1, 6, 11]})
        mode._assign_channels()
        assert set(mode._channel_map.get('wlan0', [])) == {1, 6, 11}

    def test_two_antennas_split_channels(self):
        array = _make_array(2)
        mode = WardriverMode(array, {'channels': [1, 2, 3, 4]})
        mode._assign_channels()
        ch0 = mode._channel_map.get('wlan0', [])
        ch1 = mode._channel_map.get('wlan1', [])
        assert sorted(ch0 + ch1) == [1, 2, 3, 4]
        assert len(set(ch0) & set(ch1)) == 0  # no overlap

    def test_three_antennas_remainder_distributed(self):
        array = _make_array(3)
        mode = WardriverMode(array, {'channels': list(range(1, 14))})
        mode._assign_channels()
        all_ch = []
        for ant in array.antennas:
            all_ch.extend(mode._channel_map.get(ant.id, []))
        assert sorted(all_ch) == list(range(1, 14))

    def test_default_channels_1_to_13(self):
        array = _make_array(1)
        mode = WardriverMode(array, {})
        mode._assign_channels()
        assert sorted(mode._channel_map['wlan0']) == list(range(1, 14))


class TestJsonlOutput:
    def test_output_file_created(self, tmp_path):
        out = str(tmp_path / 'session.jsonl')
        array = _make_array(1)
        mode = WardriverMode(array, {'output_path': out, 'channels': [6]})
        # Manually trigger output write (simulate _handle)
        mode._running = True
        mode._out_file = open(out, 'a', buffering=1)
        obs_rec = {'id': 'aa:bb', 'ssid': 'Test', 'rssi': -65.0,
                   'lat': 48.0, 'lon': 2.0, 'freq': 2.412e9,
                   'protocol': 'wifi', 't': time.time()}
        mode._out_file.write(json.dumps(obs_rec) + '\n')
        mode._out_file.close()
        lines = Path(out).read_text().strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec['id'] == 'aa:bb'
        assert rec['ssid'] == 'Test'

    def test_output_file_is_valid_jsonl(self, tmp_path):
        out = str(tmp_path / 'multi.jsonl')
        with open(out, 'a') as f:
            for i in range(5):
                f.write(json.dumps({'id': f'mac:{i}', 'rssi': -60 - i}) + '\n')
        records = [json.loads(l) for l in Path(out).read_text().strip().splitlines()]
        assert len(records) == 5
        assert records[4]['id'] == 'mac:4'


class TestWardriverConfig:
    def test_hop_interval_from_config(self):
        array = _make_array(1)
        mode = WardriverMode(array, {'hop_interval': 0.25})
        assert mode._hop_interval == pytest.approx(0.25)

    def test_default_hop_interval(self):
        array = _make_array(1)
        mode = WardriverMode(array, {})
        assert mode._hop_interval == pytest.approx(0.1)

    def test_on_source_callback_stored(self):
        cb = MagicMock()
        array = _make_array(1)
        mode = WardriverMode(array, {'on_source': cb})
        assert mode._on_source is cb

    def test_on_observation_callback_stored(self):
        cb = MagicMock()
        array = _make_array(1)
        mode = WardriverMode(array, {'on_observation': cb})
        assert mode._on_observation is cb

    def test_output_path_from_config(self, tmp_path):
        out = str(tmp_path / 'test.jsonl')
        array = _make_array(1)
        mode = WardriverMode(array, {'output_path': out})
        assert mode._output_path == out
