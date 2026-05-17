"""Tests for signal primitives: Frame, Observation, SignalSource."""
from __future__ import annotations

import time

import pytest

from aetherward.signal.frame import Frame
from aetherward.signal.observation import Observation
from aetherward.signal.source import SignalProperties, SignalSource


def _frame(**kw):
    defaults = dict(data=b'\x00' * 16, frequency=2.412e9, bandwidth=20e6,
                    timestamp=1_000_000.0, rssi=-70.0, antenna_id='wlan0')
    defaults.update(kw)
    return Frame(**defaults)


class TestFrame:
    def test_construction(self):
        f = _frame()
        assert f.frequency == pytest.approx(2.412e9)
        assert f.rssi == pytest.approx(-70.0)
        assert f.antenna_id == 'wlan0'
        assert len(f.data) == 16

    def test_now_sets_timestamp(self):
        before = time.time()
        f = Frame.now(data=b'', frequency=2.4e9, bandwidth=20e6,
                      rssi=-60.0, antenna_id='wlan0')
        after = time.time()
        assert before <= f.timestamp <= after

    def test_repr_contains_freq(self):
        f = _frame(frequency=2412e6)
        r = repr(f)
        assert '2412' in r or '2.412' in r

    def test_sample_rate_default(self):
        assert _frame().sample_rate == 0.0

    def test_metadata_default_empty(self):
        assert _frame().metadata == {}

    def test_metadata_stored(self):
        f = _frame(metadata={'ssid': 'HomeWifi', 'ch': 6})
        assert f.metadata['ssid'] == 'HomeWifi'


class TestObservation:
    def test_construction(self):
        f = _frame()
        obs = Observation(frame=f, antenna_id='wlan0', rssi=-65.0)
        assert obs.antenna_id == 'wlan0'
        assert obs.rssi == pytest.approx(-65.0)
        assert obs.frame is f

    def test_optional_fields_default_none(self):
        obs = Observation(frame=_frame(), antenna_id='wlan0', rssi=-70.0)
        assert obs.tdoa is None
        assert obs.csi is None
        assert obs.doppler is None
        assert obs.array_absolute is None

    def test_repr(self):
        obs = Observation(frame=_frame(), antenna_id='wlan1', rssi=-55.0)
        r = repr(obs)
        assert 'wlan1' in r
        assert '-55.0' in r

    def test_metadata_default_empty(self):
        obs = Observation(frame=_frame(), antenna_id='wlan0', rssi=-70.0)
        assert obs.metadata == {}


class TestSignalSource:
    def _props(self, freq=2.412e9):
        return SignalProperties(frequency=freq, bandwidth=20e6, protocol='wifi')

    def test_new_source_has_zero_observations(self):
        src = SignalSource(signal=self._props())
        assert src.observation_count == 0

    def test_explicit_id(self):
        src = SignalSource(signal=self._props(), id='aa:bb:cc')
        assert src.id == 'aa:bb:cc'

    def test_auto_id_generated(self):
        src = SignalSource(signal=self._props())
        assert len(src.id) > 0

    def test_add_observation_increments_count(self):
        src = SignalSource(signal=self._props())
        obs = Observation(frame=_frame(timestamp=1e6), antenna_id='wlan0', rssi=-70.0)
        src.add_observation(obs)
        assert src.observation_count == 1

    def test_last_seen_updated_from_frame_timestamp(self):
        src = SignalSource(signal=self._props())
        t = 999_999.5
        obs = Observation(frame=_frame(timestamp=t), antenna_id='wlan0', rssi=-70.0)
        src.add_observation(obs)
        assert src.last_seen == pytest.approx(t)

    def test_mean_rssi(self):
        src = SignalSource(signal=self._props())
        for rssi in [-60.0, -65.0, -70.0]:
            src.add_observation(Observation(frame=_frame(rssi=rssi), antenna_id='wlan0', rssi=rssi))
        assert src.mean_rssi == pytest.approx(-65.0)

    def test_max_rssi(self):
        src = SignalSource(signal=self._props())
        for rssi in [-80.0, -60.0, -75.0]:
            src.add_observation(Observation(frame=_frame(rssi=rssi), antenna_id='wlan0', rssi=rssi))
        assert src.max_rssi == pytest.approx(-60.0)

    def test_mean_rssi_none_when_no_obs(self):
        src = SignalSource(signal=self._props())
        assert src.mean_rssi is None
        assert src.max_rssi is None

    def test_repr(self):
        src = SignalSource(signal=self._props(), id='aa:bb')
        r = repr(src)
        assert 'aa:bb' in r
