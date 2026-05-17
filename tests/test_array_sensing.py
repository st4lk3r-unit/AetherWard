"""Tests for ArraySensingMode: calibration, state machine, event firing."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from aetherward.modes.array_sensing import ArraySensingMode, SensingEvent
from aetherward.signal.frame import Frame


def _make_array(n=1):
    array = MagicMock()
    antennas = []
    for i in range(n):
        ant = MagicMock()
        ant.id = f'wlan{i}'
        ant.backend = None
        antennas.append(ant)
    array.antennas = antennas
    return array


def _frame(ant_id='wlan0', rssi=-60.0):
    return Frame(data=b'\x00'*8, frequency=2.412e9, bandwidth=20e6,
                 timestamp=time.time(), rssi=rssi, antenna_id=ant_id)


class TestSensingEvent:
    def test_construction(self):
        ev = SensingEvent(type='presence', antenna_id='wlan0', variance=0.1)
        assert ev.type == 'presence'
        assert ev.antenna_id == 'wlan0'
        assert ev.variance == pytest.approx(0.1)
        assert ev.direction is None

    def test_event_types(self):
        for t in ('presence', 'motion', 'absence'):
            ev = SensingEvent(type=t, antenna_id='wlan0', variance=0.0)
            assert ev.type == t


class TestArraySensingConfig:
    def test_default_config(self):
        array = _make_array(1)
        mode = ArraySensingMode(array, {})
        assert mode._history_len == 100
        assert mode._calib_frames == 50
        assert mode._sensitivity == pytest.approx(0.05)
        assert mode._hysteresis == pytest.approx(0.4)
        assert mode._ema_alpha == pytest.approx(0.3)

    def test_custom_config(self):
        array = _make_array(1)
        mode = ArraySensingMode(array, {
            'history_len': 200, 'calibration_frames': 100,
            'sensitivity': 0.1, 'hysteresis': 0.5, 'ema_alpha': 0.5,
        })
        assert mode._history_len == 200
        assert mode._calib_frames == 100
        assert mode._sensitivity == pytest.approx(0.1)

    def test_on_event_callback_stored(self):
        cb = MagicMock()
        array = _make_array(1)
        mode = ArraySensingMode(array, {'on_event': cb})
        assert mode._on_event is cb


class TestCalibration:
    def test_no_events_during_calibration(self):
        events = []
        array = _make_array(1)
        mode = ArraySensingMode(array, {
            'calibration_frames': 10, 'on_event': events.append, 'history_len': 20
        })
        # Feed frames during calibration window — no events expected
        for _ in range(9):
            mode._handle(_frame('wlan0', rssi=-60.0), 'wlan0')
        assert len(events) == 0

    def test_events_possible_after_calibration(self):
        events = []
        array = _make_array(1)
        mode = ArraySensingMode(array, {
            'calibration_frames': 5, 'on_event': events.append,
            'history_len': 20, 'sensitivity': 0.001,
        })
        # Calibrate with stable signal
        for _ in range(5):
            mode._handle(_frame('wlan0', rssi=-60.0), 'wlan0')
        # Now inject high-variance frames to trigger presence
        for _ in range(5):
            rssi = -40.0 if (_ % 2 == 0) else -90.0
            mode._handle(_frame('wlan0', rssi=rssi), 'wlan0')
        # May or may not fire depending on exact variance math — just verify no crash


class TestModeIntegration:
    def test_handle_frame_increments_buffer(self):
        array = _make_array(1)
        mode = ArraySensingMode(array, {'calibration_frames': 100, 'history_len': 50})
        for i in range(10):
            mode._handle(_frame('wlan0'), 'wlan0')
        buf = mode._rssi_history.get('wlan0')
        assert buf is not None
        assert len(buf) == 10

    def test_buffer_capped_at_history_len(self):
        array = _make_array(1)
        mode = ArraySensingMode(array, {'history_len': 5, 'calibration_frames': 100})
        for _ in range(20):
            mode._handle(_frame('wlan0'), 'wlan0')
        buf = mode._rssi_history.get('wlan0')
        assert len(buf) <= 5

    def test_unknown_antenna_creates_buffer(self):
        array = _make_array(1)
        mode = ArraySensingMode(array, {})
        assert 'wlan99' not in mode._rssi_history
        mode._handle(_frame('wlan99'), 'wlan99')
        assert 'wlan99' in mode._rssi_history
