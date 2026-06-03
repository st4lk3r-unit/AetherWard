"""Tests for GPS backends: StaticGPSBackend and null/none path."""
from __future__ import annotations

import time

import pytest

from aetherward.hardware.gps import StaticGPSBackend
from aetherward.position.absolute import AbsolutePosition, FixType


class TestStaticGPSBackend:
    def _backend(self, lat=48.8566, lon=2.3522, alt=35.0):
        return StaticGPSBackend(lat=lat, lon=lon, alt=alt)

    def test_initialize_does_not_raise(self):
        b = self._backend()
        b.initialize()  # must be a no-op

    def test_get_position_returns_absolute_position(self):
        b = self._backend()
        p = b.get_position()
        assert isinstance(p, AbsolutePosition)

    def test_coordinates_correct(self):
        b = self._backend(lat=51.5074, lon=-0.1278, alt=10.0)
        p = b.get_position()
        assert p.lat == pytest.approx(51.5074)
        assert p.lon == pytest.approx(-0.1278)
        assert p.alt == pytest.approx(10.0)

    def test_fix_type_is_3d(self):
        b = self._backend()
        p = b.get_position()
        assert p.fix_type == FixType.FIX_3D
        assert p.is_valid()

    def test_accuracy_is_zero(self):
        b = self._backend()
        p = b.get_position()
        assert p.accuracy_h == pytest.approx(0.0)
        assert p.accuracy_v == pytest.approx(0.0)

    def test_timestamp_is_recent(self):
        b = self._backend()
        before = time.time()
        p = b.get_position()
        after = time.time()
        assert before <= p.timestamp <= after

    def test_each_call_gives_fresh_timestamp(self):
        b = self._backend()
        p1 = b.get_position()
        time.sleep(0.01)
        p2 = b.get_position()
        assert p2.timestamp >= p1.timestamp

    def test_close_does_not_raise(self):
        b = self._backend()
        b.initialize()
        b.close()

    def test_pps_timestamp_returns_none(self):
        b = self._backend()
        assert b.get_pps_timestamp() is None

    def test_equator_south_pole(self):
        for lat in [0.0, -90.0, 90.0]:
            b = StaticGPSBackend(lat=lat, lon=0.0)
            p = b.get_position()
            assert p.lat == pytest.approx(lat)

class TestGPSDBackendFreshPoll:
    def test_get_position_drops_streaming_backlog_and_uses_poll_snapshot(self, monkeypatch):
        import select
        from aetherward.hardware.gps import GPSDBackend

        class FakeSock:
            def __init__(self):
                self.chunks = [
                    b'{"class":"TPV","mode":3,"lat":1.0,"lon":1.0,"time":"2026-06-02T00:00:00Z"}\n'
                ]
                self.sent = []

            def sendall(self, data):
                self.sent.append(data)
                if data == b'?POLL;\n':
                    self.chunks.append(
                        b'{"class":"POLL","tpv":['
                        b'{"class":"TPV","mode":3,"lat":2.0,"lon":2.0,"time":"2026-06-02T00:00:01Z"},'
                        b'{"class":"TPV","mode":3,"lat":3.0,"lon":3.0,"time":"2026-06-02T00:00:02Z"}'
                        b']}\n'
                    )

            def recv(self, n):
                return self.chunks.pop(0) if self.chunks else b''

        fake = FakeSock()

        def fake_select(readers, *_args, **_kwargs):
            return (readers, [], []) if fake.chunks else ([], [], [])

        monkeypatch.setattr(select, 'select', fake_select)
        b = GPSDBackend()
        b._sock = fake
        pos = b.get_position()

        assert pos is not None
        assert pos.lat == pytest.approx(3.0)
        assert pos.lon == pytest.approx(3.0)
        assert b'?POLL;\n' in fake.sent
