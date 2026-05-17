"""
Source identification and frame routing in WardriverMode.

Verifies:
  - Frames with the same identifier merge into one SignalSource regardless of time.
  - Anonymous frames in the same 1-second bucket merge into one source.
  - Anonymous frames in different seconds create distinct sources (known limitation;
    this test documents the boundary behaviour as a regression anchor).
  - on_source callback fires exactly once per new source.
  - on_observation callback fires for every frame, including repeat observations.
  - observation_count on a source tracks all frames seen.
  - GPS position is snapshotted onto each Observation at capture time.
  - stop() closes the output file cleanly.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aetherward.modes.wardriver import WardriverMode
from aetherward.position.absolute import AbsolutePosition, FixType
from aetherward.signal.frame import Frame


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_array(n: int = 1):
    array = MagicMock()
    antennas = []
    for i in range(n):
        ant = MagicMock()
        ant.id = f'wlan{i}'
        ant.backend = None
        antennas.append(ant)
    array.antennas = antennas
    array.n = n
    array.absolute_position = None
    return array


def _frame(antenna_id: str = 'wlan0',
           timestamp: float = 1_000_000.0,
           freq: float = 2.412e9,
           identifier: str | None = None,
           protocol: str | None = None) -> Frame:
    meta: dict = {}
    if identifier is not None:
        meta['identifier'] = identifier
    if protocol is not None:
        meta['protocol'] = protocol
    return Frame(
        data=b'\x00' * 16,
        frequency=freq,
        bandwidth=20e6,
        timestamp=timestamp,
        rssi=-65.0,
        antenna_id=antenna_id,
        metadata=meta,
    )


def _mode(**cfg) -> WardriverMode:
    return WardriverMode(_make_array(), cfg)


# ── Identified source merging ─────────────────────────────────────────────────

class TestIdentifiedSourceMerging:
    def test_same_identifier_merges_across_time(self):
        mode = _mode()
        mac = 'aa:bb:cc:dd:ee:ff'
        for i in range(5):
            mode._handle(_frame(timestamp=1_000_000.0 + i * 2.0, identifier=mac), 'wlan0')

        assert len(mode.sources) == 1
        src = next(iter(mode.sources.values()))
        assert src.observation_count == 5

    def test_different_identifiers_create_separate_sources(self):
        mode = _mode()
        for mac in ('aa:bb:cc:00:00:01', 'aa:bb:cc:00:00:02', 'aa:bb:cc:00:00:03'):
            mode._handle(_frame(identifier=mac), 'wlan0')

        assert len(mode.sources) == 3

    def test_identifier_source_key_includes_frequency(self):
        """Same MAC on different frequencies → separate sources."""
        mode = _mode()
        mode._handle(_frame(freq=2.412e9, identifier='de:ad:be:ef:00:01'), 'wlan0')
        mode._handle(_frame(freq=5.180e9, identifier='de:ad:be:ef:00:01'), 'wlan0')

        assert len(mode.sources) == 2

    def test_source_signal_properties_populated(self):
        mode = _mode()
        mode._handle(_frame(freq=2.412e9, identifier='11:22:33:44:55:66',
                            protocol='wifi'), 'wlan0')

        src = next(iter(mode.sources.values()))
        assert src.signal.frequency == pytest.approx(2.412e9)
        assert src.signal.protocol == 'wifi'
        assert src.signal.identifier == '11:22:33:44:55:66'

    def test_mean_rssi_updated_across_observations(self):
        mode = _mode()
        mac = 'ff:ee:dd:cc:bb:aa'
        rssies = [-70.0, -60.0, -50.0]
        for rssi in rssies:
            f = _frame(identifier=mac)
            f.rssi = rssi
            mode._handle(f, 'wlan0')

        src = next(iter(mode.sources.values()))
        assert src.mean_rssi == pytest.approx(sum(rssies) / len(rssies), abs=0.1)


# ── Anonymous source bucketing ────────────────────────────────────────────────

class TestAnonymousBucketing:
    def test_same_second_merges_into_one_source(self):
        mode = _mode()
        ts = 1_000_000.0
        for offset in (0.0, 0.3, 0.7):     # all within second 1_000_000
            mode._handle(_frame(timestamp=ts + offset), 'wlan0')

        assert len(mode.sources) == 1
        src = next(iter(mode.sources.values()))
        assert src.observation_count == 3

    def test_different_seconds_create_separate_sources(self):
        """
        Known limitation: anonymous frames that straddle a 1-second boundary
        are split into separate sources.  This test documents the current
        behaviour as a regression anchor — do not remove without fixing the
        bucketing logic.
        """
        mode = _mode()
        mode._handle(_frame(timestamp=1_000_000.1), 'wlan0')   # bucket 1_000_000
        mode._handle(_frame(timestamp=1_000_001.1), 'wlan0')   # bucket 1_000_001

        assert len(mode.sources) == 2

    def test_anonymous_different_frequencies_separate(self):
        mode = _mode()
        ts = 1_000_000.0
        mode._handle(_frame(freq=2.412e9, timestamp=ts), 'wlan0')
        mode._handle(_frame(freq=5.180e9, timestamp=ts), 'wlan0')

        assert len(mode.sources) == 2


# ── Callbacks ─────────────────────────────────────────────────────────────────

class TestCallbacks:
    def test_on_source_fires_exactly_once_per_new_source(self):
        fired = []
        mode = _mode(on_source=fired.append)
        mac = 'ca:fe:ba:be:00:01'

        for _ in range(5):
            mode._handle(_frame(identifier=mac), 'wlan0')

        assert len(fired) == 1

    def test_on_source_fires_for_each_distinct_source(self):
        fired = []
        mode = _mode(on_source=fired.append)

        for i in range(4):
            mode._handle(_frame(identifier=f'00:00:00:00:00:0{i}'), 'wlan0')

        assert len(fired) == 4

    def test_on_observation_fires_for_every_frame(self):
        obs_log = []
        mode = _mode(on_observation=obs_log.append)
        mac = 'aa:aa:aa:aa:aa:aa'

        for _ in range(7):
            mode._handle(_frame(identifier=mac), 'wlan0')

        assert len(obs_log) == 7

    def test_on_source_receives_signal_source_object(self):
        from aetherward.signal.source import SignalSource
        fired = []
        mode = _mode(on_source=fired.append)
        mode._handle(_frame(identifier='11:22:33:44:55:66'), 'wlan0')

        assert len(fired) == 1
        assert isinstance(fired[0], SignalSource)

    def test_callbacks_not_required(self):
        """Mode must not crash when no callbacks are configured."""
        mode = _mode()
        mode._handle(_frame(identifier='no:cb:00:00:00:01'), 'wlan0')
        assert len(mode.sources) == 1


# ── GPS snapshotting ──────────────────────────────────────────────────────────

class TestGPSSnapshot:
    def test_gps_position_snapshotted_on_observation(self):
        array = _make_array()
        gps = AbsolutePosition(lat=48.8566, lon=2.3522, alt=35.0,
                               fix_type=FixType.FIX_3D)
        array.absolute_position = gps

        mode = WardriverMode(array, {})
        mode._handle(_frame(identifier='gp:s0:00:00:00:01'), 'wlan0')

        src = next(iter(mode.sources.values()))
        obs = src.observations[0]
        assert obs.array_absolute is gps

    def test_no_gps_observation_has_none_position(self):
        array = _make_array()
        array.absolute_position = None

        mode = WardriverMode(array, {})
        mode._handle(_frame(identifier='no:gp:s0:00:00:01'), 'wlan0')

        src = next(iter(mode.sources.values()))
        assert src.observations[0].array_absolute is None


# ── JSONL output ──────────────────────────────────────────────────────────────

class TestJsonlOutput:
    def test_gps_fields_written_when_fix_valid(self, tmp_path):
        out = str(tmp_path / 'out.jsonl')
        array = _make_array()
        array.absolute_position = AbsolutePosition(
            lat=48.0, lon=2.0, alt=10.0, fix_type=FixType.FIX_3D
        )
        mode = WardriverMode(array, {'output_path': out})
        mode.start()
        mode._handle(_frame(identifier='aa:bb:cc:dd:ee:ff'), 'wlan0')
        mode.stop()

        lines = Path(out).read_text().strip().splitlines()
        assert lines
        rec = json.loads(lines[0])
        assert 'lat' in rec and 'lon' in rec
        assert rec['lat'] == pytest.approx(48.0)

    def test_no_gps_fields_when_no_fix(self, tmp_path):
        out = str(tmp_path / 'nogps.jsonl')
        array = _make_array()
        array.absolute_position = None
        mode = WardriverMode(array, {'output_path': out})
        mode.start()
        mode._handle(_frame(identifier='aa:bb:cc:dd:ee:01'), 'wlan0')
        mode.stop()

        rec = json.loads(Path(out).read_text().strip())
        assert 'lat' not in rec
        assert 'lon' not in rec

    def test_jsonl_required_fields(self, tmp_path):
        out = str(tmp_path / 'req.jsonl')
        array = _make_array()
        array.absolute_position = None
        mode = WardriverMode(array, {'output_path': out})
        mode.start()
        mode._handle(_frame(freq=2.412e9, identifier='de:ad:be:ef:00:02'), 'wlan0')
        mode.stop()

        rec = json.loads(Path(out).read_text().strip())
        for field in ('t', 'freq', 'rssi', 'ant'):
            assert field in rec, f"required field '{field}' missing from JSONL output"

    def test_stop_closes_output_file(self, tmp_path):
        out = str(tmp_path / 'close.jsonl')
        array = _make_array()
        array.absolute_position = None
        mode = WardriverMode(array, {'output_path': out})
        mode.start()
        mode.stop()
        assert mode._out_file is None
